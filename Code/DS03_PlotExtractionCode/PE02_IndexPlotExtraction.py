"""Spectral-index plot extraction — per-plot index aggregates from SI00 maps (DS03).

This script crawls the APPN dataset structure for spectral-index maps
produced by DS05/SI00 (``<run>/T1_proc/SpectralIndices/SI_*_report.json``
manifests + their NetCDF/GeoTIFF index maps) and extracts per-plot
aggregate statistics for every index using the site's mandatory
Plot_Layout file
(``Documentation/Plot_Layout/{YYYYSiteName}_plots.geojson``).

Per manifest the outputs land in ``<run>/T1_proc/PlotExtracts/``:

- ``PixelLevel/PE_INDEX_{REGION}_{METHOD}_pixels[…]/`` — long-format
  per-pixel parquet *dataset*: one zstd part file per plot
  (``plot_id``, ``index``, ``value``), readable as one table by any
  parquet dataset reader. The YAML sidecar beside the directory is
  written last and doubles as the completion marker, so extraction is
  cached on it and an interrupted run resumes at the first
  missing/stale plot.
- ``PlotLevel/PE_INDEX_{REGION}_{METHOD}_plot_metrics[…].parquet`` —
  long-format trait table (one row per plot x index:
  count/mean/std/var/min/max/median/skew/kurtosis/normality/p01-p99
  short percentiles/valid_fraction plus run metadata), derived
  from the saved pixel dataset, with its own YAML sidecar.
- ``PlotLevel/PE_INDEX_{REGION}_{METHOD}_plot_percentiles[…].parquet``
  — only with ``--full-percentiles``: long-format full 0-100
  percentile profile per plot x index.
- ``Reports/`` — a markdown overview report with embedded QC figures.

PE02 never opens the ``.bin`` orthomosaics — the raster boundary is
SI00's (DS05 computes maps, DS03 extracts plots).

Index maps are read per plot through bounding-box windows (``clip_box``
then ``clip``, the PE01-benchmarked pattern); the NetCDF is opened with
``decode_coords="all"`` so the CRS rides on ``spatial_ref``. Extraction
is cached via ``outputs_up_to_date`` on the sidecars (inputs = index
maps + manifest + plot file); use ``--force`` to override.

Command-line Arguments
----------------------
--path : str, optional
    Folder to crawl for SI00 manifests. Defaults to the git repo root.
--plot-variant : str, optional
    Select a plot-file variant (``{YYYYSiteName}_plots_{variant}[_vNN]``)
    instead of the mandatory main plot file.
--join-trial-info : flag
    Join ``Documentation/Trial_Info/{YYYYSiteName}_trial_info.csv`` onto
    the plots via ``plot_id`` (columns carried into the trait table).
--indices : str [str ...], optional
    Restrict extraction to these indices (default: all in the manifest).
    Changing the restriction does not invalidate existing per-plot
    parts — combine with ``--force``.
--full-percentiles : flag
    Also write the full 0-100 percentile profile per plot x index to
    its own long-format parquet table.
--force : flag
    Re-create output files even when they are up to date.
"""

# ==============================================================================

__title__ = "Spectral-index plot extraction"
__author__ = "Arden Burrell"
__version__ = "v1.2(19.08.2026)"
__email__ = "arden.burrell@sydney.edu.au"

# ==============================================================================
# ========== Import core packages ==========
import os
import sys
import json
import argparse
import pathlib
from dataclasses import dataclass
from typing import Dict, List, Any, Tuple, Optional

# ========== Import other packages ==========
import git
from git import exc as git_exc
import numpy as np
import pandas as pd
import xarray as xr
import rioxarray  # noqa: F401 — registers the .rio accessor
import geopandas as gpd
from tqdm import tqdm
import warnings as warn
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

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
class PE02Config:
    """Tunable settings for one index plot-extraction invocation.

    Attributes
    ----------
    valid_sensors : tuple of str
        Sensor platform folder names handled by this script.
    si_dirname : str
        Name of the SI00 output folder inside ``T1_proc/``.
    figures_dirname : str
        Name of the figure folder inside the reports folder.
    headline_index : str
        Index used for the per-plot choropleth figure when present.
    """
    valid_sensors: Tuple[str, ...] = ("GOBI", "CALVIS")
    si_dirname: str = "SpectralIndices"
    figures_dirname: str = "PE_figures"
    headline_index: str = "NDVI"


def default_config() -> PE02Config:
    """Return the default :class:`PE02Config` for this tool."""
    return PE02Config()


# ==================================================================================
def main(args: argparse.Namespace, path: pathlib.Path) -> pd.DataFrame:
    """Run the spectral-index plot-extraction pipeline.

    Locates SI00 manifests under *path*, resolves and validates each
    site's Plot_Layout file, extracts per-plot aggregates for every
    index map, writes trait tables + a per-manifest markdown report, and
    prints a REPORTED/SKIPPED summary.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments.
    path : pathlib.Path
        Root directory to crawl.

    Returns
    -------
    pandas.DataFrame
        One row per located manifest summarising the outcome.
    """
    cfg = default_config()

    # ========== Locate the SI00 manifests ==========
    jobs = locate_si_manifests(path, cfg, args)

    # ========== Load each site's plot file once ==========
    plots_by_site = pl.load_site_plots(
        (j["site_dir"] for j in jobs),
        variant=args.plot_variant, join_trial_info=args.join_trial_info)

    # ========== Extract every manifest (cached) ==========
    summary_rows: List[Dict[str, Any]] = []
    repo = _try_repo(path)
    for job in tqdm(jobs, desc="Processing SI manifests"):
        plotshp, plot_issues = plots_by_site[job["site_dir"]]
        if plotshp is None:
            summary_rows.append(_summary_row(job, "skipped", "; ".join(plot_issues)))
            continue
        summary_rows.append(process_manifest(job, plotshp, cfg, args, repo))

    # ========== Print the end-of-run summary ==========
    summary = _print_run_summary(summary_rows)
    return summary


# ==================================================================================
def locate_si_manifests(
        path: pathlib.Path,
        cfg: PE02Config,
        args: argparse.Namespace,
    ) -> List[Dict[str, Any]]:
    """Find SI00 report manifests and assemble one job dict per manifest.

    Recursively searches *path* for
    ``T1_proc/SpectralIndices/SI_*_report.json``, parses each manifest
    and the APPN folder metadata, and resolves the index-map paths.

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively.
    cfg : PE02Config
        Tunable settings (folder names, valid sensors).
    args : argparse.Namespace
        Parsed command-line arguments (``exclude_dir``, ``plot_variant``,
        ``verbose``).

    Returns
    -------
    list of dict
        One dict per manifest with keys ``manifest``, ``report``,
        ``index_maps`` (list of paths), ``region``, ``method``,
        ``gpro_nu``, ``site_dir``, output paths, and the parsed run
        metadata (``node``, ``project``, ``site``, ``sensor``, ``date``,
        ``run``).

    Raises
    ------
    ValueError
        If no manifests are found under *path*.
    """
    print(f"Scanning {path} for SI00 manifests. {pd.Timestamp.now()}")
    manifests = sorted(f for f in path.rglob("SI_*_report.json")
                       if f.parent.name == cfg.si_dirname
                       and f.parents[1].name == "T1_proc")
    if args.exclude_dir:
        exclude_set = set(args.exclude_dir)
        manifests = [f for f in manifests
                     if not (set(p.name for p in f.parents) & exclude_set)]
    if len(manifests) == 0:
        raise ValueError(
            f"No SI00 manifests found in {path}. Expected files matching "
            "<run>/T1_proc/SpectralIndices/SI_*_report.json — run "
            "DS05/SI00_SpectralIndices.py first.")

    jobs: List[Dict[str, Any]] = []
    for manifest in manifests:
        report = json.loads(manifest.read_text(encoding="utf-8"))
        parsed = cf.parse_APPN_dataset_path(manifest)
        if not parsed["valid"]:
            warn.warn(f"Could not parse APPN metadata for {manifest}: "
                      f"{parsed['errors']}. Skipping manifest.")
            continue
        if parsed["sensor"] not in cfg.valid_sensors:
            if args.verbose:
                tqdm.write(f"Skipping {manifest}: sensor {parsed['sensor']} "
                           f"not in {cfg.valid_sensors}.")
            continue

        index_maps = [manifest.parent / m for m in report.get("index_maps", [])]
        missing = [m.name for m in index_maps if not m.is_file()]
        if missing:
            warn.warn(f"Manifest {manifest} references missing index maps "
                      f"{missing}. Skipping manifest.")
            continue

        site_dir = (pathlib.Path(parsed["root"]) / parsed["node"]
                    / parsed["project"] / parsed["site_folder"])
        dirs = pex.plotextract_dirs(manifest.parents[1])

        region = report.get("region", "unknown")
        method = report.get("method", "unknown")
        gpro_nu = report.get("gpro_nu")
        suffix_parts: List[str] = []
        if gpro_nu is not None:
            suffix_parts.append(f"gpro{gpro_nu}")
        if args.plot_variant:
            suffix_parts.append(args.plot_variant)
        suffix = ("_" + "_".join(suffix_parts)) if suffix_parts else ""
        stem = f"PE_INDEX_{region}_{method}{suffix}"

        outfile = dirs["plot"] / f"{stem}_plot_metrics.parquet"
        jobs.append({
            "manifest": manifest,
            "report": report,
            "index_maps": index_maps,
            "region": region,
            "method": method,
            "gpro_nu": gpro_nu,
            "site_dir": site_dir,
            "extracts_dir": dirs["extracts"],
            "figures_dir": dirs["reports"] / cfg.figures_dirname,
            "outfile": outfile,
            "percentiles_file": dirs["plot"] / f"{stem}_plot_percentiles.parquet",
            "metadata_outfile": outfile.with_name(f"{outfile.stem}_metadata.yaml"),
            # pixel_dataset is a parquet dataset *directory* (one part per plot)
            "pixel_dataset": dirs["pixel"] / f"{stem}_pixels",
            "pixel_metadata_outfile": dirs["pixel"] / f"{stem}_pixels_metadata.yaml",
            "report_file": dirs["reports"] / f"{stem}_report.md",
            "node": parsed["node"],
            "project": parsed["project"],
            "site": parsed["site_folder"],
            "sensor": parsed["sensor"],
            "date": parsed["date"],
            "run": parsed["run_folder"],
        })
    return jobs


# ==================================================================================
def process_manifest(
        job: Dict[str, Any],
        plotshp: gpd.GeoDataFrame,
        cfg: PE02Config,
        args: argparse.Namespace,
        repo: Optional[git.Repo],
    ) -> Dict[str, Any]:
    """Extract one manifest's maps into pixel + trait tables (cached).

    Writes the per-plot pixel dataset first (resuming at missing/stale
    parts), then its sidecar (the completion marker), then derives the
    trait table from the saved dataset. Fully skipped when both the
    pixel sidecar and the trait table are newer than all inputs (index
    maps, manifest, plot file) unless ``--force`` is set.

    Parameters
    ----------
    job : dict
        Job dict from :func:`locate_si_manifests`.
    plotshp : geopandas.GeoDataFrame
        Validated plot polygons; source path in ``.attrs['plot_file']``.
    cfg : PE02Config
        Tunable settings.
    args : argparse.Namespace
        Parsed command-line arguments (``force``, ``indices``,
        ``full_percentiles``, ``skipplot``).
    repo : git.Repo or None
        Repository handle for the provenance sidecar.

    Returns
    -------
    dict
        Summary row for :func:`_print_run_summary`.
    """
    plot_file = plotshp.attrs["plot_file"]
    inputs = job["index_maps"] + [job["manifest"], plot_file]
    # The pixel sidecar is written last, so it is the completion marker.
    fresh = (not args.force
             and len(pex.dataset_parts(job["pixel_dataset"])) > 0
             and cf.outputs_up_to_date([job["pixel_metadata_outfile"]], inputs)
             and job["outfile"].is_file()
             and cf.outputs_up_to_date([job["outfile"]],
                                       [job["pixel_metadata_outfile"]]))
    if args.full_percentiles:
        fresh = (fresh and job["percentiles_file"].is_file()
                 and cf.outputs_up_to_date([job["percentiles_file"]],
                                           [job["pixel_metadata_outfile"]]))
    if fresh:
        metrics = pd.read_parquet(job["outfile"])
        return _summary_row(job, "cached", None,
                            n_indices=int(metrics["index"].nunique()),
                            n_plots=int(metrics["plot_id"].nunique()))

    tqdm.write(f"Extracting {job['manifest'].name} started at {pd.Timestamp.now()}.")

    # ========== Per-pixel extraction (per-plot parts; resumes) ==========
    n_plots_px, n_reused, issues = extract_index_pixels(
        job["index_maps"], plotshp, job["pixel_dataset"], inputs,
        indices=args.indices, force=args.force)
    if n_plots_px == 0:
        issues.append("No plots yielded pixels from the index maps; check the "
                      "plot file and map coverage.")
        return _summary_row(job, "skipped", "; ".join(issues))

    # ========== Pixel-dataset sidecar (written last: completion marker) ==========
    pixel_meta = cf.build_run_metadata(
        {**{k: job[k] for k in ["manifest", "index_maps", "region", "method",
                                "gpro_nu", "pixel_dataset",
                                "node", "project", "site", "sensor", "date", "run"]},
         "plot_file": plot_file,
         "n_plots_with_pixels": n_plots_px,
         "n_plots_reused": n_reused,
         "n_plots_total": len(plotshp),
         "issues": issues},
        script_path=__file__, repo=repo)
    cf.write_metadata_yaml(pixel_meta, job["pixel_metadata_outfile"])

    # ========== Metrics from the saved pixel dataset ==========
    metrics, percentiles = compute_index_metrics(
        job["pixel_dataset"], full_percentiles=args.full_percentiles)

    # ========== Attach run metadata (+ optional trial-info columns) ==========
    for key in ["node", "project", "site", "sensor", "date", "run"]:
        metrics[key] = job[key]
    metrics["EM_Region"] = job["region"]
    metrics["method"] = job["method"]
    if job["gpro_nu"] is not None:
        metrics["gpro_nu"] = job["gpro_nu"]
    trial_cols = [c for c in plotshp.columns if c not in ("geometry", "plot_id")]
    if trial_cols:
        metrics = metrics.merge(
            pd.DataFrame(plotshp[["plot_id"] + trial_cols]),
            on="plot_id", how="left")

    # ========== Save the table + provenance sidecar ==========
    job["outfile"].parent.mkdir(parents=True, exist_ok=True)
    metrics.to_parquet(job["outfile"], index=False, compression="zstd")

    # +++++ Full percentile profile (own table; joined via plot_id) +++++
    if percentiles is not None:
        for key in ["node", "project", "site", "sensor", "date", "run"]:
            percentiles[key] = job[key]
        percentiles["EM_Region"] = job["region"]
        percentiles["method"] = job["method"]
        if job["gpro_nu"] is not None:
            percentiles["gpro_nu"] = job["gpro_nu"]
        percentiles.to_parquet(job["percentiles_file"], index=False,
                               compression="zstd")
    meta = cf.build_run_metadata(
        {**{k: job[k] for k in ["manifest", "index_maps", "region", "method",
                                "gpro_nu", "outfile",
                                "node", "project", "site", "sensor", "date", "run"]},
         "plot_file": plot_file,
         "n_indices": int(metrics["index"].nunique()),
         "n_plots_with_pixels": int(metrics["plot_id"].nunique()),
         "n_plots_total": len(plotshp),
         "issues": issues},
        script_path=__file__, repo=repo)
    cf.write_metadata_yaml(meta, job["metadata_outfile"])

    # ========== Markdown report + figures ==========
    write_manifest_report(job, metrics, plotshp, cfg, args)

    status = "extracted" if not issues else "extracted_with_issues"
    return _summary_row(job, status, "; ".join(issues) or None,
                        n_indices=int(metrics["index"].nunique()),
                        n_plots=int(metrics["plot_id"].nunique()))


# ==================================================================================
def extract_index_pixels(
        index_maps: List[pathlib.Path],
        plotshp: gpd.GeoDataFrame,
        dataset_dir: pathlib.Path,
        inputs: List[pathlib.Path],
        indices: Optional[List[str]] = None,
        force: bool = False,
    ) -> Tuple[int, int, List[str]]:
    """Extract per-plot per-pixel index values into a parquet dataset.

    Every map is opened once; each plot polygon is then read through
    its own bounding-box window (``clip_box`` then ``clip`` — the
    PE01-benchmarked pattern) across all maps, and the plot's pixel
    values for every index are written to one zstd part file
    (atomically: ``.tmp`` then rename). A plot whose part is already
    newer than every input is skipped without touching the rasters, so
    an interrupted run resumes. Orphan parts from plots no longer in
    the plot file are removed at the end.

    Parameters
    ----------
    index_maps : list of pathlib.Path
        SI00 index maps (NetCDF datasets or single-index GeoTIFFs).
    plotshp : geopandas.GeoDataFrame
        Validated plot polygons with a ``plot_id`` column.
    dataset_dir : pathlib.Path
        Output dataset directory (created if missing).
    inputs : list of pathlib.Path
        Freshness inputs for the per-plot resume check (index maps,
        manifest, plot file).
    indices : list of str, optional
        Restrict to these index names. Default None (all). Changing the
        restriction does not invalidate existing parts — use *force*.
    force : bool, optional
        Clear all existing parts and re-extract everything. Default
        False.

    Returns
    -------
    int
        Plots with at least one pixel written or reused.
    int
        Plots reused from fresh existing parts.
    list of str
        Issues encountered (missing CRS, unknown indices, ...).
    """
    issues: List[str] = []
    n_plots_px = 0
    n_reused = 0

    # ========== Open every map once ==========
    opened: List[Tuple[Any, xr.Dataset, Any]] = []
    try:
        for map_path in index_maps:
            # SI00 NetCDFs carry the CRS on spatial_ref via decode_coords="all";
            # GeoTIFFs go through the same xarray accessor path.
            if map_path.suffix == ".nc":
                ds = xr.open_dataset(map_path, decode_coords="all")
            else:
                da = rioxarray.open_rasterio(map_path, masked=True)
                ds = da.to_dataset(name=map_path.stem)  # type: ignore[union-attr]
            crs = ds.rio.crs
            if crs is None:
                issues.append(f"Index map {map_path.name} has no CRS; skipping.")
                ds.close()
                continue
            keep = list(ds.data_vars)
            if indices is not None:
                unknown = sorted(set(indices) - set(keep))
                if unknown:
                    issues.append(f"Indices not in {map_path.name}: {unknown}.")
                keep = [v for v in keep if v in set(indices)]
                if not keep:
                    ds.close()
                    continue
            sub_ds = ds[keep]
            if "time" in sub_ds.dims:
                sub_ds = sub_ds.isel(time=0, drop=True)
            opened.append((ds, sub_ds, crs))
        if not opened:
            return 0, 0, issues

        dataset_dir.mkdir(parents=True, exist_ok=True)
        if force:
            for old in dataset_dir.glob("*.parquet*"):
                old.unlink()

        # +++++ Reproject the plots once per map CRS (row order preserved) +++++
        per_map_plots = [plotshp.to_crs(crs)[["plot_id", "geometry"]]
                         for (_, _, crs) in opened]

        for pos in tqdm(range(len(plotshp)), desc=dataset_dir.name, leave=False):
            pid = plotshp["plot_id"].iloc[pos]
            part = dataset_dir / f"{cf.safe_filename_component(str(pid))}.parquet"
            # +++++ Per-plot resume: fresh parts never touch the rasters +++++
            if not force and cf.outputs_up_to_date([part], inputs):
                n_plots_px += 1
                n_reused += 1
                continue
            frames: List[pd.DataFrame] = []
            for (_, sub_ds, crs), plots in zip(opened, per_map_plots):
                pixels = _plot_window_pixels(sub_ds, plots.iloc[pos], crs)
                if pixels is not None:
                    frames.append(pixels)
            if not frames:
                if part.is_file():
                    part.unlink()  # plot lost coverage since the last run
                continue
            pex.write_dataset_part(pd.concat(frames, ignore_index=True), part)
            n_plots_px += 1
    finally:
        for ds, _, _ in opened:
            ds.close()

    # +++++ Remove orphan parts from plots no longer in the plot file +++++
    valid_names = {f"{cf.safe_filename_component(str(pid))}.parquet"
                   for pid in plotshp["plot_id"]}
    orphans = [p for p in pex.dataset_parts(dataset_dir)
               if p.name not in valid_names]
    for orphan in orphans:
        orphan.unlink()
    if orphans:
        warn.warn(f"Removed {len(orphans)} orphan part file(s) from "
                  f"{dataset_dir.name} (plots no longer in the plot file).")

    return n_plots_px, n_reused, issues


# ==================================================================================
def _plot_window_pixels(
        ds: xr.Dataset,
        prow: pd.Series,
        crs: Any,
    ) -> Optional[pd.DataFrame]:
    """Collect the per-pixel values of every index inside one plot polygon.

    Parameters
    ----------
    ds : xarray.Dataset
        Index maps (one variable per index, ``y``/``x`` dims, CRS set).
    prow : pandas.Series
        Plot row with ``plot_id`` and ``geometry``.
    crs : Any
        The dataset CRS (rasterio CRS object).

    Returns
    -------
    pandas.DataFrame or None
        Long-format rows (``plot_id``, ``index``, ``value``) holding the
        finite pixels; None when the clip failed or the window holds no
        pixels.
    """
    try:
        window = ds.rio.clip_box(*prow.geometry.bounds)
        clipped = window.rio.clip([prow.geometry], crs, drop=True)
    except Exception as er:  # rioxarray raises several types here
        warn.warn(f"Could not clip plot {prow['plot_id']}: {er}. Skipping plot.")
        return None
    frames: List[pd.DataFrame] = []
    for name, da in clipped.data_vars.items():
        vals = da.to_numpy().ravel()
        finite = vals[np.isfinite(vals)]
        if finite.size == 0:
            continue
        frames.append(pd.DataFrame({
            "plot_id": prow["plot_id"],
            "index": str(name),
            "value": finite.astype(np.float32),
        }))
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


# ==================================================================================
def compute_index_metrics(
        dataset_dir: pathlib.Path,
        full_percentiles: bool = False,
    ) -> Tuple[pd.DataFrame, Optional[pd.DataFrame]]:
    """Compute per plot x index metrics from a saved pixel dataset.

    Reads one per-plot part file at a time, so memory stays at one
    plot's pixels.

    Parameters
    ----------
    dataset_dir : pathlib.Path
        The pixel dataset directory from :func:`extract_index_pixels`.
    full_percentiles : bool, optional
        Also build the full 0-100 percentile profile per plot x index.
        Default False.

    Returns
    -------
    pandas.DataFrame
        One row per plot x index: ``plot_id``, ``index``, the
        :func:`pex.group_value_stats` metric set (count/mean/std/var/
        min/max/median/skew/kurtosis/normality/p01-p99) and
        ``valid_fraction`` (valid pixels over the plot's best-covered
        index — PE01 semantics, so plot geometry does not deflate the
        fraction).
    pandas.DataFrame or None
        Long-format full percentile table (``plot_id``, ``index``,
        ``percentile``, ``value``); None unless *full_percentiles*.
    """
    print(f"Computing index metrics from {dataset_dir.name} ...")
    out: List[pd.DataFrame] = []
    pctl_out: List[pd.DataFrame] = []
    for part in tqdm(pex.dataset_parts(dataset_dir),
                     desc="index metrics", leave=False):
        pdf = pd.read_parquet(part)
        if pdf.empty:
            continue
        g = pex.group_value_stats(pdf, ["index"])
        g["index"] = g["index"].astype(str)
        g["valid_fraction"] = g["count"] / g["count"].max()
        # Keep the source dtype: plot files may use int or str plot ids.
        g.insert(0, "plot_id", pdf["plot_id"].iloc[0])
        out.append(g)
        if full_percentiles:
            pctl = pex.group_value_percentiles(pdf, ["index"])
            pctl["index"] = pctl["index"].astype(str)
            pctl.insert(0, "plot_id", pdf["plot_id"].iloc[0])
            pctl_out.append(pctl)
    percentiles = (pd.concat(pctl_out, ignore_index=True)
                   if full_percentiles else None)
    return pd.concat(out, ignore_index=True), percentiles


# ==================================================================================
def write_manifest_report(
        job: Dict[str, Any],
        metrics: pd.DataFrame,
        plotshp: gpd.GeoDataFrame,
        cfg: PE02Config,
        args: argparse.Namespace,
    ) -> None:
    """Write the per-manifest markdown overview report with figures.

    Parameters
    ----------
    job : dict
        Job dict from :func:`locate_si_manifests`.
    metrics : pandas.DataFrame
        Trait table from :func:`extract_index_metrics` (+ metadata cols).
    plotshp : geopandas.GeoDataFrame
        Validated plot polygons (for the choropleth figure).
    cfg : PE02Config
        Tunable settings (figure folder, headline index).
    args : argparse.Namespace
        Parsed command-line arguments (``skipplot``).

    Returns
    -------
    None
    """
    figures: List[Tuple[str, pathlib.Path]] = []
    if not args.skipplot:
        job["figures_dir"].mkdir(parents=True, exist_ok=True)
        headline = (cfg.headline_index
                    if cfg.headline_index in set(metrics["index"]) else
                    sorted(metrics["index"].unique())[0])
        figures.append((f"Per-plot {headline} (mean)",
                        plot_index_choropleth(job, metrics, plotshp, headline)))
        figures.append(("Per-index distribution across plots",
                        plot_index_distributions(job, metrics)))

    # ========== Assemble the markdown ==========
    date_str = (pd.Timestamp(job["date"]).date().isoformat()
                if job["date"] is not None else "unknown")
    per_index = metrics.groupby("index").agg(
        plots=("plot_id", "nunique"),
        mean_of_means=("mean", "mean"),
        min_mean=("mean", "min"),
        max_mean=("mean", "max"),
        median_valid_fraction=("valid_fraction", "median"),
    ).reset_index()
    lines = [
        f"# Spectral-index plot extraction — {job['sensor']} {date_str} "
        f"{job['run']} ({job['region']}/{job['method']})",
        "",
        f"- **Project:** {job['project']}",
        f"- **Site:** {job['site']}",
        f"- **SI00 manifest:** `{job['manifest'].name}`",
        f"- **Plot file:** `{plotshp.attrs['plot_file'].name}`",
        f"- **Indices extracted:** {metrics['index'].nunique()} "
        f"across {metrics['plot_id'].nunique()} plots",
        f"- **Generated:** {pd.Timestamp.now(tz='UTC').isoformat()} "
        f"by `{pathlib.Path(__file__).name}` {__version__}",
        "",
        "## Per-index summary",
        "",
        cf.markdown_table(per_index),
        "",
    ]
    for title, figpath in figures:
        rel = figpath.relative_to(job["report_file"].parent).as_posix()
        lines += [f"## {title}", "", f"![{title}]({rel})", ""]
    job["report_file"].parent.mkdir(parents=True, exist_ok=True)
    job["report_file"].write_text("\n".join(lines), encoding="utf-8")
    print(f"Report written: {job['report_file']}")


# ==================================================================================
def plot_index_choropleth(
        job: Dict[str, Any],
        metrics: pd.DataFrame,
        plotshp: gpd.GeoDataFrame,
        index: str,
    ) -> pathlib.Path:
    """Save a per-plot choropleth of one index's mean value.

    Parameters
    ----------
    job : dict
        Job dict (figure folder, metadata).
    metrics : pandas.DataFrame
        Trait table.
    plotshp : geopandas.GeoDataFrame
        Plot polygons.
    index : str
        Index name to map.

    Returns
    -------
    pathlib.Path
        The saved PNG path (no ``%`` in the name).
    """
    vals = metrics.loc[metrics["index"] == index, ["plot_id", "mean"]]
    gdf = plotshp.merge(vals, on="plot_id", how="left")
    fig, ax = plt.subplots(figsize=(10, 8))
    gdf.plot(column="mean", ax=ax, legend=True, cmap="RdYlGn",
             missing_kwds={"color": "lightgrey", "label": "no pixels"})
    ax.set_title(f"{job['sensor']} {job['run']} — per-plot mean {index} "
                 f"({job['region']}/{job['method']})")
    ax.set_aspect("equal")
    outpath = job["figures_dir"] / f"INDEXplot_{index}_{job['region']}_{job['method']}.png"
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outpath


# ==================================================================================
def plot_index_distributions(
        job: Dict[str, Any],
        metrics: pd.DataFrame,
        max_indices: int = 40,
    ) -> pathlib.Path:
    """Save a boxplot panel of per-plot means for each index.

    Parameters
    ----------
    job : dict
        Job dict (figure folder, metadata).
    metrics : pandas.DataFrame
        Trait table.
    max_indices : int, optional
        Cap on boxes shown (alphabetical head; the full list lives in
        the trait table). Default 40.

    Returns
    -------
    pathlib.Path
        The saved PNG path.
    """
    names = sorted(metrics["index"].unique())
    shown = names[:max_indices]
    data = [metrics.loc[metrics["index"] == n, "mean"].to_numpy() for n in shown]
    fig, ax = plt.subplots(figsize=(max(10, 0.3 * len(shown)), 6))
    ax.boxplot(data, tick_labels=shown, showfliers=False)
    ax.tick_params(axis="x", rotation=90, labelsize=7)
    ax.set_ylabel("Per-plot mean index value")
    title = f"{job['sensor']} {job['run']} {job['region']}/{job['method']} — per-plot means"
    if len(names) > max_indices:
        title += f" (first {max_indices} of {len(names)} indices)"
    ax.set_title(title)
    outpath = job["figures_dir"] / f"INDEXplot_distributions_{job['region']}_{job['method']}.png"
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outpath


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
        n_indices: Optional[int] = None,
        n_plots: Optional[int] = None,
    ) -> Dict[str, Any]:
    """Build one end-of-run summary row.

    Parameters
    ----------
    job : dict
        Job dict from :func:`locate_si_manifests`.
    status : str
        Outcome label (``extracted``, ``cached``, ``skipped``, ...).
    reason : str or None
        Issue text for skipped/qualified rows.
    n_indices : int, optional
        Indices extracted.
    n_plots : int, optional
        Plots that received pixels.

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
        "region": job.get("region"),
        "method": job.get("method"),
        "n_indices": n_indices,
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
    columns = ["project", "sensor", "date", "run", "region", "method",
               "n_indices", "n_plots", "status", "reason"]
    df = pd.DataFrame(rows, columns=columns)
    if df.empty:
        print("\nNo manifests to summarise.")
        return df

    disp = df.copy()
    for col in ["n_indices", "n_plots"]:
        disp[col] = disp[col].apply(
            lambda v: "" if v is None or pd.isna(v) else f"{int(v)}")
    disp["reason"] = disp["reason"].fillna("")

    skipped = disp[disp["status"] == "skipped"]
    reported = disp[disp["status"] != "skipped"]
    if not skipped.empty:
        print(f"\nSKIPPED ({len(skipped)}):")
        print(skipped[["project", "sensor", "date", "run", "region", "method",
                       "status", "reason"]].to_string(index=False))
    if not reported.empty:
        print(f"\nREPORTED ({len(reported)}):")
        print(reported.to_string(index=False))
    return df


# ==================================================================================
if __name__ == '__main__':
    # ========== Parse args ==========
    parser = argparse.ArgumentParser(
        description="Extract per-plot aggregate statistics from DS05/SI00 spectral-index maps.")
    parser.add_argument("--path", type=str, default=None, help="The folder to crawl for SI00 manifests. By default it will search from the root dir of the git repo.")
    parser.add_argument("--plot-variant", type=str, default=None, help="Select a plot-file variant ({YYYYSiteName}_plots_{variant}[_vNN].geojson) instead of the mandatory main plot file. See the Plot_Layout spec (wiki Key-Files).")
    parser.add_argument("--join-trial-info", default=False, action="store_true", help="Join Documentation/Trial_Info/{YYYYSiteName}_trial_info.csv onto the plots via plot_id; the trial columns are carried into the trait tables.")
    parser.add_argument("--indices", type=str, nargs="+", default=None, help="Restrict extraction to these indices (e.g. --indices NDVI NDREI). Default: all indices in each manifest. Changing the restriction does not invalidate existing per-plot parts - combine with --force.")
    parser.add_argument("--full-percentiles", default=False, action="store_true", help="Also write the full 0-100 percentile profile per plot x index to PlotLevel/PE_INDEX_{REGION}_{METHOD}_plot_percentiles[...].parquet (long format; joined via plot_id).")
    parser.add_argument("-f", "--force", default=False, action="store_true", help="Force the re-creation of output files even if they are up to date. Default is to skip files that are newer than their inputs.")
    parser.add_argument("-s", "--skipplot", default=False, action="store_true", help="Skip the report figure generation (the markdown report is still written, without embeds).")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[], help="One or more directory names to exclude from the crawl. e.g. --exclude-dir 2025_TestData")
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
