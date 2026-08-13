"""Compare validation point locations against QC GCP points.

Crawls the dataset directory tree for pairs of point files following
the APPN naming convention:

* a groundtruth file under ``.../<run>/T1_proc/QC_data/QC_GCP_groundtruth_points.geojson``
* one or more matching QC files at
  ``.../<run>/T1_proc/QC_data/QC_GCP_points.geojson`` (single layer,
  valid when every data product aligns) or
  ``QC_GCP_{Product}_points.geojson`` (one digitised layer per data
  product when they do not align, e.g. ``QC_GCP_VNIR_points.geojson``).
  Either form may carry optional trailing information after another
  underscore (e.g. ``QC_GCP_points_20260805.geojson``), which is
  carried through to the output names.

GeoJSON is the preferred format throughout; ``.shp`` files are only
accepted as a legacy fallback when no geojson sibling exists.

For each pair found, features are matched by an ID column and the
planar (and, where available, 3D) distance between matched points is
reported.

Output
------
For each matched pair, a table is written next to the QC file as
``QC_GCP_distances.<csv|parquet>`` (controlled by ``--type``), with
one row per matched ID:

``id, easting_a_m, northing_a_m, height_a_m, easting_b_m, northing_b_m,
height_b_m, delta_easting_m, delta_northing_m, distance_2d_m,
bearing_deg, distance_3d_m, delta_height_m``

A companion accuracy report is written alongside as
``QC_GCP_distances_report.json``. Per-product QC layers use the
product identifier in the stem instead (``QC_GCP_{Product}_distances``,
e.g. ``QC_GCP_VNIR_distances.csv``), and any extra-info suffix on the
QC file is appended (``QC_GCP_distances_{extraInfo}``). These names
are constant across runs so it is easy to check whether a flight has
already been processed.

The report includes a ``bias`` section that decomposes the signed
errors into a systematic component (mean offset) and a random
component (standard deviation), satisfying ``rmse² = bias² + std²``.
``bias_fraction = |bias| / rmse`` lies in [0, 1]: values near 0
indicate random scatter, values near 1 indicate a strongly biased
offset in a specific direction. The 2D summary also reports the
bias bearing (clockwise from grid north).

``bearing_deg`` is measured clockwise from grid north (0–360°)
and is NaN when the two points coincide.

All distance and coordinate columns are in metres regardless of the
input CRS. If the inputs are not in a metre-based projected CRS,
they are reprojected to an appropriate UTM zone (estimated from the
data extent) before distances are computed.

Summary statistics (count, mean, median, min, max, RMSE) of the 2D
and 3D distances are printed to stdout. Unmatched IDs from each file
are also listed.

Command-line Arguments
----------------------
--path PATH, optional
    Root directory to search for groundtruth/QC pairs. Defaults to the
    git repository root.
--id-column NAME [NAME ...]
    Candidate column name(s) used to match features. Each file is
    matched against the first candidate it contains. Defaults to
    ``ID GCP_name``.
--type {csv,parquet}
    Output table format. Defaults to ``csv``.
--exclude-dir NAME [NAME ...]
    Directory names to exclude from the search.
--plot
    After saving, also display the per-pair displacement plot
    interactively. Plots are always written to
    ``<QC_data>/QC_plots/QC_GCP_distances_displacements.png`` whenever
    the JSON report is (re)generated.
--verbose
    Print extra diagnostic information.
"""

# ==============================================================================

__title__ = "Point distance comparison"
__author__ = "Arden Burrell"
__version__ = "v1.1(13.08.2026)"
__email__ = "arden.burrell@sydney.edu.au"

# ==============================================================================

import os
import argparse
import json
import pathlib
import re
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import geopandas as gpd
import matplotlib.pyplot as plt
import palettable
import rasterio
import git
from git import exc as git_exc
from tqdm import tqdm

# +++++ Make the repo's `Code/` package importable +++++
try:
    _git_root = git.Repo(
        os.path.dirname(os.path.abspath(__file__)),
        search_parent_directories=True,
    ).git.rev_parse("--show-toplevel")
    if _git_root not in sys.path:
        sys.path.insert(0, _git_root)
except git_exc.InvalidGitRepositoryError:
    pass

import Code.functions.core_functions as cf
import Code.functions.gcp_qc as gq


# +++++ DSM filename pattern: <stem>_LiDAR_DSM_<cm>cm.tif +++++
_DSM_RE = re.compile(r"_LiDAR_DSM_(\d+)cm\.tif$", re.IGNORECASE)


# ==================================================================================
@dataclass(frozen=True)
class QAConfig:
    """Tunable thresholds and report metadata for one QA invocation.

    All settings live on this object so callers can pass a single
    ``cfg`` argument through the pipeline rather than relying on
    module-level constants. Use :func:`default_config` to build the
    default instance and replace fields with ``dataclasses.replace``
    if you need overrides.

    Attributes
    ----------
    schema_version : float
        Report JSON schema version. Bump whenever the JSON shape
        changes; cached reports with a different value are
        auto-regenerated.
    pass_threshold_2d_m : float
        Maximum per-point 2D distance (m) for a pair to ``pass``.
    warn_height_delta_mean_m : float
        Triggers a warning when ``|mean(delta_height)|`` exceeds this
        value (often indicates a height-datum issue).
    warn_planar_bias_mean_m : float
        Triggers a warning when planar 2D bias magnitude exceeds this
        value AND the bias classification is ``"biased"`` or
        ``"mixed"``.
    """
    # +++++ Pass/fail threshold for 2D point accuracy (metres) +++++
    pass_threshold_2d_m: float = 0.10

    # +++++ JSON schema version +++++
    # Changing this triggers regeneration of cached reports, so bump whenever the
    # report structure or interpretation changes in a non-backwards-compatible way. 
    schema_version: float = 1.03

    # +++++ Warning thresholds (metres) +++++
    # These are not pass/fail thresholds but rather trigger warnings in the report 
    # when exceeded, as they often indicate specific issues (e.g. a height datum 
    # mismatch for the height delta mean). The warning logic is implemented in _build_report.
    # New warnings can be added as needed by introducing new fields here and wiring them 
    # into the report generation logic.
    warn_height_delta_mean_m: float = 0.50
    warn_planar_bias_mean_m: float = 0.04


def default_config() -> QAConfig:
    """Return the default :class:`QAConfig` for this tool."""
    return QAConfig()


# ==================================================================================
def main(args: argparse.Namespace, path: pathlib.Path) -> pd.DataFrame:
    """Run the point-distance comparison across all groundtruth/QC pairs.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments.
    path : pathlib.Path
        Root directory to crawl for groundtruth/QC pairs.

    Returns
    -------
    pandas.DataFrame
        One row per pair with columns ``project, sensor, date, run,
        product, n, mean_m, max_m, status, reason``. Callers may
        persist this for later inspection.
    """
    print(f"Starting search for groundtruth/QC pairs under {path} at: {pd.Timestamp.now()}")
    cfg = default_config()
    pairs = locate_point_pairs(path, args.exclude_dir, args.verbose)
    if not pairs:
        raise ValueError(
            f"No QC_GCP_groundtruth_points / QC_GCP_points pairs "
            f"(.geojson, or legacy .shp) found under {path}."
        )

    print(f"Found {len(pairs)} groundtruth/QC pair(s).")
    rows: List[Dict[str, Any]] = []
    pbar = tqdm(pairs, desc="Comparing pairs", unit="pair")
    for pair in pbar:
        vali = pair["vali"]
        pbar.set_postfix_str(str(vali.relative_to(path)))
        meta = {**_summary_metadata(pair["run_dir"]),
                "product": pair.get("product")}
        try:
            report = _process_pair(pair, args, cfg)
        except (FileNotFoundError, ValueError) as exc:
            rows.append({**meta, "status": "skipped", "reason": str(exc)})
            continue
        d2d = report["statistics_metres"]["distance_2d"]
        rows.append({
            **meta,
            "n": d2d.get("n", 0),
            "mean_m": d2d.get("mean"),
            "max_m": d2d.get("max"),
            "status": report["status"]["result"],
            "warning": bool(report.get("warnings", {}).get("triggered", False)),
            "cached": bool(report.get("cached", False)),
        })

    summary = _summary_dataframe(rows)
    _print_summary_tables(summary)
    return summary

# ==================================================================================
def _summary_metadata(run_dir: pathlib.Path) -> Dict[str, Any]:
    """Pull project / sensor / date / run from an APPN run directory.

    Falls back to ``None`` for any field the parser cannot resolve.
    """
    parsed = cf.parse_APPN_dataset_path(run_dir)
    date = parsed.get("date")
    return {
        "project": parsed.get("project"),
        "sensor": parsed.get("sensor"),
        "date": date.strftime("%Y-%m-%d") if date is not None and pd.notna(date) else None,
        "run": parsed.get("run"),
    }


def _summary_dataframe(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """Assemble the per-pair summary DataFrame.

    Columns are always present in a fixed order, regardless of which
    keys the input rows happen to carry, so callers can rely on the
    schema for downstream saving.
    """
    columns = ["project", "sensor", "date", "run", "product",
               "n", "mean_m", "max_m", "status", "warning", "cached", "reason"]
    df = pd.DataFrame(rows, columns=columns)
    return df


def _format_summary_for_print(df: pd.DataFrame) -> pd.DataFrame:
    """Return a display-ready copy of the summary DataFrame."""
    out = df.copy()
    if "n" in out.columns:
        out["n"] = out["n"].apply(
            lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v))
            else f"{int(v)}"
        )
    if "mean_m" in out.columns:
        out["mean_m"] = out["mean_m"].apply(
            lambda v: "" if v is None or pd.isna(v) else f"{v:.4f}"
        )
    if "max_m" in out.columns:
        out["max_m"] = out["max_m"].apply(
            lambda v: "" if v is None or pd.isna(v) else f"{v:.4f}"
        )
    if "reason" in out.columns:
        out["reason"] = out["reason"].fillna("")
    if "product" in out.columns:
        out["product"] = out["product"].fillna("")
    if "warning" in out.columns:
        out["warning"] = out["warning"].apply(
            lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v))
            else ("yes" if bool(v) else "no")
        )
    return out.rename(columns={"mean_m": "mean (m)", "max_m": "max (m)"})


def _print_summary_tables(df: pd.DataFrame) -> None:
    """Print the summary split into a passed and a failed/skipped table.

    Pairs with ``status == "pass"`` go to the passed table; pairs
    with ``status == "fail"`` go to the failed table; everything else
    (``"skipped"``, ``"unknown"``) goes to the skipped table. Each
    table omits columns that aren't useful for that group (no
    ``reason`` for passes).
    """
    if df.empty:
        print("\nNo pairs to summarise.")
        return

    passed = df[df["status"] == "pass"]
    failed = df[df["status"] == "fail"]
    skipped = df[~df["status"].isin(["pass", "fail"])]

    if not skipped.empty:
        cols = ["project", "sensor", "date", "run", "product",
                "status", "reason"]
        print(f"\nSKIPPED ({len(skipped)}):")
        print(_format_summary_for_print(skipped[cols]).to_string(index=False))

    if not passed.empty:
        cols = ["project", "sensor", "date", "run", "product",
                "n", "mean_m", "max_m", "status", "warning", "cached"]
        print(f"\nPASSED ({len(passed)}):")
        print(_format_summary_for_print(passed[cols]).to_string(index=False))

    if not failed.empty:
        cols = ["project", "sensor", "date", "run", "product",
                "n", "mean_m", "max_m", "status", "warning", "cached", "reason"]
        print(f"\nFAILED ({len(failed)}):")
        print(_format_summary_for_print(failed[cols]).to_string(index=False))



# ==================================================================================
def _process_pair(
        pair: Dict[str, Any],
        args: argparse.Namespace,
        cfg: QAConfig,
    ) -> Dict[str, Any]:
    """Compare a single groundtruth/QC pair and write the distance table.

    Parameters
    ----------
    pair : dict
        Mapping of role -> file path produced by
        :func:`locate_point_pairs`. Required keys: ``"vali"`` and
        ``"qc"``. Additional optional keys (e.g. ``"flightlines"``,
        ``"qc_z"``) may be added in future and will be ignored here
        until they are wired in.
    args : argparse.Namespace
        Parsed command-line arguments.

    Returns
    -------
    dict
        The accuracy report produced by :func:`_build_report` (see
        that function for the schema). Returned so callers can roll
        results up across all processed pairs.
    """
    file_a = pair["vali"]
    file_b = pair["qc"]
    if "missing_input" in pair:
        raise ValueError(pair["missing_input"])
    if "gpro_error" in pair:
        err = pair["gpro_error"]
        found = ", ".join(str(p) for p in err["found_dirs"]) or "(none)"
        raise ValueError(f"{err['reason']} [found: {found}]")

    product = pair.get("product")
    extra = pair.get("extra")
    out_stem = ("QC_GCP_" + (f"{product}_" if product else "")
                + "distances" + (f"_{extra}" if extra else ""))
    out_path = file_b.parent / f"{out_stem}.{args.type}"
    report_path = file_b.parent / f"{out_stem}_report.json"
    plots_dir = file_b.parent / "QC_plots"
    plot_path = plots_dir / f"{out_stem}_displacements.png"

    # +++++ Skip work when outputs already exist and are up to date +++++
    inputs_for_cache = [file_a, file_b]
    if pair.get("dsm") is not None:
        inputs_for_cache.append(pair["dsm"])
    if not args.force and cf.outputs_up_to_date(
        [out_path, report_path, plot_path], inputs_for_cache,
    ):
        with report_path.open("r", encoding="utf-8") as fh:
            report = json.load(fh)
        cached_version = report.get("schema_version")
        if cached_version == cfg.schema_version:
            report["cached"] = True
            if args.verbose:
                print(f"  up to date, reusing {report_path.name}")
            return report
        if args.verbose:
            print(
                f"  schema_version {cached_version!r} != {cfg.schema_version!r}, "
                f"regenerating {report_path.name}"
            )

    gdf_a = gpd.read_file(file_a)
    gdf_b = gpd.read_file(file_b)

    id_a = _resolve_id_column(gdf_a, file_a, args.id_column)
    id_b = _resolve_id_column(gdf_b, file_b, args.id_column)
    _validate_points(gdf_a, file_a, id_a)
    _validate_points(gdf_b, file_b, id_b)
    _check_crs_match(gdf_a, gdf_b, file_a, file_b)
    src_crs = gdf_a.crs
    assert src_crs is not None  # guaranteed by _check_crs_match

    # +++++ Sample DSM (in source CRS) for the QC points' Z heights +++++
    dsm_info: Optional[Dict[str, Any]] = None
    dsm_path = pair.get("dsm")
    if dsm_path is None:
        dsm_info = {"reason": "no DSM TIF in products/"}
    else:
        z_values, dsm_info = _sample_raster_at_points(gdf_b, dsm_path)
        if z_values is not None:
            xs = gdf_b.geometry.x.to_numpy()
            ys = gdf_b.geometry.y.to_numpy()
            new_geom = gpd.points_from_xy(xs, ys, z_values)
            gdf_b = gdf_b.set_geometry(
                gpd.GeoSeries(new_geom, index=gdf_b.index, crs=gdf_b.crs),
            )
            if args.verbose:
                print(
                    f"  DSM sampled: {dsm_info['n_sampled']} of "
                    f"{len(gdf_b)} point(s) have Z from "
                    f"{dsm_path.name}"
                )

    metric_crs = _resolve_metric_crs(gdf_a)
    if not src_crs.equals(metric_crs):
        if args.verbose:
            print(f"  reprojecting {src_crs.to_string()} -> {metric_crs.to_string()} for metre distances")
        gdf_a = gdf_a.to_crs(metric_crs)
        gdf_b = gdf_b.to_crs(metric_crs)
    if args.verbose:
        print(f"  CRS (source): {src_crs}")
        print(f"  CRS (distance computation, metres): {metric_crs}")
        print(f"  ID columns: {file_a.name}->'{id_a}', {file_b.name}->'{id_b}'")

    matched, only_a, only_b = _match_and_measure(
        gdf_a, gdf_b, id_a, id_b,
    )
    if matched.empty:
        raise ValueError(
            f"No matching IDs found between '{id_a}' and '{id_b}'."
        )

    report = _build_report(
        pair=pair,
        matched=matched,
        only_a=only_a,
        only_b=only_b,
        id_column_a=id_a,
        id_column_b=id_b,
        src_crs=src_crs,
        metric_crs=metric_crs,
        dsm_info=dsm_info,
        cfg=cfg,
    )
    if args.verbose:
        _print_report(report)

    _save_table(matched, out_path, args.type)
    if args.verbose:
        print(f"  wrote {len(matched)} row(s) to {out_path}")

    _save_report(report, report_path)
    if args.verbose:
        print(f"  wrote accuracy report to {report_path}")

    # +++++ Plots are regenerated whenever the report is regenerated +++++
    _plot_displacements(matched, report, plot_path, show=args.plot)
    if args.verbose:
        print(f"  wrote displacement plot to {plot_path}")

    return report


# ==================================================================================
def locate_point_pairs(
        path: pathlib.Path,
        exclude_dirs: Optional[List[str]] = None,
        verbose: bool = False,
    ) -> List[Dict[str, Any]]:
    """Find groups of groundtruth / QC / auxiliary files in the dataset tree.

    For each groundtruth file at
    ``.../<run>/T1_proc/QC_data/QC_GCP_groundtruth_points.geojson`` the
    sibling ``QC_data`` directory is inspected for related
    inputs. The required pair is the groundtruth geojson and one or
    more observed QC GCP layers: ``QC_GCP_points.geojson`` when a
    single digitised layer is valid for every data product, or
    ``QC_GCP_{Product}_points.geojson`` per product when the products
    do not align (legacy ``.shp`` accepted as a fallback for either
    form). Additional optional inputs
    (e.g. flight-line info, a Z-augmented QC layer) are looked up by
    role and included only when present, so callers can opt in as
    those datasets become available without changing this signature.

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively.
    exclude_dirs : list of str, optional
        Directory names to skip when matched anywhere in the path.
    verbose : bool
        Print extra diagnostic information about skipped files.

    Returns
    -------
    list of dict
        One dict per groundtruth/QC pair (a run with N per-product QC
        layers yields N dicts sharing the same groundtruth). Always
        contains:

        * ``"vali"``  -- groundtruth geojson (validation points).
        * ``"qc"``    -- the observed QC GCP layer (geojson, or
          legacy ``.shp``).

        May additionally contain (when the file is found on disk):

        * ``"product"``     -- the ``{Product}`` identifier when the
          QC layer is per-product (absent for ``QC_GCP_points``).
        * ``"extra"``       -- trailing ``_{extraInfo}`` suffix on the
          QC layer's filename, when present.
        * ``"qc_z"``        -- QC GCP points with Z heights.
        * ``"flightlines"`` -- flight-line information layer.
        * ``"run_dir"``     -- the ``<run>`` directory itself, useful
          for downstream code that needs to discover further files.
        * ``"products_dir"`` -- the run's
          ``T1_proc/<NAME>.gpro/products`` folder when exactly one
          ``.gpro`` is present.
        * ``"dsm"``         -- highest-resolution
          ``*_LiDAR_DSM_<cm>cm.tif`` from ``products_dir`` when found.
        * ``"gpro_error"``  -- a dict with ``count``, ``found_dirs``
          and ``reason`` describing why the run's products folder
          could not be resolved (zero or multiple ``.gpro``
          directories). When present, the pair must not be processed.
    """
    exclude = set(exclude_dirs or [])

    def _excluded(p: pathlib.Path) -> bool:
        return bool(exclude and (set(part.name for part in p.parents) & exclude))

    def _find_points(pattern: str) -> List[pathlib.Path]:
        # +++++ Prefer .geojson; legacy .shp only where no geojson sibling exists +++++
        geo = [p for p in path.rglob(f"{pattern}.geojson") if not _excluded(p)]
        geo_keys = {(p.parent, p.stem) for p in geo}
        shp: List[pathlib.Path] = []
        for p in path.rglob(f"{pattern}.shp"):
            if _excluded(p):
                continue
            if (p.parent, p.stem) in geo_keys:
                if verbose:
                    print(f"  ignoring legacy {p}: geojson sibling preferred")
                continue
            shp.append(p)
        return sorted(geo + shp)

    vali_files = _find_points("QC_GCP_groundtruth_points")

    # +++++ Observed layers: QC_GCP[_{Product}]_points[_{extraInfo}] +++++
    # `groundtruth` is a reserved identifier for the reference layer above;
    # `_z`/`_3d` suffixes are the qc_z role, not extra info.
    qc_files: List[Tuple[pathlib.Path, Optional[str], Optional[str]]] = []
    for p in _find_points("QC_GCP_*points*"):
        m = re.match(r"QC_GCP_(?:(.+)_)?points(?:_(.+))?$", p.stem)
        if m is None or m.group(1) == "groundtruth" or m.group(2) in ("z", "3d"):
            continue
        qc_files.append((p, m.group(1), m.group(2)))

    # +++++ Optional inputs: role -> candidate filenames in QC_data +++++
    # Add new role/filename(s) here as additional layers become
    # available. The first existing candidate wins.
    optional_qc_files: Dict[str, Tuple[str, ...]] = {
        "qc_z": ("QC_GCP_points_z.geojson", "QC_GCP_points_3d.geojson",
                 "QC_GCP_points_z.shp", "QC_GCP_points_3d.shp"),
        "flightlines": ("flightlines.geojson", "flight_lines.geojson",
                        "flight_lines.shp"),
    }

    # +++++ Index QC files by their parent run_dir for fast cross-lookup +++++
    qc_by_run: Dict[
        pathlib.Path, List[Tuple[pathlib.Path, Optional[str], Optional[str]]]
    ] = {}
    for qc, product, extra in qc_files:
        # +++++ QC lives in <run>/T1_proc/QC_data/, so <run> is parents[2] +++++
        if qc.parent.name != "QC_data" or qc.parents[1].name != "T1_proc":
            if verbose:
                print(f"  ignoring {qc}: not under T1_proc/QC_data")
            continue
        qc_by_run.setdefault(qc.parents[2], []).append((qc, product, extra))

    pairs: List[Dict[str, Any]] = []
    seen_runs: set = set()
    for vali in vali_files:
        # +++++ groundtruth lives in <run>/T1_proc/QC_data/, so <run> is parents[2] +++++
        if vali.parent.name != "QC_data" or vali.parents[1].name != "T1_proc":
            if verbose:
                print(f"  ignoring {vali}: not under T1_proc/QC_data")
            continue
        run_dir = vali.parents[2]
        seen_runs.add(run_dir)
        qc_dir = run_dir / "T1_proc" / "QC_data"
        qc_list = qc_by_run.get(run_dir, [])
        if not qc_list:
            # +++++ Surface groundtruth-without-QC in the skipped summary +++++
            pairs.append({
                "vali": vali,
                "qc": qc_dir / "QC_GCP_points.geojson",
                "run_dir": run_dir,
                "missing_input": (
                    "no QC_GCP_points.geojson or QC_GCP_{Product}_points.geojson "
                    f"(or legacy .shp) for groundtruth (expected in {qc_dir})"
                ),
            })
            continue

        # +++++ Run-level lookups shared by every product's pair +++++
        optional_entries: Dict[str, pathlib.Path] = {}
        for role, candidates in optional_qc_files.items():
            for name in candidates:
                candidate = qc_dir / name
                if candidate.is_file():
                    optional_entries[role] = candidate
                    break

        # +++++ Locate the run's products folder and DSM TIF (if any) +++++
        products_dir, gpro_error = _resolve_products_dir(run_dir)
        dsm = None if products_dir is None else _resolve_dsm(products_dir)
        if verbose:
            if gpro_error is not None:
                print(
                    f"  ambiguous .gpro for {vali}: "
                    f"count={gpro_error['count']}"
                )
            elif dsm is None:
                print(f"  no LiDAR DSM TIF in {products_dir}")

        # +++++ One pair per QC layer (all share the run's groundtruth) +++++
        for qc_file, product, extra in qc_list:
            entry: Dict[str, Any] = {
                "vali": vali,
                "qc": qc_file,
                "run_dir": run_dir,
                **optional_entries,
            }
            if product is not None:
                entry["product"] = product
            if extra is not None:
                entry["extra"] = extra
            if gpro_error is not None:
                entry["gpro_error"] = gpro_error
            else:
                assert products_dir is not None
                entry["products_dir"] = products_dir
                if dsm is not None:
                    entry["dsm"] = dsm
            pairs.append(entry)

    # +++++ Also surface QC files whose run has no groundtruth input +++++
    for run_dir, qc_list in qc_by_run.items():
        if run_dir in seen_runs:
            continue
        gt_dir = run_dir / "T1_proc" / "QC_data"
        pairs.append({
            "vali": gt_dir / "QC_GCP_groundtruth_points.geojson",
            "qc": qc_list[0][0],
            "run_dir": run_dir,
            "missing_input": (
                f"no QC_GCP_groundtruth_points.geojson for QC (expected in {gt_dir})"
            ),
        })

    return pairs


def _resolve_products_dir(
        run_dir: pathlib.Path,
    ) -> Tuple[Optional[pathlib.Path], Optional[Dict[str, Any]]]:
    """Find the single ``<NAME>.gpro/products`` folder for a run.

    Parameters
    ----------
    run_dir : pathlib.Path
        The ``<run>`` directory containing a ``T1_proc/`` subdirectory.

    Returns
    -------
    products_dir : pathlib.Path or None
        Path to ``T1_proc/<NAME>.gpro/products`` when exactly one
        ``.gpro`` folder exists. ``None`` when zero or more than one
        ``.gpro`` folders are present.
    gpro_error : dict or None
        ``None`` on success. Otherwise a structured payload with keys
        ``"count"``, ``"found_dirs"`` (list of pathlib.Path), and
        ``"reason"`` (str).
    """
    proc_dir = run_dir / "T1_proc"
    gpros = sorted(p for p in proc_dir.glob("*.gpro") if p.is_dir())
    if len(gpros) == 1:
        return gpros[0] / "products", None
    if not gpros:
        reason = "no .gpro folder in T1_proc/"
    else:
        reason = f"{len(gpros)} .gpro folders in T1_proc/ (expected 1)"
    return None, {
        "count": len(gpros),
        "found_dirs": gpros,
        "reason": reason,
    }


def _resolve_dsm(products_dir: pathlib.Path) -> Optional[pathlib.Path]:
    """Pick the highest-resolution LiDAR DSM TIF from a products folder.

    Looks for filenames matching ``*_LiDAR_DSM_<cm>cm.tif`` and returns
    the one with the smallest ``<cm>`` value (highest resolution).

    Parameters
    ----------
    products_dir : pathlib.Path
        The ``<NAME>.gpro/products`` folder for a run.

    Returns
    -------
    pathlib.Path or None
        Selected DSM TIF, or ``None`` if no candidate is present.
    """
    if not products_dir.is_dir():
        return None
    candidates: List[Tuple[int, pathlib.Path]] = []
    for tif in products_dir.glob("*_LiDAR_DSM_*cm.tif"):
        match = _DSM_RE.search(tif.name)
        if match:
            candidates.append((int(match.group(1)), tif))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _sample_raster_at_points(
        gdf: gpd.GeoDataFrame,
        raster_path: pathlib.Path,
    ) -> Tuple[Optional[np.ndarray], Dict[str, Any]]:
    """Sample band 1 of *raster_path* at the locations of *gdf*.

    The raster's CRS must match ``gdf.crs``; if it does not, a warning
    is emitted and ``(None, info)`` is returned (no reprojection, no
    fallback). Sampling uses nearest-neighbour. Values equal to the
    raster's nodata, or for points outside the raster bounds, are set
    to NaN in the returned array.

    Parameters
    ----------
    gdf : geopandas.GeoDataFrame
        Point layer in its source CRS.
    raster_path : pathlib.Path
        Raster file to sample.

    Returns
    -------
    samples : numpy.ndarray or None
        Float array aligned to ``gdf.index`` with sampled values, or
        ``None`` when the CRS check fails.
    info : dict
        Diagnostic payload with keys ``"file"``, ``"crs"``,
        ``"nodata"``, ``"n_sampled"``, ``"n_outside"``,
        ``"reason"`` (only when ``samples is None``).
    """
    info: Dict[str, Any] = {
        "file": str(raster_path),
        "crs": None,
        "nodata": None,
        "n_sampled": 0,
        "n_outside": 0,
    }
    with rasterio.open(raster_path.as_posix()) as src:
        info["crs"] = src.crs.to_string() if src.crs is not None else None
        nodata = src.nodata
        if nodata is None or not np.isfinite(nodata):
            info["nodata"] = None
        else:
            info["nodata"] = float(nodata)
        if src.crs is None or gdf.crs is None or not src.crs == gdf.crs:
            reason = (
                f"DSM CRS ({info['crs']}) does not match QC points CRS "
                f"({gdf.crs.to_string() if gdf.crs is not None else None}); "
                "skipping DSM sampling for this pair."
            )
            warnings.warn(f"{raster_path.name}: {reason}", stacklevel=2)
            info["reason"] = reason
            return None, info

        xs = gdf.geometry.x.to_numpy()
        ys = gdf.geometry.y.to_numpy()
        bounds = src.bounds
        inside = (
            (xs >= bounds.left) & (xs <= bounds.right)
            & (ys >= bounds.bottom) & (ys <= bounds.top)
        )

        samples = np.full(len(gdf), np.nan, dtype=float)
        coords_inside = list(zip(xs[inside], ys[inside]))
        if coords_inside:
            sampled = np.array(
                [v[0] for v in src.sample(coords_inside, indexes=1)],
                dtype=float,
            )
            if src.nodata is not None and np.isfinite(src.nodata):
                sampled = np.where(
                    sampled == float(src.nodata), np.nan, sampled,
                )
            samples[inside] = sampled

    info["n_outside"] = int((~inside).sum())
    info["n_sampled"] = int(np.isfinite(samples).sum())
    return samples, info


def _validate_points(
        gdf: gpd.GeoDataFrame,
        path: pathlib.Path,
        id_column: str,
    ) -> None:
    """Confirm a GeoDataFrame is a usable points layer for comparison.

    Parameters
    ----------
    gdf : geopandas.GeoDataFrame
        Loaded layer.
    path : pathlib.Path
        Source file (used in error messages).
    id_column : str
        Required ID column.

    Raises
    ------
    ValueError
        If ``id_column`` is missing, the layer is empty, or it does not
        contain points.
    """
    if gdf.empty:
        raise ValueError(f"{path} contains no features.")
    if id_column not in gdf.columns:
        raise ValueError(
            f"{path} is missing the ID column '{id_column}'. "
            f"Available columns: {list(gdf.columns)}."
        )
    geom_types = set(gdf.geometry.geom_type.unique())
    if not geom_types.issubset({"Point"}):
        raise ValueError(
            f"{path} contains non-point geometries ({geom_types}). "
            "Only Point layers are supported."
        )


def _resolve_metric_crs(gdf: gpd.GeoDataFrame):
    """Return a projected CRS whose linear unit is metres.

    If *gdf*'s CRS is already projected in metres it is returned
    unchanged. Otherwise an appropriate UTM zone is estimated from the
    layer's extent via :meth:`GeoDataFrame.estimate_utm_crs`, which
    always returns a metre-based CRS.

    Parameters
    ----------
    gdf : geopandas.GeoDataFrame
        Layer used to determine the metric CRS.

    Returns
    -------
    pyproj.CRS
        A metre-based projected CRS suitable for distance computation.
    """
    crs = gdf.crs
    if crs is not None and crs.is_projected:
        units = {ax.unit_name for ax in crs.axis_info if ax.unit_name}
        if units and units.issubset({"metre", "meter"}):
            return crs
    return gdf.estimate_utm_crs()


def _check_crs_match(
        gdf_a: gpd.GeoDataFrame,
        gdf_b: gpd.GeoDataFrame,
        file_a: pathlib.Path,
        file_b: pathlib.Path,
    ) -> None:
    """Confirm both layers declare the same CRS.

    Parameters
    ----------
    gdf_a, gdf_b : geopandas.GeoDataFrame
        Loaded layers. Both must have a CRS defined.
    file_a, file_b : pathlib.Path
        Source paths, used in error messages.

    Raises
    ------
    ValueError
        If either layer is missing a CRS, or the two CRSs differ.
    """
    if gdf_a.crs is None:
        raise ValueError(f"{file_a} has no CRS defined.")
    if gdf_b.crs is None:
        raise ValueError(f"{file_b} has no CRS defined.")
    if not gdf_a.crs.equals(gdf_b.crs):
        raise ValueError(
            f"CRS mismatch: {file_a.name} is {gdf_a.crs} but "
            f"{file_b.name} is {gdf_b.crs}. Reproject the inputs to a "
            "common CRS before running this comparison."
        )


def _resolve_id_column(
        gdf: gpd.GeoDataFrame,
        path: pathlib.Path,
        candidates: List[str],
    ) -> str:
    """Pick the first candidate column name present in *gdf*.

    Parameters
    ----------
    gdf : geopandas.GeoDataFrame
        Loaded layer.
    path : pathlib.Path
        Source file (used in the error message).
    candidates : list of str
        Column names to try, in priority order.

    Returns
    -------
    str
        Name of the matched column.

    Raises
    ------
    ValueError
        If none of *candidates* are present in *gdf*.
    """
    for name in candidates:
        if name in gdf.columns:
            return name
    raise ValueError(
        f"{path} has none of the candidate ID columns {candidates}. "
        f"Available columns: {list(gdf.columns)}."
    )


def _match_and_measure(
        gdf_a: gpd.GeoDataFrame,
        gdf_b: gpd.GeoDataFrame,
        id_column_a: str,
        id_column_b: str,
    ) -> Tuple[pd.DataFrame, list, list]:
    """Match features by ID and compute pairwise distances.

    Parameters
    ----------
    gdf_a, gdf_b : geopandas.GeoDataFrame
        Point layers already projected into the same CRS.
    id_column_a, id_column_b : str
        Column used to match features in each file. Values are compared
        as strings, so the columns may have different names but must
        contain compatible identifiers.

    Returns
    -------
    matched : pandas.DataFrame
        One row per matched ID. Columns:
        ``id, easting_a_m, northing_a_m, height_a_m, easting_b_m,
        northing_b_m, height_b_m, delta_easting_m, delta_northing_m,
        distance_2d_m, bearing_deg, distance_3d_m, delta_height_m``.
        All distance/coordinate values are in metres. ``bearing_deg``
        is the offset direction in degrees clockwise from grid north.
        ``height_*_m``, ``distance_3d_m`` and ``delta_height_m`` are NaN
        when either file lacks a Z coordinate.
    only_a, only_b : list
        IDs present in only one of the inputs.
    """
    df_a = _flatten(gdf_a, id_column_a, suffix="a")
    df_b = _flatten(gdf_b, id_column_b, suffix="b")

    ids_a = set(df_a["id"])
    ids_b = set(df_b["id"])
    only_a = sorted(ids_a - ids_b)
    only_b = sorted(ids_b - ids_a)

    merged = df_a.merge(df_b, on="id", how="inner")

    dx = merged["easting_b_m"] - merged["easting_a_m"]
    dy = merged["northing_b_m"] - merged["northing_a_m"]
    merged["delta_easting_m"] = dx
    merged["delta_northing_m"] = dy
    merged["distance_2d_m"] = np.hypot(dx, dy)

    # Bearing of the offset vector (B - A), measured clockwise from
    # grid north in degrees, in the [0, 360) range.
    bearing = np.degrees(np.arctan2(dx, dy)) % 360.0
    # NaN bearing where the two points coincide (no defined direction).
    bearing = np.where(merged["distance_2d_m"] > 0, bearing, np.nan)
    merged["bearing_deg"] = bearing

    has_z = (merged["height_a_m"].notna() & merged["height_b_m"].notna())
    dz = merged["height_b_m"] - merged["height_a_m"]
    merged["delta_height_m"] = dz.where(has_z, np.nan)
    merged["distance_3d_m"] = np.where(
        has_z,
        np.sqrt(dx ** 2 + dy ** 2 + dz.fillna(0) ** 2),
        np.nan,
    )

    return merged, only_a, only_b


def _flatten(
        gdf: gpd.GeoDataFrame,
        id_column: str,
        suffix: str,
    ) -> pd.DataFrame:
    """Flatten a points GeoDataFrame to ``id`` + suffixed XY[Z] columns.

    Parameters
    ----------
    gdf : geopandas.GeoDataFrame
        Point layer (already in the target CRS).
    id_column : str
        Source column to use as the join key.
    suffix : str
        Either ``"a"`` or ``"b"``; appended to the coordinate column
        names.

    Returns
    -------
    pandas.DataFrame
        Columns ``id, easting_<suffix>, northing_<suffix>,
        height_<suffix>``. ``height_<suffix>`` is NaN for 2D geometries.
    """
    out = pd.DataFrame({
        "id": gdf[id_column].astype(str).values,
        f"easting_{suffix}_m": gdf.geometry.x.values,
        f"northing_{suffix}_m": gdf.geometry.y.values,
    })
    # Z is only present for XYZ geometries.
    has_z = gdf.geometry.has_z
    if has_z.any():
        z = np.where(has_z, gdf.geometry.z, np.nan)
    else:
        z = np.full(len(gdf), np.nan)
    out[f"height_{suffix}_m"] = z

    # Drop rows where the same ID appears more than once (ambiguous).
    duplicated_ids = out.loc[out["id"].duplicated(keep=False), "id"].unique()
    if len(duplicated_ids):
        print(
            f"  warning: dropping {len(duplicated_ids)} duplicate ID(s) in "
            f"file '{suffix}': {list(duplicated_ids)[:10]}"
            + (" ..." if len(duplicated_ids) > 10 else "")
        )
        out = out[~out["id"].isin(duplicated_ids)]
    return out


def _evaluate_warnings(
        stats: Dict[str, Dict[str, Any]],
        bias: Dict[str, Any],
        cfg: QAConfig,
    ) -> Dict[str, Any]:
    """Evaluate non-fatal warning conditions for a single VALI/QC pair.

    Each rule that fires contributes one human-readable string to the
    ``reasons`` list; ``triggered`` is True when at least one rule
    fires. Add new rules as they become available without changing
    the return shape.

    Current rules:

    * Mean height delta exceeds ``cfg.warn_height_delta_mean_m``
      (often indicates a height-datum problem).
    * Planar 2D bias is classified as ``"biased"`` or ``"mixed"``
      AND its magnitude exceeds ``cfg.warn_planar_bias_mean_m``.

    Parameters
    ----------
    stats : dict
        ``statistics_metres`` payload from :func:`_build_report`.
    bias : dict
        ``bias`` payload from :func:`Code.functions.gcp_qc.bias_analysis`.
    cfg : QAConfig
        Threshold configuration for this run.

    Returns
    -------
    dict
        ``{"triggered": bool, "reasons": [str, ...], "rules": {...}}``
        where ``rules`` records the threshold values used so the
        report is self-describing.
    """
    reasons: List[str] = []

    height = stats.get("delta_height", {}) if stats else {}
    h_mean = height.get("mean")
    if h_mean is not None and abs(float(h_mean)) > cfg.warn_height_delta_mean_m:
        reasons.append(
            f"|mean(delta_height)|={abs(float(h_mean)):.3f} m exceeds "
            f"{cfg.warn_height_delta_mean_m:.2f} m (possible height-datum issue)"
        )

    planar = bias.get("planar_2d", {}) if bias else {}
    p_class = planar.get("classification")
    p_mag = planar.get("bias_magnitude_m")
    if (
        p_class in {"biased", "mixed"}
        and p_mag is not None
        and float(p_mag) > cfg.warn_planar_bias_mean_m
    ):
        reasons.append(
            f"planar 2D bias magnitude={float(p_mag):.3f} m exceeds "
            f"{cfg.warn_planar_bias_mean_m:.2f} m (classification: {p_class})"
        )

    return {
        "triggered": bool(reasons),
        "reasons": reasons,
        "rules": {
            "height_delta_mean_threshold_m": cfg.warn_height_delta_mean_m,
            "planar_bias_magnitude_threshold_m": cfg.warn_planar_bias_mean_m,
        },
    }


def _build_report(
        pair: Dict[str, pathlib.Path],
        matched: pd.DataFrame,
        only_a: list,
        only_b: list,
        id_column_a: str,
        id_column_b: str,
        src_crs,
        metric_crs,
        dsm_info: Optional[Dict[str, Any]] = None,
        cfg: Optional[QAConfig] = None,
    ) -> Dict[str, Any]:
    """Assemble a JSON-serialisable accuracy report for one VALI/QC pair.

    The report captures inputs (file paths, ID columns, CRSs), match
    counts, and summary statistics for the 2D and 3D distance and
    height-difference columns. It is the canonical structure written
    to disk by :func:`_save_report` and is also the source for the
    on-screen summary printed by :func:`_print_report`.

    Parameters
    ----------
    pair : dict
        Output of :func:`locate_point_pairs` for this run.
    matched : pandas.DataFrame
        Output of :func:`_match_and_measure`.
    only_a, only_b : list
        IDs present in only one of the inputs.
    id_column_a, id_column_b : str
        Column names used for matching.
    src_crs, metric_crs : pyproj.CRS
        Source CRS of the inputs and the metre-based CRS used for
        distance computation.
    dsm_info : dict, optional
        Diagnostic payload from :func:`_sample_raster_at_points`, or a
        ``{"reason": ...}`` dict when no DSM was available. Recorded
        verbatim under ``rasters.dsm`` in the output report.

    Returns
    -------
    dict
        Nested mapping suitable for ``json.dump``.
    """
    stats = {
        "distance_2d":   gq.distance_stats(matched["distance_2d_m"]),
        "distance_3d":   gq.distance_stats(matched["distance_3d_m"]),
        "delta_height":  gq.distance_stats(matched["delta_height_m"]),
        "delta_easting": gq.distance_stats(matched["delta_easting_m"]),
        "delta_northing": gq.distance_stats(matched["delta_northing_m"]),
    }
    bias = gq.bias_analysis(matched)
    if cfg is None:
        cfg = default_config()
    warnings_payload = _evaluate_warnings(stats, bias, cfg)

    # +++++ Pass/fail: every matched point's 2D distance must be <= threshold +++++
    d2d = matched["distance_2d_m"].dropna()
    if d2d.empty:
        status = "unknown"
        n_failing = 0
        worst = None
    else:
        n_failing = int((d2d > cfg.pass_threshold_2d_m).sum())
        status = "pass" if n_failing == 0 else "fail"
        worst = float(d2d.max())

    return {
        "schema_version": cfg.schema_version,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "tool": {
            "name": __title__,
            "version": __version__,
        },
        "status": {
            "result": status,
            "metric": "distance_2d_m",
            "threshold_m": cfg.pass_threshold_2d_m,
            "rule": "all matched points must have distance_2d_m <= threshold_m",
            "n_failing": n_failing,
            "worst_distance_2d_m": worst,
        },
        "warnings": warnings_payload,
        "product": pair.get("product"),
        "extra": pair.get("extra"),
        "inputs": {
            role: str(p)
            for role, p in pair.items()
            if isinstance(p, pathlib.PurePath)
        },
        "id_columns": {
            "a": id_column_a,
            "b": id_column_b,
        },
        "crs": {
            "source": src_crs.to_string() if src_crs is not None else None,
            "distance": metric_crs.to_string() if metric_crs is not None else None,
        },
        "counts": {
            "matched": int(len(matched)),
            "only_in_a": int(len(only_a)),
            "only_in_b": int(len(only_b)),
        },
        "unmatched_ids": {
            "only_in_a": list(only_a),
            "only_in_b": list(only_b),
        },
        "statistics_metres": stats,
        "bias": bias,
        "rasters": {
            "dsm": dsm_info,
        },
    }


def _print_report(report: Dict[str, Any]) -> None:
    """Print a brief human-readable view of an accuracy report.

    Parameters
    ----------
    report : dict
        Output of :func:`_build_report`.
    """
    counts = report["counts"]
    ids = report["id_columns"]
    status = report["status"]
    label = status["result"].upper()
    print(
        f"\nStatus: {label} "
        f"(threshold {status['threshold_m'] * 100:.0f} cm on "
        f"{status['metric']}; {status['n_failing']} failing point(s))"
    )
    warn = report.get("warnings", {})
    if warn.get("triggered"):
        print(f"WARNING ({len(warn.get('reasons', []))}):")
        for reason in warn.get("reasons", []):
            print(f"  - {reason}")
    print(
        f"Matched {counts['matched']} point(s) "
        f"('{ids['a']}' vs '{ids['b']}')."
    )
    if counts["only_in_a"]:
        print(f"  {counts['only_in_a']} ID(s) only in file A: "
              f"{report['unmatched_ids']['only_in_a']}")
    if counts["only_in_b"]:
        print(f"  {counts['only_in_b']} ID(s) only in file B: "
              f"{report['unmatched_ids']['only_in_b']}")

    print("\nDistance summary (metres):")
    for key in ("distance_2d", "distance_3d"):
        s = report["statistics_metres"][key]
        if s.get("n", 0) == 0:
            print(f"  {key}: (no values)")
            continue
        print(
            f"  {key}: n={s['n']}  mean={s['mean']:.4f}  "
            f"median={s['median']:.4f}  min={s['min']:.4f}  "
            f"max={s['max']:.4f}  rmse={s['rmse']:.4f}"
        )

    bias = report.get("bias", {})
    planar = bias.get("planar_2d", {}) if bias else {}
    if planar.get("n", 0) > 0:
        bearing = planar.get("bias_bearing_deg")
        bearing_str = f"{bearing:.1f}°" if bearing is not None else "n/a"
        frac = planar.get("bias_fraction")
        frac_str = f"{frac:.2f}" if frac is not None else "n/a"
        print(
            f"\nBias (2D): magnitude={planar['bias_magnitude_m']:.4f} m  "
            f"bearing={bearing_str}  rmse={planar['rmse_2d_m']:.4f} m  "
            f"fraction={frac_str} -> {planar.get('classification', 'unknown')}"
        )
        height = bias.get("height", {})
        if height.get("n", 0) > 0:
            h_frac = height.get("bias_fraction")
            h_frac_str = f"{h_frac:.2f}" if h_frac is not None else "n/a"
            print(
                f"Bias (height): mean={height['mean']:+.4f} m  "
                f"std={height['std']:.4f} m  "
                f"fraction={h_frac_str} -> {height.get('classification', 'unknown')}"
            )


def _save_report(report: Dict[str, Any], out_path: pathlib.Path) -> None:
    """Write an accuracy report to disk as pretty-printed JSON.

    Parameters
    ----------
    report : dict
        Output of :func:`_build_report`.
    out_path : pathlib.Path
        Destination ``.json`` file. Parent directories are created as
        needed.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=False)
        fh.write("\n")


def _save_table(
        df: pd.DataFrame,
        out_path: pathlib.Path,
        file_type: str,
    ) -> None:
    """Write ``df`` to disk as CSV or Parquet.

    Parameters
    ----------
    df : pandas.DataFrame
        Table to write.
    out_path : pathlib.Path
        Destination file. Parent directories are created as needed.
    file_type : str
        Either ``"csv"`` or ``"parquet"``.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if file_type == "csv":
        df.to_csv(out_path.as_posix(), index=False)
    elif file_type == "parquet":
        df.to_parquet(out_path.as_posix(), index=False)
    else:
        raise ValueError(
            f"Unsupported --type '{file_type}'. Use 'csv' or 'parquet'."
        )


def _displacement_title(report: Dict[str, Any]) -> str:
    """Compose a multi-line title summarising 2D displacement stats.

    Pulls counts, mean/median/max/rmse and the planar-bias decomposition
    out of an accuracy *report*. Falls back gracefully when fields are
    missing (e.g. report has no matched points).
    """
    stats = report.get("statistics_metres", {}).get("distance_2d", {})
    n = stats.get("n", 0)
    if n == 0:
        return "Displacement: no matched points"
    line1 = (
        f"2D displacement (n={n})\n"
        f"mean={stats['mean']:.3f} m  median={stats['median']:.3f} m  "
        f"max={stats['max']:.3f} m  rmse={stats['rmse']:.3f} m"
    )
    planar = report.get("bias", {}).get("planar_2d", {})
    if planar.get("n", 0) > 0:
        bearing = planar.get("bias_bearing_deg")
        bearing_str = f"{bearing:.0f}\u00b0" if bearing is not None else "n/a"
        frac = planar.get("bias_fraction")
        frac_str = f"{frac:.2f}" if frac is not None else "n/a"
        line2 = (
            f"bias: {planar['bias_magnitude_m']:.3f} m @ {bearing_str}  "
            f"(fraction={frac_str}, {planar.get('classification', 'unknown')})"
        )
    else:
        line2 = ""
    status = report.get("status", {})
    result = status.get("result", "unknown").upper()
    threshold = status.get("threshold_m")
    if threshold is not None:
        line3 = f"status: {result} (threshold {threshold * 100:.0f} cm)"
    else:
        line3 = f"status: {result}"
    return "\n".join(part for part in (line1, line2, line3) if part)


def _plot_displacements(
        matched: pd.DataFrame,
        report: Dict[str, Any],
        save_path: pathlib.Path,
        show: bool = False,
    ) -> None:
    """Render and save a quick-look plot of matched-point displacements.

    Produces a quiver plot of the in-plane offset vectors (B minus A)
    drawn from the origin, with reference rings every 5 cm. The figure
    is always saved to *save_path* (parent directory created as
    needed); when *show* is true it is also displayed interactively.

    Parameters
    ----------
    matched : pandas.DataFrame
        Output of :func:`_match_and_measure`.
    report : dict
        Output of :func:`_build_report`; used to compose a stats-based
        title via :func:`_displacement_title`.
    save_path : pathlib.Path
        Destination image file (e.g. ``.../QC_plots/...png``).
    show : bool
        If true, call ``plt.show()`` after saving.
    """
    fig, ax = plt.subplots(figsize=(6, 6.5))

    # Quiver: arrows go from the origin in the direction/magnitude of B - A.
    dx = matched["delta_easting_m"].to_numpy()
    dy = matched["delta_northing_m"].to_numpy()
    zeros = np.zeros_like(dx)
    ax.axhline(0, color="grey", lw=0.5)
    ax.axvline(0, color="grey", lw=0.5)

    # Reference circles every 5 cm out to the furthest point.
    max_r = float(np.nanmax(matched["distance_2d_m"])) if len(dx) else 0.0
    step = 0.05
    n_rings = max(1, int(np.ceil(max_r / step))) if max_r > 0 else 1
    theta = np.linspace(0, 2 * np.pi, 256)
    ring_colors = palettable.colorbrewer.diverging.PiYG_4_r.mpl_colors # type: ignore
    last_color = ring_colors[-1]
    for k in range(1, n_rings + 1):
        r = k * step
        # First N rings use the discrete palette colours; any rings
        # beyond the palette length stay at the final (magenta) colour.
        color = ring_colors[k - 1] if k <= len(ring_colors) else last_color
        ax.plot(r * np.cos(theta), r * np.sin(theta),
                color=color, lw=1.0, alpha=0.8)
        ax.text(0, r, f"{r * 100:.0f} cm", fontsize=7,
                color=color, ha="left", va="bottom")

    ax.quiver(
        zeros, zeros, dx, dy,
        matched["distance_2d_m"],
        angles="xy", scale_units="xy", scale=1.0, width=0.004,
    )
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xlabel("ΔEasting (m)")
    ax.set_ylabel("ΔNorthing (m)")
    ax.set_title(_displacement_title(report), fontsize=10)
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    save_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(save_path.as_posix(), dpi=150)
    if show:
        plt.show()
    else:
        plt.close(fig)


# ==================================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=("Crawl the dataset for VALI/QC point-file pairs and "
                     "report the distance between matched features."),
    )
    parser.add_argument("--path", type=str, default=None,
                        help=("Root directory to search for VALI/QC pairs. "
                              "Defaults to the git repository root."))
    parser.add_argument("--id-column", type=str, nargs="+",
                        default=["ID", "GCP_name"],
                        help=("Candidate column name(s) used to match "
                              "features. Each file is matched against the "
                              "first candidate it contains. Default: "
                              "'ID GCP_name'."))
    parser.add_argument("--type", type=str, default="csv",
                        choices=["csv", "parquet"],
                        help="Output table format. Default: csv.")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[],
                        help=("One or more directory names to exclude from "
                              "the search."))
    parser.add_argument("--plot", default=False, action="store_true",
                        help=("After saving, also display the per-pair "
                              "displacement plot interactively. Plots are "
                              "always written to <QC_data>/QC_plots/ "
                              "whenever the JSON report is regenerated."))
    parser.add_argument("-f", "--force", default=False, action="store_true",
                        help=("Recompute every pair even when the output "
                              "files already exist and are newer than the "
                              "inputs. Default: skip up-to-date pairs."))
    parser.add_argument("-v", "--verbose", default=False,
                        action="store_true",
                        help="Print extra diagnostic information.")

    args = parser.parse_args()

    # +++++ Resolve search root: --path overrides git, otherwise use git root +++++
    if args.path is not None:
        root = pathlib.Path(args.path).expanduser().resolve()
    else:
        try:
            git_repo = git.Repo(os.getcwd(), search_parent_directories=True)
            root = pathlib.Path(
                git_repo.git.rev_parse("--show-toplevel")
            ).resolve()
        except git_exc.InvalidGitRepositoryError as err:
            print(
                f"Error: not in a git repo and --path was not provided: {err}",
                file=sys.stderr,
            )
            sys.exit(1)

    if not root.is_dir():
        print(f"Error: search path is not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    try:
        main(args, root)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
