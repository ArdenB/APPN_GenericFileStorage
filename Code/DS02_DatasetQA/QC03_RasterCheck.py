"""Raster check (QC03) — reflectance orthomosaic data-validity scan.

Crawls the APPN dataset tree for processed runs (``<run>/T1_proc/*.gpro``)
and scans the **ortho reflectance products only** (VNIR + SWIR ENVI
``.bin``/``.hdr`` pairs under ``gpro/products/``) — radiance and
intermediates are out of scope. Values are reflectance x 10^4, so the
physical range is 0-10000 (design record: the retired QC pipeline plan
§5c, in this repo's git history).

Check set (per product, per band + whole-cube):

- ``zeros_in_footprint`` — fraction of pixels with reflectance = 0 inside
  the data footprint. Background/nodata is also 0, so the footprint is
  established first: all-bands-zero => background; zero in some bands but
  not others => suspect data.
- ``dropout_in_roi`` — all-bands-zero fraction of the *region of
  interest*: the flown-area capture polygon
  (``<gpro>/extents/hyper_extent.geojson``) eroded inward by the swath
  margin. All-band scan-line dropouts and data holes are invisible to
  ``zeros_in_footprint`` (they are absorbed into "background"), so the
  capture polygon becomes the analysis domain — everything outside it is
  discarded, the eroded ROI is graded, and the ring between them is
  advisory-only.
- ``zero_edge_band`` — all-bands-zero fraction of the bbox-minus-ROI
  ring. Expected incomplete capture at the swath edges (a known GOBI/
  CALViS failure mode): reported, never graded.
- ``data_outside_bbox`` — nonzero pixels outside the capture polygon at
  0.5 px tolerance. A sanity guard on the extent/raster pairing (stale
  or mismatched extent file), not a data defect.
- ``capture_extent`` — the capture polygon is required whenever an
  orthomosaic exists; absence warns and downgrades the zone split to the
  border-connectivity fallback classifier.
- ``over_range`` — fraction of pixels > 10000 (impossible reflectance;
  ELM extrapolation / specular / saturation tell), plus the max value
  and worst-band identification.
- ``negative`` — fraction < 0 (signed dtypes only).
- ``nan_inf`` — NaN/Inf counts (float products only).
- ``header_bin_integrity`` — ``.bin`` size matches ``lines x samples x
  bands x dtype`` from the ``.hdr``; wavelength-list length matches the
  band count. A missing/unreadable header fails this check and skips
  that product's scan — the crawl continues.
- Recorded but not gated: all-constant bands.

Known-bad wavelength ranges (``spectral_qc.default_bad_wavelengths``) are
masked before the whole-cube roll-ups so known-bad SWIR bands don't
dominate the fractions; per-band statistics are still reported for every
band with a ``bad_band`` flag.

**Advisory:** thresholds ship as uncalibrated defaults
(``reference/thresholds/raster_validity.yml``) and grade
``warning``/``fail``, but nothing downstream is voided — QC03 becomes a
gate for DS03/DS05 only after background rates are calibrated via the
reserved QA03_RasterComparison. Unlike QC01/QC02, a QC00 GNSS reprocess
does *not* void QC03 (values are ELM-derived, not trajectory-derived),
but an ELM reprocess does — the §2 staleness fields cover this.

Outputs (per run, §4 layout):

- ``QC_data/QC03_RasterCheck_summary.yaml`` — contract summary.
- ``QC_data/QC03_RasterCheck/QC03_RasterCheck_detail.json`` — contract
  detail with per-band statistics (min/max/mean/percentiles, bad-pixel
  fractions) so QA03 can aggregate later.

Command-line Arguments
----------------------
--path : str, optional
    Folder to crawl for processed runs (any APPN tree level). Defaults
    to the git repo root.
--spec : str
    Raster-validity threshold YAML relative to the repo root
    (default: reference/thresholds/raster_validity.yml).
--chunk-mb : int
    Target memory per read chunk in MB (default 256; GDAL fallback
    path only).
--threads : int
    Reader threads for the raw BSQ fast path (default 4, a good fit
    for SSD/NAS storage; use 1 on a single spinning disk to avoid
    seek thrash).
--exclude-dir : str [str ...]
    Directory names to exclude from the crawl.
--allow-multi-gpro : flag
    Process runs holding more than one ``.gpro`` bundle instead of
    skipping them (product labels get a ``_gproN`` suffix). Debugging
    only: the product set is ambiguous.
--force : flag
    Regenerate reports even when they are newer than every input.
--verbose : flag
    Print extra diagnostic information.
"""

# ==============================================================================

__title__ = "Raster check"
__author__ = "Arden Burrell"
__version__ = "v1.6(03.09.2026)"
__email__ = "arden.burrell@sydney.edu.au"

# ==============================================================================
# ========== Import core packages ==========
import os
import sys
import argparse
import json
import pathlib
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

# ========== Import other packages ==========
import git
from git import exc as git_exc
import numpy as np
import pandas as pd
import rasterio
from rasterio.crs import CRS
from rasterio.errors import RasterioIOError
from rasterio.warp import transform as rio_transform
from scipy import ndimage
from tqdm import tqdm
import warnings as warn

# ========== Resolve git root (must happen before importing functions.*) ==========
try:
    _git_root = git.Repo(
        os.getcwd(), search_parent_directories=True
    ).git.rev_parse("--show-toplevel")
except git_exc.InvalidGitRepositoryError as err:
    raise git_exc.InvalidGitRepositoryError(
        f"Script must be run from inside a git repo (cwd={os.getcwd()})."
    ) from err
if _git_root not in sys.path:
    sys.path.insert(0, _git_root)

# ========== Import custom packages ==========
import Code.functions.core_functions as cf
import Code.functions.spectral_qc as sq
import Code.functions.qc_report as qr


# ==================================================================================
def main(args: argparse.Namespace) -> pd.DataFrame:
    """Top-level orchestration. Reads like pseudocode.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.

    Returns
    -------
    pd.DataFrame
        End-of-run summary: one row per discovered run with columns
        ``project, site, sensor, date, run, status, reason``.
    """
    path = pathlib.Path(args.path) if args.path else pathlib.Path(_git_root)
    # ========== Step 1: load the validity thresholds (with provenance) ==========
    spec, spec_snapshot = load_validity_spec(pathlib.Path(args.spec))
    # ========== Step 2: discover processed runs ==========
    run_dirs = find_run_dirs(path, exclude_dirs=args.exclude_dir)
    # ========== Step 3: per-run scans ==========
    rows: List[Dict[str, Any]] = []
    for run_dir in tqdm(run_dirs, desc="QC03 raster check"):
        rows.append(process_run(run_dir, spec, spec_snapshot, args))
    # ========== Step 4: end-of-run summary ==========
    return _print_run_summary(rows)


# ==================================================================================
def load_validity_spec(
    path: pathlib.Path,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Load the raster-validity threshold YAML with its config snapshot.

    Parameters
    ----------
    path : pathlib.Path
        Path to ``raster_validity.yml`` (repo-relative by default).

    Returns
    -------
    tuple
        ``(spec, snapshot)`` — the parsed thresholds plus the
        ``{"path", "sha256"}`` provenance snapshot, or built-in
        defaults with ``snapshot=None`` (and a warning) when missing.
    """
    if not path.is_file():
        warn.warn(f"Raster-validity spec {path} missing - using built-in "
                  "advisory defaults (config snapshot will be empty).")
        return {
            "zeros_in_footprint_pct": {"warn_above": 0.5, "fail_above": 5.0},
            "over_range_pct": {"warn_above": 0.1, "fail_above": 1.0},
            "negative_pct": {"warn_above": 0.1, "fail_above": 1.0},
            "nan_inf_count": {"warn_above": 0},
            "dropout_in_roi_pct": {"warn_above": 0.1},
            "data_outside_bbox_pct": {"warn_above": 0.01},
            "zero_zones": {"short_axis_fraction": 0.10,
                           "inset_factor": 0.5},
            "reflectance_max": 10000,
        }, None
    loaded = qr.load_thresholds(path.name, thresholds_dir=path.parent)
    return loaded["spec"], {"path": loaded["path"], "sha256": loaded["sha256"]}


# ==================================================================================
def find_run_dirs(
    path: pathlib.Path,
    exclude_dirs: Optional[List[str]] = None,
) -> List[pathlib.Path]:
    """Discover run directories holding a processed bundle under *path*.

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively (any APPN tree level).
    exclude_dirs : list of str, optional
        Directory names to exclude from the search.

    Returns
    -------
    list of pathlib.Path
        Sorted unique run directories with a ``T1_proc/*.gpro`` bundle.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    """
    print(f"Scanning {path} for processed runs. {pd.Timestamp.now()}")
    if not path.is_dir():
        raise FileNotFoundError(f"Search path does not exist: {path}")
    exclude_set = set(exclude_dirs or [])

    def _excluded(p: pathlib.Path) -> bool:
        return bool(exclude_set
                    and (set(par.name for par in p.parents) & exclude_set))

    run_dirs = sorted({
        g.parent.parent for g in path.rglob("*.gpro")
        if g.parent.name == "T1_proc" and g.is_dir() and not _excluded(g)
    })
    print(f"Found {len(run_dirs)} run(s) with a .gpro bundle.")
    if not run_dirs:
        parsed = cf.parse_APPN_dataset_path(path)
        if not parsed["valid"]:
            warn.warn(
                f"{path} is not a valid APPN dataset path: "
                + " ".join(parsed["errors"]))
    return run_dirs


# ==================================================================================
def process_run(
    run_dir: pathlib.Path,
    spec: Dict[str, Any],
    spec_snapshot: Optional[Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    """Scan every reflectance orthomosaic of one run and write the report.

    Parameters
    ----------
    run_dir : pathlib.Path
        Run directory containing ``T1_proc/*.gpro``.
    spec : dict
        Parsed validity thresholds.
    spec_snapshot : dict or None
        ``{"path", "sha256"}`` config snapshot for the detail JSON.
    args : argparse.Namespace
        Parsed CLI arguments.

    Returns
    -------
    dict
        Summary row: run metadata plus ``status`` and ``reason``.
    """
    parsed = cf.parse_APPN_dataset_path(run_dir)
    row = {key: parsed.get(key)
           for key in ("project", "site", "sensor", "run")}
    row["date"] = parsed.get("date")
    row.update({"status": "skipped", "reason": None})

    # ========== Malformed APPN trees: report why, never grade ==========
    if not parsed["valid"]:
        row["reason"] = ("invalid APPN folder structure: "
                         + " ".join(parsed["errors"]))
        return row

    # ========== Multiple .gpro bundles are ambiguous (repo convention) ==========
    gpros = sorted((run_dir / "T1_proc").glob("*.gpro"))
    if len(gpros) > 1:
        if args.allow_multi_gpro:
            warn.warn(
                f"{run_dir} holds multiple .gpro bundles "
                f"({[g.name for g in gpros]}); --allow-multi-gpro is set - "
                "product labels get a _gproN suffix. Debugging only.")
        else:
            row["reason"] = (
                f"multiple .gpro bundles ({[g.name for g in gpros]}) - "
                "ambiguous; rerun with --allow-multi-gpro to process anyway")
            return row

    products = find_ortho_products(run_dir)
    if not products:
        row["reason"] = "no reflectance orthomosaics under T1_proc"
        return row

    # ========== Skip when cached outputs are current ==========
    qc_data = run_dir / "T1_proc" / "QC_data"
    summary_path, detail_path = qr.report_paths(qc_data, "QC03_RasterCheck")
    line_spacing_m, spacing_source = qc01_line_spacing(run_dir)
    zones_spec = spec.get("zero_zones", {}) or {}
    inputs = [p["bin"] for p in products] + \
             [p["hdr"] for p in products if p["hdr"].is_file()] + \
             [p["extent"] for p in products if p["extent"].is_file()]
    flight_lines = (run_dir / "T1_proc" / "QC_data" / "QC01_FlightCheck"
                    / "flight_lines.csv")
    if flight_lines.is_file():
        inputs.append(flight_lines)
    if spec_snapshot is not None:
        inputs.append(pathlib.Path(spec_snapshot["path"]))
    if not args.force and cf.outputs_up_to_date(
            [summary_path, detail_path], inputs):
        version_ok, version_reason = qr.report_is_current(
            qc_data, "QC03_RasterCheck", __version__)
        if version_ok:
            row.update({"status": "cached", "reason": "outputs up to date"})
            return row
        if args.verbose:
            tqdm.write(f"{run_dir}: {version_reason}; re-running")

    # ========== Scan each product + assemble the contract report ==========
    run_meta = {key: parsed.get(key)
                for key in ("node", "project", "site", "sensor", "run")}
    run_meta["date"] = parsed.get("date")
    report = qr.new_report("QC03_RasterCheck", __version__, run=run_meta)
    report["products"] = {}
    for product in products:
        scan = scan_product(product, sensor_platform=str(parsed.get("sensor")),
                            vmax=int(spec.get("reflectance_max", 10000)),
                            line_spacing_m=line_spacing_m,
                            spacing_source=spacing_source,
                            zones_spec=zones_spec,
                            chunk_mb=args.chunk_mb, threads=args.threads,
                            verbose=args.verbose)
        add_product_checks(report, product["label"], scan, spec)
        report["products"][product["label"]] = scan

    report["config"] = spec_snapshot or {"path": None, "sha256": None}
    report["staleness"] = {
        p["label"]: {"path": str(p["bin"]),
                     "mtime_utc": pd.Timestamp(
                         p["bin"].stat().st_mtime, unit="s",
                         tz="UTC").isoformat()}
        for p in products}
    qr.write_report(qc_data, report)
    qr.update_qc_report(qc_data, report)
    row.update({"status": report["status"], "reason": None})
    if args.verbose:
        tqdm.write(f"{run_dir}: {report['status']}")
    return row


# ==================================================================================
def find_ortho_products(run_dir: pathlib.Path) -> List[Dict[str, Any]]:
    """Find the VNIR/SWIR reflectance orthomosaics of one run.

    Parameters
    ----------
    run_dir : pathlib.Path
        The ``<run>`` directory.

    Returns
    -------
    list of dict
        One entry per ``*_{VNIR|SWIR}_Orthomosaic*.bin`` under any
        ``T1_proc/*.gpro/products/``: ``{"bin", "hdr", "region",
        "label", "gpro", "extent"}``. Labels are the lower-case region
        plus a ``_{suffix}`` when a run ships split orthos, plus a
        ``_gproN`` suffix when the run holds several bundles (keeps
        report keys unique); ``extent`` is the bundle's
        ``extents/hyper_extent.geojson`` (may not exist).
    """
    products: List[Dict[str, Any]] = []
    gpros = sorted((run_dir / "T1_proc").glob("*.gpro"))
    for gi, gpro in enumerate(gpros, start=1):
        for region in ("VNIR", "SWIR"):
            bins = sorted((gpro / "products").glob(
                f"*_{region}_Orthomosaic*.bin"))
            for b in bins:
                m = re.search(rf"_{region}_Orthomosaic(?:_(\w+))?\.bin$",
                              b.name)
                suffix = m.group(1) if m and m.group(1) else None
                label = region.lower() + (f"_{suffix}" if suffix else "")
                if len(gpros) > 1:
                    label += f"_gpro{gi}"   # keep multi-bundle keys unique
                products.append({"bin": b, "hdr": b.with_suffix(".hdr"),
                                 "region": region, "label": label,
                                 "gpro": gpro.name,
                                 "extent": (gpro / "extents"
                                            / "hyper_extent.geojson")})
    return products


# ==================================================================================
def qc01_line_spacing(
    run_dir: pathlib.Path,
) -> Tuple[Optional[float], str]:
    """Median flight-line spacing for a run, from QC01's line table.

    The swath margin that separates the expected edge band from the ROI
    is at least one line spacing (GOBI fieldbook step 2: the capture
    polygon is buffered perpendicular to the flight lines by >= one line
    spacing). QC01 already measures it per line.

    Parameters
    ----------
    run_dir : pathlib.Path
        The ``<run>`` directory.

    Returns
    -------
    tuple
        ``(metres, source)`` — the median ``line_spacing_m`` with source
        ``"qc01"``, or ``(None, "10%-rule")`` when QC01 has not run (the
        caller then falls back to the short-axis rule alone).
    """
    table = (run_dir / "T1_proc" / "QC_data" / "QC01_FlightCheck"
             / "flight_lines.csv")
    if not table.is_file():
        return None, "10%-rule"
    try:
        col = pd.read_csv(table, usecols=["line_spacing_m"])["line_spacing_m"]
    except (OSError, ValueError, pd.errors.ParserError):
        warn.warn(f"Unreadable {table} - falling back to the 10% rule.")
        return None, "10%-rule"
    col = pd.to_numeric(col, errors="coerce").dropna()
    col = col[col > 0]
    if col.empty:
        return None, "10%-rule"
    return float(col.median()), "qc01"


# ==================================================================================
def load_capture_extent(
    extent_path: pathlib.Path,
) -> Optional[Dict[str, Any]]:
    """Load the flown-area capture polygon shipped with a gpro bundle.

    ``<gpro>/extents/hyper_extent.geojson`` is a CRS84 (lon/lat)
    ``FeatureCollection`` whose first feature is the capture polygon.
    Rings are 4-vertex in the common case but 6-9 vertex variants exist,
    so nothing is hard-coded to four. Only convex rings are usable — the
    zone split is a half-plane test — so anything else is rejected and
    the caller falls back to the connectivity classifier.

    Parameters
    ----------
    extent_path : pathlib.Path
        Candidate ``extents/hyper_extent.geojson``.

    Returns
    -------
    dict or None
        ``{"path", "ring", "n_verts"}`` with the closing vertex and any
        repeated vertices dropped, or None when the file is missing,
        unreadable or unusable.
    """
    if not extent_path.is_file():
        return None
    try:
        geojson = json.loads(extent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        warn.warn(f"Unreadable capture extent {extent_path}.")
        return None
    features = geojson.get("features") or []
    geometry = (features[0].get("geometry") or {}) if features else {}
    if geometry.get("type") != "Polygon" or not geometry.get("coordinates"):
        warn.warn(f"Capture extent {extent_path} is not a Polygon.")
        return None
    ring = [(float(pt[0]), float(pt[1])) for pt in geometry["coordinates"][0]]
    # drop repeated vertices: zero-length edges break the half-plane test
    deduped: List[Tuple[float, float]] = []
    for pt in ring:
        if not deduped or pt != deduped[-1]:
            deduped.append(pt)
    while len(deduped) > 1 and deduped[-1] == deduped[0]:
        deduped.pop()
    ring = deduped
    if len(ring) < 3 or not _is_convex(np.asarray(ring, dtype=float)):
        warn.warn(f"Capture extent {extent_path} is not a convex ring "
                  f"({len(ring)} vertices) - using the fallback classifier.")
        return None
    return {"path": str(extent_path), "ring": ring, "n_verts": len(ring)}


# ==================================================================================
def _is_convex(verts: np.ndarray) -> bool:
    """Return True when a vertex ring is convex (winding-agnostic).

    numpy 2.x dropped the 2-D ``np.cross``, so the z-component of the
    consecutive-edge cross product is formed manually. Collinear
    vertices (zero cross product) are tolerated.

    Parameters
    ----------
    verts : np.ndarray
        ``(n, 2)`` ring vertices, closing vertex dropped.

    Returns
    -------
    bool
        True when every turn has the same sign.
    """
    edge = np.roll(verts, -1, axis=0) - verts
    nxt = np.roll(edge, -1, axis=0)
    turn = edge[:, 0] * nxt[:, 1] - edge[:, 1] * nxt[:, 0]
    turn = turn[np.abs(turn) > 1e-12]
    return bool(turn.size and (np.all(turn > 0) or np.all(turn < 0)))


# ==================================================================================
def extent_verts_px(
    ring: List[Tuple[float, float]],
    src: rasterio.DatasetReader,
) -> Tuple[np.ndarray, np.ndarray]:
    """Project a CRS84 ring into the raster's CRS and its pixel grid.

    ``rasterio.warp.transform`` is used rather than ``pyproj`` — it takes
    x/y sequences so the axis order is explicit (lon, lat) and it reuses
    the PROJ data GDAL is already bound to.

    Parameters
    ----------
    ring : list of tuple of float
        Ring vertices as ``(lon, lat)``, closing vertex dropped.
    src : rasterio.DatasetReader
        The open orthomosaic (supplies ``crs`` and ``transform``).

    Returns
    -------
    tuple of np.ndarray
        ``(verts_m, verts_px)`` — ring vertices in the raster CRS
        (metres) and in pixel coordinates ``(column, row)``.
    """
    east, north = rio_transform(CRS.from_epsg(4326), src.crs,
                               [pt[0] for pt in ring],
                               [pt[1] for pt in ring])
    inverse = ~src.transform
    return (np.column_stack([east, north]),
            np.array([inverse * (x, y) for x, y in zip(east, north)],
                     dtype=float))


# ==================================================================================
def inside_mask_px(
    verts: np.ndarray,
    hh: int,
    ww: int,
    inset: float = 0.0,
    rows_per_block: int = 2048,
) -> np.ndarray:
    """Point-in-convex-polygon test over a whole pixel grid.

    Half-planes only — no rasterisation — so the same test yields the
    bbox (``inset=0``), the eroded ROI (``inset>0``) and the tolerance
    band used by the ``data_outside_bbox`` guard (``inset=-0.5``).
    Pixel centres are tested. Winding-agnostic and N-vertex; rows are
    processed in blocks so the intermediate distance array stays small
    on 100+ Mpx grids.

    Parameters
    ----------
    verts : np.ndarray
        ``(n, 2)`` ring vertices in pixel space ``(column, row)``,
        closing vertex dropped.
    hh, ww : int
        Grid height and width in pixels.
    inset : float, optional
        Inward offset in pixels (negative widens the polygon).
        Default 0.
    rows_per_block : int, optional
        Rows evaluated per block. Default 2048.

    Returns
    -------
    np.ndarray
        Boolean ``(hh, ww)`` mask, True inside the (inset) polygon.
    """
    x, y = verts[:, 0], verts[:, 1]
    sgn = np.sign(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))
    edges = list(zip(verts, np.roll(verts, -1, axis=0)))
    px = np.arange(ww, dtype=np.float32) + 0.5   # pixel centres
    inside = np.empty((hh, ww), dtype=bool)
    for row0 in range(0, hh, rows_per_block):
        row1 = min(row0 + rows_per_block, hh)
        py = np.arange(row0, row1, dtype=np.float32) + 0.5
        block = np.ones((row1 - row0, ww), dtype=bool)
        for (x0, y0), (x1, y1) in edges:
            el = np.hypot(x1 - x0, y1 - y0)
            block &= (sgn * ((x1 - x0) * (py[:, None] - y0)
                             - (y1 - y0) * (px[None, :] - x0)) / el) >= inset
        inside[row0:row1] = block
    return inside


# ==================================================================================
def _min_rect_short_axis(verts_m: np.ndarray) -> float:
    """Short side of a convex ring's minimum-area rotated rectangle.

    A mean-of-shortest-edges heuristic breaks on valid convex rings with
    split (collinear) sides, so rotating calipers is used instead: for a
    convex polygon the minimum-area enclosing rectangle shares a
    direction with one of the edges.

    Parameters
    ----------
    verts_m : np.ndarray
        ``(n, 2)`` ring vertices in metres, closing vertex dropped.

    Returns
    -------
    float
        Short side of the minimum rotated rectangle, in metres.
    """
    edges = np.roll(verts_m, -1, axis=0) - verts_m
    lengths = np.hypot(edges[:, 0], edges[:, 1])
    best_area = np.inf
    short = 0.0
    for (ex, ey), el in zip(edges, lengths):
        if el < 1e-9:
            continue
        u = np.array([ex, ey]) / el
        v = np.array([-u[1], u[0]])
        du = float(np.ptp(verts_m @ u))
        dv = float(np.ptp(verts_m @ v))
        if du * dv < best_area:
            best_area = du * dv
            short = min(du, dv)
    return float(short)


# ==================================================================================
def zone_inset(
    short_axis_m: float,
    line_spacing_m: Optional[float],
    spacing_source: str,
    zones_spec: Dict[str, Any],
) -> Dict[str, Any]:
    """Per-edge erosion separating the expected edge band from the ROI.

    The fieldbooks put the effective capture area ~10 % inside the
    survey polygon per edge (``Standard-Flight.md``) and buffer the
    capture polygon by at least one line spacing perpendicular to the
    lines (GOBI fieldbook step 2), so the swath margin is the larger of
    the two. Only ``inset_factor`` of it is eroded (operator decision
    2026-08-26: the full margin absorbed real artefact into the advisory
    edge band).

    Parameters
    ----------
    short_axis_m : float
        Short side of the capture polygon's minimum rotated rectangle,
        in metres.
    line_spacing_m : float or None
        Median flight-line spacing, or None when QC01 has not run.
    spacing_source : str
        ``"qc01"`` or ``"10%-rule"``.
    zones_spec : dict
        ``zero_zones`` block of the validity spec
        (``short_axis_fraction``, ``inset_factor``).

    Returns
    -------
    dict
        Inset in metres plus the full provenance of how it was derived.
    """
    fraction = float(zones_spec.get("short_axis_fraction", 0.10))
    factor = float(zones_spec.get("inset_factor", 0.5))
    margin_m = fraction * short_axis_m
    if line_spacing_m is not None:
        margin_m = max(margin_m, float(line_spacing_m))
    return {
        "metres": float(factor * margin_m),
        "swath_margin_m": float(margin_m),
        "short_axis_m": float(short_axis_m),
        "short_axis_fraction": fraction,
        "inset_factor": factor,
        "line_spacing_m": (None if line_spacing_m is None
                           else float(line_spacing_m)),
        "line_spacing_source": spacing_source,
    }


# ==================================================================================
def _interior_zero_mask(all_zero: np.ndarray) -> np.ndarray:
    """Keep only the all-zero components that do not touch the border.

    Border-connected all-zero components are out-of-capture background;
    interior components are data holes. This is the discriminator the
    fallback classifier grades on, and the evidence split reported
    alongside the bbox ROI metric.

    Parameters
    ----------
    all_zero : np.ndarray
        Boolean ``(height, width)`` all-bands-zero mask.

    Returns
    -------
    np.ndarray
        Boolean mask of interior-connected all-zero pixels.
    """
    labels, n_comp = ndimage.label(all_zero)
    if n_comp == 0:
        return np.zeros_like(all_zero)
    border = np.unique(np.concatenate([labels[0], labels[-1],
                                       labels[:, 0], labels[:, -1]]))
    interior = np.ones(n_comp + 1, dtype=bool)
    interior[0] = False
    interior[border[border > 0]] = False
    return interior[labels]


# ==================================================================================
def classify_zero_zones(
    bin_path: pathlib.Path,
    extent_path: pathlib.Path,
    all_zero: np.ndarray,
    line_spacing_m: Optional[float],
    spacing_source: str,
    zones_spec: Dict[str, Any],
) -> Dict[str, Any]:
    """Split the all-bands-zero pixels of one product into zones.

    With a usable capture polygon the bbox is the analysis domain:
    everything outside it is discarded (background by construction —
    only the ``data_outside_bbox`` guard looks there), the bbox eroded by
    :func:`zone_inset` is the graded ROI, and the ring between them is
    the advisory edge band. Without one, border connectivity stands in:
    border-connected all-zero components are out-of-capture, interior
    components are dropout.

    Parameters
    ----------
    bin_path : pathlib.Path
        The orthomosaic (reopened for its CRS/transform/resolution).
    extent_path : pathlib.Path
        The bundle's ``extents/hyper_extent.geojson``.
    all_zero : np.ndarray
        Boolean ``(height, width)`` all-bands-zero mask.
    line_spacing_m : float or None
        Median flight-line spacing from QC01.
    spacing_source : str
        ``"qc01"`` or ``"10%-rule"``.
    zones_spec : dict
        ``zero_zones`` block of the validity spec.

    Returns
    -------
    dict
        Zone pixel counts, the two reported percentages, the
        ``data_outside_bbox`` guard and the inset/extent/classifier
        provenance. Bbox-only fields are None under the fallback.
    """
    height, width = all_zero.shape
    grid_px = int(all_zero.size)
    zones: Dict[str, Any] = {
        "classifier": "connectivity-fallback",
        "extent": {"path": None, "n_verts": None},
        "inset": None,
        "grid_px": grid_px,
        "all_zero_px": int(np.count_nonzero(all_zero)),
        "outside_bbox_px": None,
        "bbox_px": None,
        "edge_band_px": None,
        "zero_edge_band_px": None,
        "zero_edge_band_pct": None,
        "data_outside_bbox_px": None,
        "data_outside_bbox_pct": None,
    }
    interior = _interior_zero_mask(all_zero)
    extent = load_capture_extent(extent_path)

    if extent is None:
        # ===== no usable polygon: border connectivity decides the ROI =====
        dropout_px = int(np.count_nonzero(interior))
        roi_px = grid_px - (zones["all_zero_px"] - dropout_px)
        zones.update({
            "roi_px": roi_px,
            "dropout_roi_px": dropout_px,
            "dropout_in_roi_pct": float(100.0 * dropout_px / max(roi_px, 1)),
            "interior_cc_roi_px": dropout_px,
            "interior_cc_roi_share_pct": 100.0 if dropout_px else 0.0,
        })
        return zones

    with rasterio.open(bin_path) as src:
        verts_m, verts_px = extent_verts_px(extent["ring"], src)
        pixel_size_m = float(np.mean(np.abs(src.res)))
    inset = zone_inset(_min_rect_short_axis(verts_m), line_spacing_m,
                       spacing_source, zones_spec)
    inset["pixel_size_m"] = pixel_size_m
    inset["pixels"] = float(inset["metres"] / max(pixel_size_m, 1e-12))

    # ===== bbox: the analysis domain, plus the extent sanity guard =====
    bbox = inside_mask_px(verts_px, height, width)
    bbox_px = int(np.count_nonzero(bbox))
    zero_in_bbox_px = int(np.count_nonzero(all_zero & bbox))
    del bbox
    tolerated = inside_mask_px(verts_px, height, width, inset=-0.5)
    outside_data_px = int(np.count_nonzero(~tolerated & ~all_zero))
    del tolerated

    # ===== ROI: the graded zone; the edge band is the rest of the bbox =====
    roi = inside_mask_px(verts_px, height, width, inset=inset["pixels"])
    roi_px = int(np.count_nonzero(roi))
    dropout_px = int(np.count_nonzero(all_zero & roi))
    interior_roi_px = int(np.count_nonzero(interior & roi))
    edge_band_px = bbox_px - roi_px
    zero_edge_band_px = zero_in_bbox_px - dropout_px

    zones.update({
        "classifier": "bbox",
        "extent": {"path": extent["path"], "n_verts": extent["n_verts"]},
        "inset": inset,
        "outside_bbox_px": grid_px - bbox_px,
        "bbox_px": bbox_px,
        "zero_in_bbox_px": zero_in_bbox_px,
        "edge_band_px": edge_band_px,
        "zero_edge_band_px": zero_edge_band_px,
        "zero_edge_band_pct": float(
            100.0 * zero_edge_band_px / max(edge_band_px, 1)),
        "roi_px": roi_px,
        "dropout_roi_px": dropout_px,
        "dropout_in_roi_pct": float(100.0 * dropout_px / max(roi_px, 1)),
        "interior_cc_roi_px": interior_roi_px,
        "interior_cc_roi_share_pct": float(
            100.0 * interior_roi_px / max(dropout_px, 1)),
        "data_outside_bbox_px": outside_data_px,
        "data_outside_bbox_pct": float(
            100.0 * outside_data_px / max(grid_px, 1)),
    })
    return zones


# ==================================================================================
def scan_product(
    product: Dict[str, Any],
    sensor_platform: str,
    vmax: int,
    line_spacing_m: Optional[float] = None,
    spacing_source: str = "10%-rule",
    zones_spec: Optional[Dict[str, Any]] = None,
    chunk_mb: int = 256,
    threads: int = 4,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Scan one reflectance orthomosaic for data-validity statistics.

    Uncompressed little-endian uint16 BSQ products (the GRYFN standard)
    take the fast path: each band is one contiguous ``np.fromfile``
    read (``threads`` concurrent readers) and a single raw
    ``bincount(65536)`` yields zeros/over-range/min/max/mean plus the
    exact percentile histogram; the footprint follows algebraically
    from a per-pixel zero-count accumulator (every background pixel is
    zero in every band, so ``n_zero_fp = n_zero_total -
    background_px``). Anything else falls back to the original GDAL
    row-window chunk scan. Both paths produce identical statistics and
    both return the full-grid all-bands-zero mask, which
    :func:`classify_zero_zones` then splits into the reported zones.

    Parameters
    ----------
    product : dict
        Entry from :func:`find_ortho_products`.
    sensor_platform : str
        Sensor platform folder name (``CALVIS``/``GOBI``) for the
        bad-band lookup.
    vmax : int
        Physical reflectance ceiling (10000 for x 10^4 products).
    line_spacing_m : float or None, optional
        Median flight-line spacing from QC01, for the ROI inset.
    spacing_source : str, optional
        ``"qc01"`` or ``"10%-rule"``. Default ``"10%-rule"``.
    zones_spec : dict, optional
        ``zero_zones`` block of the validity spec.
    chunk_mb : int, optional
        Target memory per read chunk in MB (GDAL fallback only).
        Default 256.
    threads : int, optional
        Reader threads for the fast path. Default 4.
    verbose : bool, optional
        Print per-product progress. Default False.

    Returns
    -------
    dict
        ``header_bin_integrity`` block, whole-cube roll-ups (bad bands
        excluded), the ``zero_zones`` split of the all-bands-zero
        pixels, and a ``bands`` list of per-band statistics dicts. When
        the raster cannot be opened (missing/corrupt ``.hdr``) a
        minimal dict with a failed integrity block and ``cube=None`` is
        returned instead.
    """
    bin_path: pathlib.Path = product["bin"]
    integrity = check_header_bin_integrity(bin_path, product["hdr"])
    bad_ranges = sq.default_bad_wavelengths().get(
        sensor_platform, {}).get(product["region"], [])

    try:
        wavelengths, _ = cf.band_wavelengths(bin_path)
        with rasterio.open(bin_path) as src:
            n_bands, height, width = src.count, src.height, src.width
            dtype = np.dtype(src.dtypes[0])
        is_float = np.issubdtype(dtype, np.floating)
        is_signed = is_float or np.issubdtype(dtype, np.signedinteger)

        offset = _raw_bsq_offset(product["hdr"], integrity, dtype)
        if offset is not None:
            acc = _scan_bands_raw(bin_path, n_bands, height, width, offset,
                                  vmax, threads, verbose)
        else:
            acc = _scan_bands_gdal(bin_path, vmax, chunk_mb, is_float,
                                   is_signed, verbose)
    except RasterioIOError as err:
        # a broken/missing .hdr must fail the check, not abort the crawl
        integrity["ok"] = False
        integrity["issues"].append(f"raster unreadable: {err}")
        warn.warn(f"{bin_path}: raster unreadable ({err}) - recording a "
                  "failed integrity check and skipping the scan.")
        return {"file": bin_path.name, "header_bin_integrity": integrity,
                "shape": None, "footprint_px": None,
                "footprint_fraction": None, "zero_zones": None,
                "n_bands_bad_masked": None, "cube": None,
                "constant_bands": None, "bands": []}

    n_px = height * width
    footprint_px = acc["footprint_px"]
    zones = classify_zero_zones(
        bin_path, product["extent"], acc.pop("all_zero"), line_spacing_m,
        spacing_source, zones_spec or {})
    bands = _per_band_stats(
        n_bands, wavelengths, bad_ranges, footprint_px, acc["n_zero_fp"],
        acc["n_over"], acc["n_neg"], acc["n_naninf"], acc["n_valid"],
        acc["band_min"], acc["band_max"], acc["band_sum"], acc["hist"],
        n_px)
    good = [b for b in bands if not b["bad_band"]]
    denom_fp = max(footprint_px * max(len(good), 1), 1)
    denom_all = max(n_px * max(len(good), 1), 1)
    worst_over = max(good, key=lambda b: b["over_range_pct"], default=None)
    return {
        "file": bin_path.name,
        "header_bin_integrity": integrity,
        "shape": {"bands": n_bands, "height": height, "width": width,
                  "dtype": str(dtype)},
        "footprint_px": int(footprint_px),
        "footprint_fraction": float(footprint_px / max(n_px, 1)),
        "zero_zones": zones,
        "n_bands_bad_masked": int(len(bands) - len(good)),
        "cube": {  # whole-cube roll-ups, bad bands excluded (§5c)
            "zeros_in_footprint_pct": float(
                100.0 * sum(b["n_zero_in_footprint"] for b in good)
                / denom_fp),
            "over_range_pct": float(
                100.0 * sum(b["n_over_range"] for b in good) / denom_all),
            "negative_pct": float(
                100.0 * sum(b["n_negative"] for b in good) / denom_all),
            "nan_inf_count": int(sum(b["n_nan_inf"] for b in good)),
            "max_value": float(max((b["max"] for b in good),
                                   default=np.nan)),
            "worst_over_range_band": (
                None if worst_over is None
                or worst_over["over_range_pct"] == 0 else
                {"band": worst_over["band"],
                 "wavelength_nm": worst_over["wavelength_nm"],
                 "over_range_pct": worst_over["over_range_pct"]}),
            "signed_dtype": bool(is_signed),
            "float_dtype": bool(is_float),
        },
        "constant_bands": [b["band"] for b in bands
                           if b["min"] == b["max"]],
        "bands": bands,
    }


# ==================================================================================
def _raw_bsq_offset(
    hdr_path: pathlib.Path,
    integrity: Dict[str, Any],
    dtype: np.dtype,
) -> Optional[int]:
    """Return the data offset when the fast raw-read path is safe.

    The raw path bypasses GDAL, so it is only taken when the ``.hdr``
    declares uncompressed little-endian uint16 BSQ *and* the integrity
    check confirmed the ``.bin`` size matches the declared geometry
    (otherwise band offsets would be wrong).

    Parameters
    ----------
    hdr_path : pathlib.Path
        ENVI ``.hdr`` sidecar.
    integrity : dict
        Output of :func:`check_header_bin_integrity`.
    dtype : np.dtype
        Raster dtype reported by rasterio.

    Returns
    -------
    int or None
        Header offset in bytes, or None when the GDAL fallback must be
        used.
    """
    if not integrity["ok"] or dtype != np.uint16:
        return None
    text = hdr_path.read_text(encoding="utf-8", errors="replace")

    def _field(name: str) -> Optional[str]:
        m = re.search(rf"^\s*{name}\s*=\s*(\S+)", text,
                      re.IGNORECASE | re.MULTILINE)
        return m.group(1).lower() if m else None

    if _field("interleave") != "bsq" or _field("byte order") not in ("0",
                                                                     None):
        return None
    if _field("file compression") not in ("0", None):
        return None
    return int(integrity["dims"].get("header offset") or 0)


# ==================================================================================
def _scan_bands_raw(
    bin_path: pathlib.Path,
    n_bands: int,
    height: int,
    width: int,
    offset: int,
    vmax: int,
    threads: int,
    verbose: bool,
) -> Dict[str, Any]:
    """Threaded raw band-major scan of an uncompressed uint16 BSQ.

    Each worker owns a file handle and a private per-pixel zero-count
    partial; per-band outputs are written to disjoint slots, so no
    locking is needed and the merged result is order-independent.

    Parameters
    ----------
    bin_path : pathlib.Path
        ENVI ``.bin`` file.
    n_bands, height, width : int
        Raster geometry.
    offset : int
        Header offset in bytes.
    vmax : int
        Physical reflectance ceiling.
    threads : int
        Reader-thread count.
    verbose : bool
        Show a per-band progress bar.

    Returns
    -------
    dict
        Accumulators: ``footprint_px``, the full-grid ``all_zero`` mask,
        per-band ``n_zero_fp``, ``n_over``, ``n_neg``, ``n_naninf``,
        ``band_min``, ``band_max``, ``band_sum`` and the clipped ``hist``
        ``(n_bands, vmax + 1)``.
    """
    band_px = height * width
    n_zero = np.zeros(n_bands, dtype=np.int64)
    n_over = np.zeros(n_bands, dtype=np.int64)
    band_min = np.full(n_bands, np.inf)
    band_max = np.full(n_bands, -np.inf)
    band_sum = np.zeros(n_bands, dtype=np.float64)
    hist = np.zeros((n_bands, vmax + 1), dtype=np.int64)
    values = np.arange(65536, dtype=np.int64)
    zero_parts: List[Optional[np.ndarray]] = [None] * threads
    pbar = tqdm(total=n_bands, desc=f"  {bin_path.name}", unit="band",
                leave=False, disable=not verbose)

    def _worker(w: int) -> None:
        local_zero = np.zeros(band_px, dtype=np.uint16)
        with open(bin_path, "rb") as fh:
            for b in range(w, n_bands, threads):
                fh.seek(offset + b * band_px * 2)
                band = np.fromfile(fh, dtype="<u2", count=band_px)
                h = np.bincount(band, minlength=65536)
                n_zero[b] = int(h[0])
                n_over[b] = int(h[vmax + 1:].sum())
                nz = np.nonzero(h)[0]
                band_min[b] = float(nz[0])
                band_max[b] = float(nz[-1])
                band_sum[b] = float(np.dot(h, values))
                # fold over-range counts into the top bin (== old clip)
                hist[b, :vmax] = h[:vmax]
                hist[b, vmax] = int(h[vmax:].sum())
                local_zero += band == 0
                pbar.update(1)
        zero_parts[w] = local_zero

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(_worker, range(threads)))
    pbar.close()

    # merge the per-thread partials in place (max value is n_bands, so
    # uint16 cannot overflow) and keep the full grid: the zone split needs
    # nonzero knowledge *outside* the capture polygon, and ~100 MB is
    # irrelevant next to the tens of GB just read.
    zero_count = zero_parts[0]
    for w in range(1, threads):
        zero_count += zero_parts[w]
        zero_parts[w] = None
    all_zero = (zero_count == n_bands).reshape(height, width)
    del zero_count, zero_parts
    background_px = int(np.count_nonzero(all_zero))
    return {
        "footprint_px": band_px - background_px,
        "all_zero": all_zero,
        "n_zero_fp": n_zero - background_px,
        "n_over": n_over,
        "n_neg": np.zeros(n_bands, dtype=np.int64),
        "n_naninf": np.zeros(n_bands, dtype=np.int64),
        "n_valid": np.full(n_bands, band_px, dtype=np.int64),
        "band_min": band_min,
        "band_max": band_max,
        "band_sum": band_sum,
        "hist": hist,
    }


# ==================================================================================
def _scan_bands_gdal(
    bin_path: pathlib.Path,
    vmax: int,
    chunk_mb: int,
    is_float: bool,
    is_signed: bool,
    verbose: bool,
) -> Dict[str, Any]:
    """Original GDAL row-window chunk scan (non-uint16/BSQ fallback).

    Non-finite samples (NaN/Inf) are excluded from every ordinary
    statistic — background, zeros, extrema, sums, histogram — and only
    counted in ``n_naninf``.

    Parameters
    ----------
    bin_path : pathlib.Path
        ENVI ``.bin`` file.
    vmax : int
        Physical reflectance ceiling.
    chunk_mb : int
        Target memory per read chunk in MB.
    is_float, is_signed : bool
        Dtype traits (enable NaN/negative counting).
    verbose : bool
        Show a per-chunk progress bar.

    Returns
    -------
    dict
        Same accumulator set as :func:`_scan_bands_raw`, including the
        full-grid ``all_zero`` mask.
    """
    with rasterio.open(bin_path) as src:
        n_bands, height, width = src.count, src.height, src.width
        dtype = np.dtype(src.dtypes[0])
        rows_per_chunk = max(1, int(chunk_mb * 1e6
                                    / (n_bands * width * dtype.itemsize)))
        n_zero_fp = np.zeros(n_bands, dtype=np.int64)
        n_over = np.zeros(n_bands, dtype=np.int64)
        n_neg = np.zeros(n_bands, dtype=np.int64)
        n_naninf = np.zeros(n_bands, dtype=np.int64)
        n_valid = np.zeros(n_bands, dtype=np.int64)
        band_min = np.full(n_bands, np.inf)
        band_max = np.full(n_bands, -np.inf)
        band_sum = np.zeros(n_bands, dtype=np.float64)
        hist = np.zeros((n_bands, vmax + 1), dtype=np.int64)
        all_zero = np.zeros((height, width), dtype=bool)
        footprint_px = 0

        offsets = range(0, height, rows_per_chunk)
        iterator = tqdm(offsets, desc=f"  {bin_path.name}", unit="chunk",
                        leave=False) if verbose else offsets
        for row_off in iterator:
            h = min(rows_per_chunk, height - row_off)
            window = rasterio.windows.Window(0, row_off, width, h)
            data = src.read(window=window)          # (bands, h, w)
            finite = np.isfinite(data) if is_float else None
            if is_float:
                n_naninf += (~finite).reshape(n_bands, -1).sum(axis=1)
            # NaN/Inf compare unequal to 0, so they never count as background
            background = (data == 0).all(axis=0)    # all-bands-zero
            all_zero[row_off:row_off + h] = background
            fp = ~background
            footprint_px += int(fp.sum())
            flat_fp = fp.ravel()
            for b in range(n_bands):
                vals = data[b].ravel()
                fp_b = flat_fp
                if is_float:
                    fin = finite[b].ravel()
                    vals = vals[fin]        # finite samples only
                    fp_b = flat_fp[fin]
                n_zero_fp[b] += int(((vals == 0) & fp_b).sum())
                n_over[b] += int((vals > vmax).sum())
                if is_signed:
                    n_neg[b] += int((vals < 0).sum())
                n_valid[b] += vals.size
                if vals.size:
                    band_min[b] = min(band_min[b], float(vals.min()))
                    band_max[b] = max(band_max[b], float(vals.max()))
                    band_sum[b] += float(vals.sum())
                    clipped = np.clip(vals, 0, vmax).astype(np.int64)
                    hist[b] += np.bincount(clipped, minlength=vmax + 1)

    return {
        "footprint_px": footprint_px,
        "all_zero": all_zero,
        "n_zero_fp": n_zero_fp,
        "n_over": n_over,
        "n_neg": n_neg,
        "n_naninf": n_naninf,
        "n_valid": n_valid,
        "band_min": band_min,
        "band_max": band_max,
        "band_sum": band_sum,
        "hist": hist,
    }


# ==================================================================================
def _per_band_stats(
    n_bands: int,
    wavelengths: Dict[int, float],
    bad_ranges: List[Tuple[float, float]],
    footprint_px: int,
    n_zero_fp: np.ndarray,
    n_over: np.ndarray,
    n_neg: np.ndarray,
    n_naninf: np.ndarray,
    n_valid: np.ndarray,
    band_min: np.ndarray,
    band_max: np.ndarray,
    band_sum: np.ndarray,
    hist: np.ndarray,
    n_px: int,
) -> List[Dict[str, Any]]:
    """Assemble the per-band statistics list from the scan accumulators.

    Percentiles are exact for in-range integer data (computed from the
    clipped histogram); out-of-range values are clipped to the ends.

    Parameters
    ----------
    n_bands : int
        Number of bands.
    wavelengths : dict of int to float
        1-based band index to centre wavelength (nm).
    bad_ranges : list of tuple of float
        Known-bad wavelength ranges for this sensor/region.
    footprint_px : int
        Total footprint pixels.
    n_zero_fp, n_over, n_neg, n_naninf : np.ndarray
        Per-band counters.
    n_valid : np.ndarray
        Per-band finite-sample counts (``n_px`` for integer data); the
        ``mean`` denominator.
    band_min, band_max, band_sum : np.ndarray
        Per-band extrema and sums (finite samples only).
    hist : np.ndarray
        Per-band clipped histograms ``(n_bands, vmax + 1)``.
    n_px : int
        Total pixels per band.

    Returns
    -------
    list of dict
        One statistics dict per band (1-based ``band`` index).
    """
    stats: List[Dict[str, Any]] = []
    pcts = (1, 5, 50, 95, 99)
    for b in range(n_bands):
        wl = wavelengths.get(b + 1, np.nan)
        bad = any(lo <= wl <= hi for lo, hi in bad_ranges) \
            if np.isfinite(wl) else False
        cum = np.cumsum(hist[b])
        total = int(cum[-1])
        pct_vals = {}
        for p in pcts:
            rank = max(int(np.ceil(p / 100.0 * total)), 1)
            pct_vals[f"p{p:02d}"] = int(np.searchsorted(cum, rank))
        stats.append({
            "band": b + 1,
            "wavelength_nm": None if not np.isfinite(wl) else float(wl),
            "bad_band": bool(bad),
            "min": float(band_min[b]),
            "max": float(band_max[b]),
            "mean": float(band_sum[b] / max(int(n_valid[b]), 1)),
            **pct_vals,
            "n_zero_in_footprint": int(n_zero_fp[b]),
            "zeros_in_footprint_pct": float(
                100.0 * n_zero_fp[b] / max(footprint_px, 1)),
            "n_over_range": int(n_over[b]),
            "over_range_pct": float(100.0 * n_over[b] / max(n_px, 1)),
            "n_negative": int(n_neg[b]),
            "n_nan_inf": int(n_naninf[b]),
        })
    return stats


# ==================================================================================
def check_header_bin_integrity(
    bin_path: pathlib.Path,
    hdr_path: pathlib.Path,
) -> Dict[str, Any]:
    """Check the ``.bin`` size and wavelength list against the ``.hdr``.

    Parameters
    ----------
    bin_path : pathlib.Path
        The ENVI ``.bin`` file.
    hdr_path : pathlib.Path
        Its ``.hdr`` sidecar.

    Returns
    -------
    dict
        ``ok`` (bool), ``issues`` (list of str), plus the parsed
        dimensions and expected/actual byte counts.
    """
    issues: List[str] = []
    result: Dict[str, Any] = {"ok": False, "issues": issues}
    if not hdr_path.is_file():
        issues.append("no .hdr sidecar")
        return result
    text = hdr_path.read_text(encoding="utf-8", errors="replace")

    def _int_field(name: str) -> Optional[int]:
        m = re.search(rf"^\s*{name}\s*=\s*(\d+)", text,
                      re.IGNORECASE | re.MULTILINE)
        return int(m.group(1)) if m else None

    dims = {name: _int_field(name)
            for name in ("samples", "lines", "bands", "data type",
                         "header offset")}
    missing = [k for k, v in dims.items() if v is None
               and k != "header offset"]
    if missing:
        issues.append(f"missing .hdr fields: {missing}")
        return result
    # ENVI data-type code -> bytes per element
    type_bytes = {1: 1, 2: 2, 3: 4, 4: 4, 5: 8, 6: 8, 9: 16,
                  12: 2, 13: 4, 14: 8, 15: 8}
    itemsize = type_bytes.get(dims["data type"] or 0)
    if itemsize is None:
        issues.append(f"unknown ENVI data type {dims['data type']}")
        return result
    expected = (dims["samples"] * dims["lines"] * dims["bands"] * itemsize
                + (dims["header offset"] or 0))
    actual = bin_path.stat().st_size
    result.update({"dims": dims, "expected_bytes": int(expected),
                   "actual_bytes": int(actual)})
    if actual != expected:
        issues.append(
            f"bin size {actual} != expected {expected} "
            "(lines x samples x bands x dtype)")

    m = re.search(r"wavelength\s*=\s*\{([^}]*)\}", text,
                  re.IGNORECASE | re.DOTALL)
    if m is None:
        issues.append("no wavelength list in .hdr")
    else:
        n_wl = len([v for v in m.group(1).split(",") if v.strip()])
        result["n_wavelengths"] = n_wl
        if n_wl != dims["bands"]:
            issues.append(
                f"wavelength list length {n_wl} != bands {dims['bands']}")
    result["ok"] = not issues
    return result


# ==================================================================================
def add_product_checks(
    report: Dict[str, Any],
    label: str,
    scan: Dict[str, Any],
    spec: Dict[str, Any],
) -> None:
    """Grade one product's scan against the advisory thresholds.

    Parameters
    ----------
    report : dict
        Contract report dict (mutated in place).
    label : str
        Product label (``vnir``, ``swir``, ``vnir_2``, ...).
    scan : dict
        Output of :func:`scan_product`.
    spec : dict
        Parsed validity thresholds.

    Returns
    -------
    None
    """
    integrity = scan["header_bin_integrity"]
    qr.add_check(
        report, f"header_bin_integrity_{label}",
        "good" if integrity["ok"] else "fail",
        value=scan["file"],
        note="; ".join(integrity["issues"]) or None)

    cube = scan["cube"]
    if cube is None:
        for name in ("zeros_in_footprint", "over_range", "negative",
                     "nan_inf", "dropout_in_roi"):
            qr.add_check(report, f"{name}_{label}", "not_checked",
                         note="raster unreadable - scan skipped")
        return
    zf = cube["zeros_in_footprint_pct"]
    qr.add_check(report, f"zeros_in_footprint_{label}",
                 _pct_status(zf, "zeros_in_footprint_pct", spec),
                 value=f"{zf:.3f} %")
    over = cube["over_range_pct"]
    worst = cube.get("worst_over_range_band")
    qr.add_check(report, f"over_range_{label}",
                 _pct_status(over, "over_range_pct", spec),
                 value=f"{over:.3f} % (max {cube['max_value']:.0f})",
                 note=(f"worst band {worst['band']} "
                       f"({worst['wavelength_nm']} nm): "
                       f"{worst['over_range_pct']:.3f} %"
                       if worst else None))
    if cube["signed_dtype"]:
        neg = cube["negative_pct"]
        qr.add_check(report, f"negative_{label}",
                     _pct_status(neg, "negative_pct", spec),
                     value=f"{neg:.3f} %")
    else:
        qr.add_check(report, f"negative_{label}", "not_checked",
                     note="unsigned dtype cannot hold negatives")
    if cube["float_dtype"]:
        n_ni = cube["nan_inf_count"]
        thr = spec.get("nan_inf_count", {})
        status = ("warning" if thr.get("warn_above") is not None
                  and n_ni > thr["warn_above"] else "good")
        qr.add_check(report, f"nan_inf_{label}", status, value=str(n_ni))
    else:
        qr.add_check(report, f"nan_inf_{label}", "not_checked",
                     note="integer dtype cannot hold NaN/Inf")

    _add_zone_checks(report, label, scan.get("zero_zones") or {}, spec)


# ==================================================================================
def _pct_status(value: float, key: str, spec: Dict[str, Any]) -> str:
    """Grade a percentage against the spec's warn/fail ceilings.

    Parameters
    ----------
    value : float
        The measured percentage.
    key : str
        Spec key holding ``warn_above`` / ``fail_above`` (either may be
        absent or null, in which case that level never fires).
    spec : dict
        Parsed validity thresholds.

    Returns
    -------
    str
        ``good``, ``warning`` or ``fail``.
    """
    thr = spec.get(key, {}) or {}
    if thr.get("fail_above") is not None and value > thr["fail_above"]:
        return "fail"
    if thr.get("warn_above") is not None and value > thr["warn_above"]:
        return "warning"
    return "good"


# ==================================================================================
def _add_zone_checks(
    report: Dict[str, Any],
    label: str,
    zones: Dict[str, Any],
    spec: Dict[str, Any],
) -> None:
    """Report the capture-polygon zone split of one product.

    Three checks plus the extent-presence finding: the ROI dropout is
    graded, the edge band is advisory (expected incomplete capture at
    the swath edges — a known fieldbook failure mode, never a defect)
    and ``data_outside_bbox`` guards the extent/raster pairing. The
    interior-connected share of the ROI dropout rides along as evidence
    so the composite rule can be calibrated later without a re-scan.

    Parameters
    ----------
    report : dict
        Contract report dict (mutated in place).
    label : str
        Product label (``vnir``, ``swir``, ``vnir_2``, ...).
    zones : dict
        Output of :func:`classify_zero_zones` (empty to skip).
    spec : dict
        Parsed validity thresholds.

    Returns
    -------
    None
    """
    if not zones:
        return
    inset = zones.get("inset") or {}
    if zones.get("classifier") == "bbox":
        qr.add_check(
            report, f"capture_extent_{label}", "good",
            value=f"{zones['extent']['n_verts']} vertices",
            note=(f"ROI inset {inset['metres']:.2f} m "
                  f"({inset['pixels']:.0f} px), swath margin from "
                  f"{inset['line_spacing_source']}"),
            extent_path=zones["extent"]["path"], inset=inset)
    else:
        qr.add_check(
            report, f"capture_extent_{label}", "warning",
            value="missing",
            note="no usable extents/hyper_extent.geojson - zones fall back "
                 "to the border-connectivity classifier")

    band_pct = zones.get("zero_edge_band_pct")
    qr.add_check(
        report, f"zero_edge_band_{label}", "not_checked",
        value=None if band_pct is None else f"{band_pct:.3f} %",
        note=(f"{zones['zero_edge_band_px']} of {zones['edge_band_px']} "
              "edge-band px - expected incomplete capture, never graded"
              if band_pct is not None else
              "no capture polygon - edge band not separable"),
        advisory=True,
        edge_band_px=zones.get("edge_band_px"),
        zero_edge_band_px=zones.get("zero_edge_band_px"))

    roi_pct = zones.get("dropout_in_roi_pct")
    if roi_pct is None:
        qr.add_check(report, f"dropout_in_roi_{label}", "not_checked",
                     note="no ROI could be established")
    else:
        qr.add_check(
            report, f"dropout_in_roi_{label}",
            _pct_status(roi_pct, "dropout_in_roi_pct", spec),
            value=f"{roi_pct:.3f} %",
            note=(f"{zones['dropout_roi_px']} of {zones['roi_px']} ROI px "
                  f"all-bands-zero, {zones['interior_cc_roi_share_pct']:.0f} "
                  f"% interior-connected ({zones['classifier']})"),
            classifier=zones.get("classifier"),
            dropout_roi_px=zones.get("dropout_roi_px"),
            roi_px=zones.get("roi_px"),
            interior_cc_roi_px=zones.get("interior_cc_roi_px"))

    out_pct = zones.get("data_outside_bbox_pct")
    if out_pct is None:
        qr.add_check(report, f"data_outside_bbox_{label}", "not_checked",
                     note="no capture polygon to test against")
    else:
        qr.add_check(
            report, f"data_outside_bbox_{label}",
            _pct_status(out_pct, "data_outside_bbox_pct", spec),
            value=f"{zones['data_outside_bbox_px']} px",
            note="nonzero pixels outside the capture polygon at 0.5 px "
                 "tolerance - extent/raster mismatch when material",
            data_outside_bbox_pct=out_pct,
            outside_bbox_px=zones.get("outside_bbox_px"))


# ==================================================================================
def _print_run_summary(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """Print the end-of-run REPORTED/SKIPPED summary and return it.

    Parameters
    ----------
    rows : list of dict
        Per-run summary rows from :func:`process_run`.

    Returns
    -------
    pd.DataFrame
        The summary table (one row per discovered run).
    """
    df = pd.DataFrame(rows)
    if df.empty:
        print("\nNo runs found.")
        return df
    cols = ["project", "site", "sensor", "date", "run", "status", "reason"]
    df = df[[c for c in cols if c in df.columns]]
    skipped = df[df["status"] == "skipped"]
    reported = df[df["status"] != "skipped"]
    if not reported.empty:
        print("\n===== REPORTED runs =====")
        print(reported.drop(columns=["reason"]).to_string(index=False))
    if not skipped.empty:
        print("\n===== SKIPPED runs =====")
        print(skipped.to_string(index=False))
    return df


# ==================================================================================
if __name__ == "__main__":
    # ========== chdir to git root (resolved at module top) ==========
    os.chdir(_git_root)

    # ========== Parse args ==========
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--path", type=str, default=None,
        help="Folder to crawl for processed runs (any APPN tree level). "
             "Defaults to the git repo root.")
    parser.add_argument(
        "--spec", type=str,
        default="reference/thresholds/raster_validity.yml",
        help="Raster-validity threshold YAML relative to the repo root.")
    parser.add_argument(
        "--chunk-mb", type=int, default=256,
        help="Target memory per read chunk in MB (default 256; GDAL "
             "fallback path only).")
    parser.add_argument(
        "--threads", type=int, default=4,
        help="Reader threads for the raw BSQ fast path (default 4).")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[],
                        help="Directory names to exclude from the crawl.")
    parser.add_argument("--allow-multi-gpro", default=False,
                        action="store_true",
                        help="Process runs that contain more than one .gpro "
                             "folder instead of skipping them (product "
                             "labels get a _gproN suffix). Debugging only: "
                             "the product set is ambiguous.")
    parser.add_argument("-f", "--force", default=False, action="store_true",
                        help="Regenerate reports even when they are newer "
                             "than every input.")
    parser.add_argument("-v", "--verbose", default=False,
                        action="store_true",
                        help="Enable verbose output.")
    args = parser.parse_args()
    cf.check_environment(_git_root)

    main(args)
