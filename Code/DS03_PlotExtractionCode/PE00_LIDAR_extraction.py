"""LIDAR plot extraction — per-plot point-cloud tables (DS03).

This script crawls the APPN dataset structure for GRYFN LiDAR point
clouds (``<run>/T1_proc/*.gpro/products/*_LiDAR_CombinedPointCloud.las``
or ``.laz``) and extracts the points falling inside each polygon of the
site's mandatory Plot_Layout file
(``Documentation/Plot_Layout/{YYYYSiteName}_plots.geojson``). For every
point it also samples the sibling DTM and DSM rasters and computes the
canopy height ``Delta_z = z - DTM``.

Per run the output is a long-format parquet *dataset* (one row per
point, tagged with ``plot_id`` and the run metadata) written as a
directory of per-scan-chunk part files to
``<run>/T1_proc/PlotExtracts/PixelLevel/PE_LIDAR_points[_gproN][_{variant}]/``
plus a YAML provenance sidecar beside the directory. Chunks stream
straight to disk, so peak memory is one laspy chunk regardless of
point-cloud size, and any parquet dataset reader (pandas, pyarrow,
duckdb) reads the directory as one table. The sidecar is written last
and doubles as the completion marker: extraction is cached via
``outputs_up_to_date`` on the sidecar (inputs = point cloud, DSM/DTM,
plot file). Use ``--force`` to override. ``--type csv`` writes a single
flat file instead (debugging escape hatch; no plot metrics).

A per-plot canopy-height metrics table is then derived from the saved
point dataset (never a second point-cloud read):
``PlotLevel/PE_LIDAR_plot_metrics[…].parquet`` — one row per plot with
the shared DS03 statistic set (count/mean/std/var/min/max/median/skew/
kurtosis/normality/p01-p99 short percentiles) of ``Delta_z`` plus run
metadata. ``--full-percentiles`` additionally writes the full 0-100
percentile profile per plot to
``PlotLevel/PE_LIDAR_plot_percentiles[…].parquet``.

Command-line Arguments
----------------------
--path : str, optional
    Folder to crawl for LiDAR products. Defaults to the git repo root.
--plot-variant : str, optional
    Select a plot-file variant (``{YYYYSiteName}_plots_{variant}[_vNN]``)
    instead of the mandatory main plot file.
--join-trial-info : flag
    Join ``Documentation/Trial_Info/{YYYYSiteName}_trial_info.csv`` onto
    the plots via ``plot_id`` before extraction.
--full-percentiles : flag
    Also write the full 0-100 percentile profile of ``Delta_z`` per plot
    to its own long-format parquet table.
--force : flag
    Re-create output files even when they are up to date.
"""

# ==============================================================================

__title__ = "LIDAR plot extraction"
__author__ = "Arden Burrell & Richard Harwood"
__version__ = "v2.2(19.08.2026)"
__email__ = "arden.burrell@sydney.edu.au"

# ==============================================================================
# ========== Import core packages ==========
import os
import sys
import argparse
import pathlib
from dataclasses import dataclass
from typing import Dict, List, Any, Tuple, Optional

# ========== Import other packages ==========
import git
from git import exc as git_exc
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import laspy
import rioxarray
import xarray as xr
import geopandas as gpd
from tqdm import tqdm
import warnings as warn

# ========== Resolve git root (must happen before importing Code.functions.*) ==========
# Resolved from this file's location so the Code/ package is importable even
# when the search --path points at a non-git dataset root.
try:
    _git_root = git.Repo(
        os.path.dirname(os.path.abspath(__file__)),
        search_parent_directories=True,
    ).git.rev_parse("--show-toplevel")
except git_exc.InvalidGitRepositoryError as err:
    raise git_exc.InvalidGitRepositoryError(
        f"Could not resolve the git repo containing this script ({__file__})."
    ) from err
if _git_root not in sys.path:
    sys.path.insert(0, _git_root)

# ========== Import custom packages ==========
import Code.functions.core_functions as cf
import Code.functions.plot_layout as pl
import Code.functions.plot_extracts as pex


# ==================================================================================
@dataclass(frozen=True)
class PEConfig:
    """Tunable settings for one LIDAR plot-extraction invocation.

    Attributes
    ----------
    valid_sensors : tuple of str
        Sensor platform folder names handled by this script.
    chunk_points : int
        Number of points read per laspy chunk. Bounds peak memory for
        large point clouds (~100 MB of coordinates per 4M points).
    """
    valid_sensors: Tuple[str, ...] = ("GOBI", "CALVIS")
    chunk_points: int = 4_000_000


def default_config() -> PEConfig:
    """Return the default :class:`PEConfig` for this tool."""
    return PEConfig()


# ==================================================================================
def main(args: argparse.Namespace, path: pathlib.Path) -> pd.DataFrame:
    """Run the LIDAR plot-extraction pipeline.

    Locates the LiDAR point clouds under *path*, resolves and validates
    each site's Plot_Layout file, extracts per-plot points (with DTM/DSM
    sampling) into parquet tables, and prints a REPORTED/SKIPPED summary.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments.
    path : pathlib.Path
        Root directory to crawl.

    Returns
    -------
    pandas.DataFrame
        One row per located point cloud summarising the outcome.
    """
    cfg = default_config()

    # ========== Locate the point clouds and their sibling rasters ==========
    jobs = locate_lidar_runs(path, cfg, args)

    # ========== Load each site's plot file once ==========
    plots_by_site = pl.load_site_plots(
        (j["site_dir"] for j in jobs),
        variant=args.plot_variant, join_trial_info=args.join_trial_info)

    # ========== Extract each run (cached) ==========
    summary_rows: List[Dict[str, Any]] = []
    repo = _try_repo(path)
    for job in tqdm(jobs, desc="Extracting LIDAR runs"):
        plotshp, plot_issues = plots_by_site[job["site_dir"]]
        if plotshp is None:
            summary_rows.append(_summary_row(job, "skipped", "; ".join(plot_issues)))
            continue
        row = process_lidar(job, plotshp, cfg, args, repo)
        summary_rows.append(row)

    # ========== Print the end-of-run summary ==========
    summary = _print_run_summary(summary_rows)
    return summary


# ==================================================================================
def locate_lidar_runs(
        path: pathlib.Path,
        cfg: PEConfig,
        args: argparse.Namespace,
    ) -> List[Dict[str, Any]]:
    """Find LiDAR point clouds and assemble one job dict per file.

    Recursively searches *path* for
    ``T1_proc/*.gpro/products/*_LiDAR_CombinedPointCloud.{las,laz}``,
    parses the APPN folder metadata, locates the sibling DSM/DTM rasters
    and the site folder, and builds the output paths.

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively.
    cfg : PEConfig
        Tunable settings (valid sensors, output folder name).
    args : argparse.Namespace
        Parsed command-line arguments (``exclude_dir``,
        ``allow_multi_gpro``, ``plot_variant``, ``type``, ``verbose``).

    Returns
    -------
    list of dict
        One dict per point cloud with keys ``las``, ``dsm``, ``dtm``,
        ``outfile``, ``metadata_outfile``, ``site_dir``, ``gpro_nu``,
        ``issues`` plus the parsed run metadata (``node``, ``project``,
        ``site``, ``sensor``, ``date``, ``run``).

    Raises
    ------
    ValueError
        If no point clouds are found under *path*.
    """
    print(f"Scanning {path} for LiDAR point clouds. {pd.Timestamp.now()}")
    files = sorted(
        list(path.rglob("*_LiDAR_CombinedPointCloud.las"))
        + list(path.rglob("*_LiDAR_CombinedPointCloud.laz")))

    # +++++ Enforce the official location: <run>/T1_proc/<x>.gpro/products/ +++++
    files = [f for f in files
             if f.parent.name == "products"
             and f.parents[1].suffix == ".gpro"
             and f.parents[2].name == "T1_proc"]

    if args.exclude_dir:
        exclude_set = set(args.exclude_dir)
        files = [f for f in files
                 if not (set(p.name for p in f.parents) & exclude_set)]

    if len(files) == 0:
        raise ValueError(
            f"No LiDAR point clouds found in {path}. Expected files matching "
            "<run>/T1_proc/*.gpro/products/*_LiDAR_CombinedPointCloud.las|laz.")

    jobs: List[Dict[str, Any]] = []
    for las in files:
        run_dir = las.parents[2].parent
        issues: List[str] = []

        # ========== Require exactly one .gpro per run (QA00 pattern) ==========
        gpro_dirs = sorted(las.parents[2].glob("*.gpro"))
        gpro_nu: Optional[int] = None
        if len(gpro_dirs) > 1:
            if args.allow_multi_gpro:
                gpro_nu = gpro_dirs.index(las.parents[1])
                warn.warn(
                    f"Found {len(gpro_dirs)} .gpro folders in {las.parents[2]}. "
                    "Processing anyway because --allow-multi-gpro is set; treat "
                    "the extracted tables as debugging output.")
            else:
                warn.warn(
                    f"Found {len(gpro_dirs)} .gpro folders in {las.parents[2]} "
                    f"({[g.name for g in gpro_dirs]}). Multiple .gpro folders "
                    "usually indicate an issue being actively debugged; skipping "
                    "this run (or use --allow-multi-gpro).")
                continue

        # ========== Parse the APPN folder structure for metadata ==========
        parsed = cf.parse_APPN_dataset_path(las)
        if not parsed["valid"]:
            warn.warn(f"Could not parse APPN metadata for {las}: "
                      f"{parsed['errors']}. Skipping file.")
            continue
        if parsed["sensor"] not in cfg.valid_sensors:
            if args.verbose:
                tqdm.write(f"Skipping {las}: sensor {parsed['sensor']} not in "
                           f"{cfg.valid_sensors}.")
            continue

        # ========== Derive the site folder from the parsed metadata ==========
        site_dir = (pathlib.Path(parsed["root"]) / parsed["node"]
                    / parsed["project"] / parsed["site_folder"])

        # ========== Locate the sibling DSM / DTM rasters ==========
        dsm = _single_sibling(las, "*_LiDAR_DSM_*.tif", issues)
        dtm = _single_sibling(las, "*_LiDAR_DTM_*.tif", issues)

        # ========== Build the stable output name ==========
        stem_parts = ["PE_LIDAR_points"]
        if gpro_nu is not None:
            stem_parts.append(f"gpro{gpro_nu}")
        if args.plot_variant:
            stem_parts.append(args.plot_variant)
        stem = "_".join(stem_parts)
        suffix = ("_" + "_".join(stem_parts[1:])) if stem_parts[1:] else ""
        dirs = pex.plotextract_dirs(run_dir / "T1_proc")
        # parquet -> dataset directory of chunk parts; csv -> single flat file
        if args.type == "csv":
            outfile = dirs["pixel"] / f"{stem}.csv"
        else:
            outfile = dirs["pixel"] / stem

        jobs.append({
            "las": las,
            "dsm": dsm,
            "dtm": dtm,
            "outfile": outfile,
            "metadata_outfile": dirs["pixel"] / f"{stem}_metadata.yaml",
            "metrics_file": dirs["plot"] / f"PE_LIDAR_plot_metrics{suffix}.parquet",
            "percentiles_file": dirs["plot"] / f"PE_LIDAR_plot_percentiles{suffix}.parquet",
            "site_dir": site_dir,
            "gpro_nu": gpro_nu,
            "issues": issues,
            "node": parsed["node"],
            "project": parsed["project"],
            "site": parsed["site_folder"],
            "sensor": parsed["sensor"],
            "date": parsed["date"],
            "run": parsed["run_folder"],
        })
    return jobs


# ==================================================================================
def process_lidar(
        job: Dict[str, Any],
        plotshp: gpd.GeoDataFrame,
        cfg: PEConfig,
        args: argparse.Namespace,
        repo: Optional[git.Repo],
    ) -> Dict[str, Any]:
    """Extract one point cloud into a per-plot parquet dataset (cached).

    Skips the extraction when the provenance sidecar (written last, so
    it marks completion) is newer than all of the inputs (point cloud,
    DSM/DTM rasters, plot file) and the output exists, unless
    ``--force`` is set. The per-plot ``Delta_z`` metrics table is then
    derived from the saved dataset (parquet only) and refreshed
    independently when stale.

    Parameters
    ----------
    job : dict
        Job dict from :func:`locate_lidar_runs`.
    plotshp : geopandas.GeoDataFrame
        Validated plot polygons (``plot_id`` column, CRS set); the source
        path rides on ``plotshp.attrs['plot_file']``.
    cfg : PEConfig
        Tunable settings (chunk size).
    args : argparse.Namespace
        Parsed command-line arguments (``force``, ``type``,
        ``full_percentiles``).
    repo : git.Repo or None
        Repository handle for the provenance sidecar.

    Returns
    -------
    dict
        Summary row for :func:`_print_run_summary`.
    """
    plot_file = plotshp.attrs["plot_file"]
    inputs = [job["las"], plot_file] + [p for p in (job["dsm"], job["dtm"]) if p]
    # The sidecar is written last, so it is the completion marker + mtime anchor.
    have_out = (job["outfile"].is_file() if args.type == "csv"
                else len(pex.dataset_parts(job["outfile"])) > 0)
    if (not args.force and have_out
            and cf.outputs_up_to_date([job["metadata_outfile"]], inputs)):
        n = _row_count(job["outfile"], args.type)
        if args.type == "csv" or _plot_metrics_fresh(job, args):
            return _summary_row(job, "cached", None, n_points=n)
        # +++++ Raw dataset cached but the metrics table is stale +++++
        metric_issues = write_plot_metrics(job, plotshp, args)
        return _summary_row(job, "metrics_refreshed",
                            "; ".join(metric_issues) or None, n_points=n)

    tqdm.write(f"Processing {job['las'].name} started at {pd.Timestamp.now()}.")

    # ========== Stream the clipped + sampled chunks to disk ==========
    run_meta = {k: job[k] for k in ["node", "project", "site", "sensor",
                                    "date", "run"]}
    if job["gpro_nu"] is not None:
        run_meta["gpro_nu"] = job["gpro_nu"]
    n_points, n_plots, issues = extract_plot_points(
        job["las"], plotshp, job["outfile"], dtm=job["dtm"], dsm=job["dsm"],
        run_meta=run_meta, chunk_points=cfg.chunk_points, file_type=args.type)
    issues = job["issues"] + issues
    if n_points == 0:
        issues.append("No points fell inside any plot polygon; check the plot "
                      "file and point-cloud coverage.")
        return _summary_row(job, "skipped", "; ".join(issues))

    # ========== Provenance sidecar (written last: completion marker) ==========
    meta = cf.build_run_metadata(
        {**{k: job[k] for k in ["las", "dsm", "dtm", "outfile", "gpro_nu",
                                "node", "project", "site", "sensor", "date", "run"]},
         "plot_file": plot_file,
         "n_points": n_points,
         "n_plots_with_points": n_plots,
         "n_plots_total": len(plotshp),
         "issues": issues},
        script_path=__file__, repo=repo)
    cf.write_metadata_yaml(meta, job["metadata_outfile"])

    # ========== Per-plot Delta_z metrics from the saved dataset ==========
    if args.type == "parquet":
        issues += write_plot_metrics(job, plotshp, args)

    status = "extracted" if not issues else "extracted_with_issues"
    return _summary_row(job, status, "; ".join(issues) or None,
                        n_points=n_points, n_plots=n_plots)


# ==================================================================================
def _plot_metrics_fresh(job: Dict[str, Any], args: argparse.Namespace) -> bool:
    """Check whether the plot-metrics outputs are newer than the dataset.

    Parameters
    ----------
    job : dict
        Job dict from :func:`locate_lidar_runs`.
    args : argparse.Namespace
        Parsed command-line arguments (``full_percentiles``).

    Returns
    -------
    bool
        True when the metrics table (and, with ``--full-percentiles``,
        the percentile table) is newer than the dataset sidecar.
    """
    fresh = (job["metrics_file"].is_file()
             and cf.outputs_up_to_date([job["metrics_file"]],
                                       [job["metadata_outfile"]]))
    if args.full_percentiles:
        fresh = (fresh and job["percentiles_file"].is_file()
                 and cf.outputs_up_to_date([job["percentiles_file"]],
                                           [job["metadata_outfile"]]))
    return fresh


# ==================================================================================
def write_plot_metrics(
        job: Dict[str, Any],
        plotshp: gpd.GeoDataFrame,
        args: argparse.Namespace,
    ) -> List[str]:
    """Derive and save the per-plot ``Delta_z`` metrics table(s).

    Loads only the ``plot_id``/``Delta_z`` columns of the saved point
    dataset (plots span scan-chunk parts, so a per-part pass cannot
    aggregate per plot) and computes the shared DS03 statistic set per
    plot, plus the full 0-100 percentile profile when
    ``--full-percentiles`` is set. Run metadata and any trial-info
    columns on *plotshp* are attached to the metrics table.

    Parameters
    ----------
    job : dict
        Job dict from :func:`locate_lidar_runs`.
    plotshp : geopandas.GeoDataFrame
        Validated plot polygons (plus trial-info columns when joined).
    args : argparse.Namespace
        Parsed command-line arguments (``full_percentiles``).

    Returns
    -------
    list of str
        Issues encountered (missing ``Delta_z`` column, no finite
        heights); empty on success.
    """
    parts = pex.dataset_parts(job["outfile"])
    if "Delta_z" not in pq.read_schema(parts[0]).names:
        return ["Point dataset has no Delta_z column (DTM missing); "
                "plot metrics skipped."]
    print(f"Computing plot metrics from {job['outfile'].name} ...")
    pdf = pd.read_parquet(job["outfile"], columns=["plot_id", "Delta_z"])
    pdf = pdf[np.isfinite(pdf["Delta_z"])]
    if pdf.empty:
        return ["No finite Delta_z values in the point dataset; "
                "plot metrics skipped."]

    metrics = pex.group_value_stats(pdf, ["plot_id"], value_col="Delta_z")
    # variable column keeps the schema stable for future height definitions
    metrics.insert(1, "variable", "Delta_z")
    for key in ["node", "project", "site", "sensor", "date", "run"]:
        metrics[key] = job[key]
    if job["gpro_nu"] is not None:
        metrics["gpro_nu"] = job["gpro_nu"]
    trial_cols = [c for c in plotshp.columns
                  if c not in ("geometry",) and c != "plot_id"]
    if trial_cols:
        metrics = metrics.merge(
            pd.DataFrame(plotshp[["plot_id"] + trial_cols]),
            on="plot_id", how="left")
    job["metrics_file"].parent.mkdir(parents=True, exist_ok=True)
    metrics.to_parquet(job["metrics_file"], index=False, compression="zstd")

    # +++++ Full percentile profile (own table; joined via plot_id) +++++
    if args.full_percentiles:
        pctl = pex.group_value_percentiles(pdf, ["plot_id"],
                                           value_col="Delta_z")
        pctl.insert(1, "variable", "Delta_z")
        for key in ["node", "project", "site", "sensor", "date", "run"]:
            pctl[key] = job[key]
        if job["gpro_nu"] is not None:
            pctl["gpro_nu"] = job["gpro_nu"]
        pctl.to_parquet(job["percentiles_file"], index=False,
                        compression="zstd")
    return []


# ==================================================================================
def extract_plot_points(
        las_path: pathlib.Path,
        plotshp: gpd.GeoDataFrame,
        outfile: pathlib.Path,
        dtm: Optional[pathlib.Path],
        dsm: Optional[pathlib.Path],
        run_meta: Dict[str, Any],
        chunk_points: int = 4_000_000,
        file_type: str = "parquet",
    ) -> Tuple[int, int, List[str]]:
    """Stream in-plot points (with DTM/DSM sampling) to a parquet dataset.

    Each laspy chunk is pre-filtered against the plot file's total
    bounds with a cheap numpy box test before the exact
    point-in-polygon spatial join (QA00 bbox-first lesson applied to
    points), sampled against the surface rasters, tagged with the run
    metadata, and written straight to its own part file — peak memory
    is one chunk regardless of point-cloud size. Parts are written
    atomically (``.tmp`` then rename); stale parts from a previous run
    are cleared first because chunk numbering restarts.

    Parameters
    ----------
    las_path : pathlib.Path
        Path to the ``.las``/``.laz`` point cloud.
    plotshp : geopandas.GeoDataFrame
        Validated plot polygons with a ``plot_id`` column.
    outfile : pathlib.Path
        Dataset directory (``file_type="parquet"``) or flat file path
        (``file_type="csv"``).
    dtm : pathlib.Path or None
        DTM raster path (None when missing).
    dsm : pathlib.Path or None
        DSM raster path (None when missing).
    run_meta : dict of str to Any
        Constant metadata columns added to every row.
    chunk_points : int, optional
        Points per laspy chunk. Default 4,000,000.
    file_type : str, optional
        ``"parquet"`` (dataset directory) or ``"csv"`` (single file).

    Returns
    -------
    int
        Points written across all chunks (0 when the CRS could not be
        resolved or nothing fell inside a plot).
    int
        Plots that received points.
    list of str
        Issues encountered (CRS mismatch note, missing rasters, ...).
    """
    issues: List[str] = []
    n_points = 0
    plot_ids: set = set()
    with laspy.open(las_path) as reader:
        crs = reader.header.parse_crs()
        if crs is None:
            issues.append(f"Point cloud {las_path} has no CRS; cannot "
                          "relate it to the plot file.")
            return 0, 0, issues
        if plotshp.crs.to_epsg() != crs.to_epsg():  # type: ignore[union-attr]
            issues.append(
                f"Plot file CRS (EPSG:{plotshp.crs.to_epsg()}) differs from the "  # type: ignore[union-attr]
                f"point cloud (EPSG:{crs.to_epsg()}); plots reprojected.")
            plots = plotshp.to_crs(crs)
        else:
            plots = plotshp
        minx, miny, maxx, maxy = plots.total_bounds
        plots = plots[["plot_id", "geometry"]]

        # +++++ Open the surface rasters once for the whole run +++++
        rasters, raster_issues = _open_surface_rasters(dtm, dsm)
        issues += raster_issues

        # +++++ Clear stale parts: chunk numbering restarts every run +++++
        if file_type == "parquet" and outfile.is_dir():
            for old in outfile.glob("*.parquet*"):
                old.unlink()
        outfile.parent.mkdir(parents=True, exist_ok=True)
        csv_tmp = outfile.with_suffix(".csv.tmp")

        part_nu = 0
        try:
            n_chunks = int(np.ceil(reader.header.point_count / chunk_points))
            for points in tqdm(reader.chunk_iterator(chunk_points),
                               total=n_chunks, desc=las_path.stem, leave=False):
                x = np.asarray(points.x)
                y = np.asarray(points.y)
                # +++++ Cheap bbox pre-filter before any geometry work +++++
                box = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
                if not box.any():
                    continue
                cand = gpd.GeoDataFrame(
                    {"x": x[box], "y": y[box], "z": np.asarray(points.z)[box]},
                    geometry=gpd.points_from_xy(x[box], y[box]), crs=crs)
                joined = gpd.sjoin(cand, plots, how="inner", predicate="within")
                if joined.empty:
                    continue
                chunk = pd.DataFrame(
                    joined[["plot_id", "x", "y", "z"]].reset_index(drop=True))
                chunk = _sample_rasters(chunk, rasters)
                for key, val in run_meta.items():
                    chunk[key] = val
                if file_type == "csv":
                    chunk.to_csv(csv_tmp, mode="a", index=False,
                                 header=(n_points == 0))
                else:
                    pex.write_dataset_part(
                        chunk, outfile / f"part_{part_nu:04d}.parquet")
                n_points += len(chunk)
                plot_ids.update(chunk["plot_id"].unique())
                part_nu += 1
        finally:
            for ras in rasters.values():
                ras.close()
        if file_type == "csv" and n_points > 0:
            os.replace(csv_tmp, outfile)
    return n_points, len(plot_ids), issues


# ==================================================================================
def _open_surface_rasters(
        dtm: Optional[pathlib.Path],
        dsm: Optional[pathlib.Path],
    ) -> Tuple[Dict[str, Any], List[str]]:
    """Lazily open the DTM/DSM rasters for repeated per-chunk sampling.

    Parameters
    ----------
    dtm : pathlib.Path or None
        DTM raster path (None when missing).
    dsm : pathlib.Path or None
        DSM raster path (None when missing).

    Returns
    -------
    dict of str to xarray.DataArray
        Opened, band-squeezed rasters keyed ``"DTM"``/``"DSM"`` (missing
        ones omitted). Caller closes them.
    list of str
        One issue per missing raster.
    """
    rasters: Dict[str, Any] = {}
    issues: List[str] = []
    for name, ras_path in [("DTM", dtm), ("DSM", dsm)]:
        if ras_path is None:
            issues.append(f"No {name} raster found; {name} column omitted.")
            continue
        rasters[name] = rioxarray.open_rasterio(
            ras_path, masked=True).squeeze("band", drop=True)  # type: ignore[union-attr]
    return rasters, issues


# ==================================================================================
def _sample_rasters(
        points: pd.DataFrame,
        rasters: Dict[str, Any],
    ) -> pd.DataFrame:
    """Sample the opened rasters at every point and compute canopy height.

    Uses vectorised nearest-neighbour selection (only the touched blocks
    are read from disk). Adds ``DTM``, ``DSM`` and ``Delta_z = z - DTM``
    columns where available.

    Parameters
    ----------
    points : pandas.DataFrame
        Point table with ``x``, ``y``, ``z`` columns (raster CRS assumed
        to match the point cloud).
    rasters : dict of str to xarray.DataArray
        Opened rasters from :func:`_open_surface_rasters`.

    Returns
    -------
    pandas.DataFrame
        The point table with the added columns.
    """
    if not rasters:
        return points
    x_idx = xr.DataArray(points["x"].to_numpy(), dims="points")
    y_idx = xr.DataArray(points["y"].to_numpy(), dims="points")
    for name, ras in rasters.items():
        vals = ras.sel(x=x_idx, y=y_idx, method="nearest").to_numpy()
        points[name] = vals
        if name == "DTM":
            points["Delta_z"] = points["z"] - vals
    return points


# ==================================================================================
def _single_sibling(
        las: pathlib.Path,
        pattern: str,
        issues: List[str],
    ) -> Optional[pathlib.Path]:
    """Return the single sibling file matching *pattern*, recording issues.

    Parameters
    ----------
    las : pathlib.Path
        The point-cloud path whose ``products/`` folder is searched.
    pattern : str
        Glob pattern (e.g. ``"*_LiDAR_DSM_*.tif"``).
    issues : list of str
        Issue list to append to when zero or multiple matches are found.

    Returns
    -------
    pathlib.Path or None
        The first match, or None when absent.
    """
    matches = sorted(las.parent.glob(pattern))
    if len(matches) == 0:
        issues.append(f"Missing file matching '{pattern}' beside {las.name}.")
        return None
    if len(matches) > 1:
        issues.append(f"Multiple files match '{pattern}' beside {las.name} "
                      f"({[m.name for m in matches]}); using {matches[0].name}.")
    return matches[0]


# ==================================================================================
def _row_count(outfile: pathlib.Path, file_type: str) -> Optional[int]:
    """Return the row count of an existing output table.

    Parameters
    ----------
    outfile : pathlib.Path
        The output dataset directory (parquet) or file path (csv).
    file_type : str
        ``"parquet"`` or ``"csv"``.

    Returns
    -------
    int or None
        Row count, or None when the output cannot be read.
    """
    if file_type == "parquet":
        return pex.dataset_row_count(outfile)
    try:
        return len(pd.read_csv(outfile, usecols=[0]))
    except (OSError, ValueError):
        return None


# ==================================================================================
def _try_repo(path: pathlib.Path) -> Optional[git.Repo]:
    """Return the git repo containing *path*, or None outside any repo.

    Parameters
    ----------
    path : pathlib.Path
        Any path inside the candidate repository.

    Returns
    -------
    git.Repo or None
    """
    try:
        return git.Repo(path, search_parent_directories=True)
    except git_exc.InvalidGitRepositoryError:
        return None


# ==================================================================================
def _summary_row(
        job: Dict[str, Any],
        status: str,
        reason: Optional[str],
        n_points: Optional[int] = None,
        n_plots: Optional[int] = None,
    ) -> Dict[str, Any]:
    """Build one end-of-run summary row.

    Parameters
    ----------
    job : dict
        Job dict from :func:`locate_lidar_runs`.
    status : str
        Outcome label (``extracted``, ``cached``, ``skipped``, ...).
    reason : str or None
        Issue text for skipped/qualified rows.
    n_points : int, optional
        Points extracted.
    n_plots : int, optional
        Plots that received points.

    Returns
    -------
    dict
        Row for :func:`_print_run_summary`.
    """
    date = job.get("date")
    return ({
        "project": job.get("project"),
        "sensor": job.get("sensor"),
        "date": date.strftime("%Y-%m-%d") if date is not None and pd.notna(date) else None,
        "run": job.get("run"),
        "n_points": n_points,
        "n_plots": n_plots,
        "status": status,
        "reason": reason,
    })


# ==================================================================================
def _print_run_summary(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """Print the REPORTED/SKIPPED summary tables and return the DataFrame.

    Parameters
    ----------
    rows : list of dict
        Rows from :func:`_summary_row`.

    Returns
    -------
    pandas.DataFrame
        The full summary with a fixed column order.
    """
    columns = ["project", "sensor", "date", "run", "n_points", "n_plots",
               "status", "reason"]
    df = pd.DataFrame(rows, columns=columns)
    if df.empty:
        print("\nNo runs to summarise.")
        return df

    disp = df.copy()
    for col in ["n_points", "n_plots"]:
        disp[col] = disp[col].apply(
            lambda v: "" if v is None or pd.isna(v) else f"{int(v)}")
    disp["reason"] = disp["reason"].fillna("")

    skipped = disp[disp["status"] == "skipped"]
    reported = disp[disp["status"] != "skipped"]
    if not skipped.empty:
        print(f"\nSKIPPED ({len(skipped)}):")
        print(skipped[["project", "sensor", "date", "run", "status", "reason"]
                      ].to_string(index=False))
    if not reported.empty:
        print(f"\nREPORTED ({len(reported)}):")
        print(reported.to_string(index=False))
    return df


# ==================================================================================
if __name__ == '__main__':
    # ========== Parse args ==========
    parser = argparse.ArgumentParser(
        description="Extract per-plot LiDAR points (with DTM/DSM sampling) from GRYFN point clouds.")
    parser.add_argument("--path", type=str, default=None, help="The folder to crawl for LiDAR products. By default it will search from the root dir of the git repo.")
    parser.add_argument("--plot-variant", type=str, default=None, help="Select a plot-file variant ({YYYYSiteName}_plots_{variant}[_vNN].geojson) instead of the mandatory main plot file. See the Plot_Layout spec (wiki Key-Files).")
    parser.add_argument("--join-trial-info", default=False, action="store_true", help="Join Documentation/Trial_Info/{YYYYSiteName}_trial_info.csv onto the plots via plot_id; the trial columns are carried into the output tables.")
    parser.add_argument("--full-percentiles", default=False, action="store_true", help="Also write the full 0-100 percentile profile of Delta_z per plot to PlotLevel/PE_LIDAR_plot_percentiles[...].parquet (long format; joined via plot_id).")
    parser.add_argument("-f", "--force", default=False, action="store_true", help="Force the re-creation of output files even if they are up to date. Default is to skip files that are newer than their inputs.")
    parser.add_argument("--type", type=str, default="parquet", choices=["parquet", "csv"], help="Output table format. Default parquet.")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[], help="One or more directory names to exclude from the crawl. e.g. --exclude-dir 2025_TestData")
    parser.add_argument("--allow-multi-gpro", default=False, action="store_true", help="Process runs that contain more than one .gpro folder instead of skipping them. Debugging only; outputs get a _gproN suffix.")
    parser.add_argument("-v", "--verbose", default=False, action="store_true", help="Enable verbose output.")
    args = parser.parse_args()

    # +++++ Resolve the search path: --path wins, else the cwd's git root +++++
    if args.path is not None:
        search_root = args.path
    else:
        try:
            search_root = git.Repo(
                os.getcwd(), search_parent_directories=True
            ).git.rev_parse("--show-toplevel")
        except git_exc.InvalidGitRepositoryError as err:
            raise git_exc.InvalidGitRepositoryError(
                f"This script was called from an unknown path ({os.getcwd()}). "
                "Must be in a git repo or provide a valid --path argument."
            ) from err

    # ========= Check if the provided path exists ==========
    # Resolve to absolute BEFORE chdir, otherwise a relative --path breaks.
    path = pathlib.Path(search_root).resolve()
    if not path.is_dir():
        raise NotADirectoryError(f"The provided path does not exist: {path}")
    os.chdir(path)

    # ========== Parse Args to main function ==========
    cf.check_environment(_git_root)
    main(args, path)
