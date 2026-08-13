"""Hyperspectral plot extraction — per-plot pixel spectra from .bin orthos (DS03).

This script crawls the APPN dataset structure for GRYFN hyperspectral
orthomosaics (``<run>/T1_proc/*.gpro/products/*_{VNIR|SWIR}_Orthomosaic.bin``
— VNIR for GOBI, VNIR+SWIR for CALVIS) and extracts the pixels falling
inside each polygon of the site's mandatory Plot_Layout file
(``Documentation/Plot_Layout/{YYYYSiteName}_plots.geojson``).

The orthomosaics are 16 GB+ single files, so nothing is ever read whole:
each plot polygon is read through its own bounding-box window (the
proven QA00 pattern; ``--read-strategy block`` reads one window per
block of adjacent plots instead, but benchmarked slower — see
``--read-strategy``). Raw pixel rows stream to parquet via a pyarrow
writer so peak memory is one window plus one plot's rows.

Per run and EM region two tables are written to
``<run>/T1_proc/PlotExtracts/``:

- ``PE_{VNIR|SWIR}_pixels[...].parquet`` — raw long-format per-pixel table
  (``plot_id``, ``band``, ``wavelength``, ``value``).
- ``PE_{VNIR|SWIR}_plot_metrics[...].parquet`` — per plot x band metrics
  (mean/median/std/count/valid_fraction) plus the run metadata columns.

Metrics are always derived from the saved raw table (never a second ortho
read) unless ``--force``; a metrics-only rerun on an already-extracted run
touches parquet, not the 16 GB ``.bin``. A markdown overview report with
embedded QC figures (``PE_extraction_report[...].md`` + ``PE_figures/``)
is written alongside the tables.

Command-line Arguments
----------------------
--path : str, optional
    Folder to crawl for orthomosaics. Defaults to the git repo root.
--plot-variant : str, optional
    Select a plot-file variant (``{YYYYSiteName}_plots_{variant}[_vNN]``)
    instead of the mandatory main plot file.
--join-trial-info : flag
    Join ``Documentation/Trial_Info/{YYYYSiteName}_trial_info.csv`` onto
    the plots via ``plot_id`` (columns carried into the metrics table).
--raw-only / --metrics-only : flags
    Produce only the raw pixel table, or only the metrics table (from an
    existing raw table).
--read-strategy : {plot, block}
    One GDAL window per plot (default) or per block of adjacent plots.
    Benchmarked 2026-08-13 on the 16 GB GOBI test ortho (600 plots,
    172 bands): plot = 369 s, block(24) = 631 s — block windows read
    the inter-plot gap pixels across every band, so per-plot wins for
    dense plot grids too.
--force : flag
    Re-extract from the ortho even when outputs are up to date.
"""

# ==============================================================================

__title__ = "Hyperspectral plot extraction"
__author__ = "Arden Burrell"
__version__ = "v1.0(13.08.2026)"
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
import pyarrow as pa
import pyarrow.parquet as pq
import rioxarray
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


# ==================================================================================
@dataclass(frozen=True)
class PE01Config:
    """Tunable settings for one hyperspectral plot-extraction invocation.

    Attributes
    ----------
    valid_sensors : tuple of str
        Sensor platform folder names handled by this script.
    em_regions : tuple of str
        Orthomosaic EM-region tokens searched per run.
    extracts_dirname : str
        Name of the output folder inside ``T1_proc/``.
    figures_dirname : str
        Name of the figure folder inside the extracts folder.
    block_size : int
        Plots per read block for the ``block`` strategy.
    max_window_mb : float
        In-flight window cap; a block whose bbox window would exceed this
        is processed per plot instead.
    """
    valid_sensors: Tuple[str, ...] = ("GOBI", "CALVIS")
    em_regions: Tuple[str, ...] = ("VNIR", "SWIR")
    extracts_dirname: str = "PlotExtracts"
    figures_dirname: str = "PE_figures"
    block_size: int = 24
    max_window_mb: float = 1024.0


def default_config() -> PE01Config:
    """Return the default :class:`PE01Config` for this tool."""
    return PE01Config()


# ==================================================================================
def main(args: argparse.Namespace, path: pathlib.Path) -> pd.DataFrame:
    """Run the hyperspectral plot-extraction pipeline.

    Locates the orthomosaics under *path*, resolves and validates each
    site's Plot_Layout file, extracts per-plot pixel tables + per-plot
    metrics per EM region, writes a per-run markdown report, and prints a
    REPORTED/SKIPPED summary.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments.
    path : pathlib.Path
        Root directory to crawl.

    Returns
    -------
    pandas.DataFrame
        One row per located orthomosaic summarising the outcome.
    """
    cfg = default_config()

    # ========== Locate the orthomosaics, grouped per run ==========
    runs = locate_ortho_runs(path, cfg, args)

    # ========== Load each site's plot file once ==========
    plots_by_site = pl.load_site_plots(
        (r["site_dir"] for r in runs),
        variant=args.plot_variant, join_trial_info=args.join_trial_info)

    # ========== Extract every run x EM region (cached) ==========
    summary_rows: List[Dict[str, Any]] = []
    repo = _try_repo(path)
    for run in tqdm(runs, desc="Processing runs"):
        plotshp, plot_issues = plots_by_site[run["site_dir"]]
        if plotshp is None:
            for job in run["orthos"]:
                summary_rows.append(
                    _summary_row(run, job, "skipped", "; ".join(plot_issues)))
            continue
        run_stats: List[Dict[str, Any]] = []
        for job in run["orthos"]:
            row, stats = process_ortho(run, job, plotshp, cfg, args, repo)
            summary_rows.append(row)
            if stats is not None:
                run_stats.append(stats)
        # +++++ Per-run markdown report + figures +++++
        if run_stats and not args.raw_only:
            write_run_report(run, run_stats, plotshp, cfg, args)

    # ========== Print the end-of-run summary ==========
    summary = _print_run_summary(summary_rows)
    return summary


# ==================================================================================
def locate_ortho_runs(
        path: pathlib.Path,
        cfg: PE01Config,
        args: argparse.Namespace,
    ) -> List[Dict[str, Any]]:
    """Find hyperspectral orthomosaics and group them into per-run jobs.

    Recursively searches *path* for
    ``T1_proc/*.gpro/products/*_{VNIR|SWIR}_Orthomosaic.bin``, applies the
    single-``.gpro`` guard, parses the APPN folder metadata, and builds
    output paths.

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively.
    cfg : PE01Config
        Tunable settings (valid sensors, EM regions, folder names).
    args : argparse.Namespace
        Parsed command-line arguments (``exclude_dir``,
        ``allow_multi_gpro``, ``plot_variant``, ``verbose``).

    Returns
    -------
    list of dict
        One dict per run with keys ``site_dir``, ``extracts_dir``,
        ``report_file``, ``figures_dir``, run metadata (``node``,
        ``project``, ``site``, ``sensor``, ``date``, ``run``) and
        ``orthos`` — a list of per-ortho job dicts (``ortho``, ``region``,
        ``gpro_nu``, ``raw_file``, ``metrics_file``,
        ``metadata_outfile``).

    Raises
    ------
    ValueError
        If no orthomosaics are found under *path*.
    """
    print(f"Scanning {path} for hyperspectral orthomosaics. {pd.Timestamp.now()}")
    files: List[Tuple[pathlib.Path, str]] = []
    for region in cfg.em_regions:
        for f in path.rglob(f"*_{region}_Orthomosaic.bin"):
            if (f.parent.name == "products" and f.parents[1].suffix == ".gpro"
                    and f.parents[2].name == "T1_proc"):
                files.append((f, region))
    if args.exclude_dir:
        exclude_set = set(args.exclude_dir)
        files = [(f, r) for f, r in files
                 if not (set(p.name for p in f.parents) & exclude_set)]
    if len(files) == 0:
        raise ValueError(
            f"No hyperspectral orthomosaics found in {path}. Expected files "
            "matching <run>/T1_proc/*.gpro/products/*_{VNIR|SWIR}_Orthomosaic.bin.")

    runs: Dict[pathlib.Path, Dict[str, Any]] = {}
    for ortho, region in sorted(files):
        t1_dir = ortho.parents[2]
        run_dir = t1_dir.parent

        # ========== Require exactly one .gpro per run (QA00 pattern) ==========
        gpro_dirs = sorted(t1_dir.glob("*.gpro"))
        gpro_nu: Optional[int] = None
        if len(gpro_dirs) > 1:
            if args.allow_multi_gpro:
                gpro_nu = gpro_dirs.index(ortho.parents[1])
                warn.warn(
                    f"Found {len(gpro_dirs)} .gpro folders in {t1_dir}. Processing "
                    "anyway because --allow-multi-gpro is set; treat the extracted "
                    "tables as debugging output.")
            else:
                warn.warn(
                    f"Found {len(gpro_dirs)} .gpro folders in {t1_dir} "
                    f"({[g.name for g in gpro_dirs]}). Multiple .gpro folders "
                    "usually indicate an issue being actively debugged; skipping "
                    "this run (or use --allow-multi-gpro).")
                continue

        # ========== Parse the APPN folder structure for metadata ==========
        parsed = cf.parse_APPN_dataset_path(ortho)
        if not parsed["valid"]:
            warn.warn(f"Could not parse APPN metadata for {ortho}: "
                      f"{parsed['errors']}. Skipping file.")
            continue
        if parsed["sensor"] not in cfg.valid_sensors:
            if args.verbose:
                tqdm.write(f"Skipping {ortho}: sensor {parsed['sensor']} not in "
                           f"{cfg.valid_sensors}.")
            continue

        site_dir = (pathlib.Path(parsed["root"]) / parsed["node"]
                    / parsed["project"] / parsed["site_folder"])
        extracts_dir = t1_dir / cfg.extracts_dirname

        suffix_parts: List[str] = []
        if gpro_nu is not None:
            suffix_parts.append(f"gpro{gpro_nu}")
        if args.plot_variant:
            suffix_parts.append(args.plot_variant)
        suffix = ("_" + "_".join(suffix_parts)) if suffix_parts else ""

        run = runs.setdefault(run_dir, {
            "site_dir": site_dir,
            "extracts_dir": extracts_dir,
            "figures_dir": extracts_dir / cfg.figures_dirname,
            "report_file": extracts_dir / f"PE_extraction_report{suffix}.md",
            "node": parsed["node"],
            "project": parsed["project"],
            "site": parsed["site_folder"],
            "sensor": parsed["sensor"],
            "date": parsed["date"],
            "run": parsed["run_folder"],
            "orthos": [],
        })
        raw_file = extracts_dir / f"PE_{region}_pixels{suffix}.parquet"
        run["orthos"].append({
            "ortho": ortho,
            "region": region,
            "gpro_nu": gpro_nu,
            "raw_file": raw_file,
            "metrics_file": extracts_dir / f"PE_{region}_plot_metrics{suffix}.parquet",
            "metadata_outfile": raw_file.with_name(f"{raw_file.stem}_metadata.yaml"),
        })
    return list(runs.values())


# ==================================================================================
def process_ortho(
        run: Dict[str, Any],
        job: Dict[str, Any],
        plotshp: gpd.GeoDataFrame,
        cfg: PE01Config,
        args: argparse.Namespace,
        repo: Optional[git.Repo],
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Extract one orthomosaic into raw + metrics tables (cached).

    The raw pixel table is regenerated only when missing or older than
    the ortho/plot file (or ``--force``). Metrics are derived from the
    saved raw table — the ortho is never re-read for a metrics-only
    refresh.

    Parameters
    ----------
    run : dict
        Run dict from :func:`locate_ortho_runs`.
    job : dict
        Per-ortho job dict (one EM region).
    plotshp : geopandas.GeoDataFrame
        Validated plot polygons; source path in ``.attrs['plot_file']``.
    cfg : PE01Config
        Tunable settings.
    args : argparse.Namespace
        Parsed command-line arguments (``force``, ``raw_only``,
        ``metrics_only``, ``read_strategy``, ``block_size``, ``keep_xy``).
    repo : git.Repo or None
        Repository handle for the provenance sidecar.

    Returns
    -------
    dict
        Summary row for :func:`_print_run_summary`.
    dict or None
        Extraction stats consumed by :func:`write_run_report`; None when
        the region was skipped or is raw-only.
    """
    plot_file = plotshp.attrs["plot_file"]
    raw_fresh = cf.outputs_up_to_date([job["raw_file"]], [job["ortho"], plot_file])
    metrics_fresh = (job["metrics_file"].is_file()
                     and cf.outputs_up_to_date([job["metrics_file"]], [job["raw_file"]]))

    # ========== Raw extraction (streams to parquet) ==========
    extract_stats: Optional[Dict[str, Any]] = None
    if args.force or (not raw_fresh and not args.metrics_only):
        tqdm.write(f"Extracting {job['ortho'].name} ({job['region']}) started at "
                   f"{pd.Timestamp.now()}.")
        extract_stats = extract_ortho_pixels(
            job["ortho"], plotshp, job["raw_file"],
            strategy=args.read_strategy, block_size=args.block_size,
            keep_xy=args.keep_xy, max_window_mb=cfg.max_window_mb)
        meta = cf.build_run_metadata(
            {**{k: run[k] for k in ["node", "project", "site", "sensor", "date", "run"]},
             "ortho": job["ortho"], "region": job["region"],
             "plot_file": plot_file, "gpro_nu": job["gpro_nu"],
             "read_strategy": args.read_strategy,
             **{k: v for k, v in extract_stats.items() if not isinstance(v, pd.DataFrame)}},
            script_path=__file__, repo=repo)
        cf.write_metadata_yaml(meta, job["metadata_outfile"])
        raw_fresh = True
        metrics_fresh = False
    elif not raw_fresh and args.metrics_only:
        if not job["raw_file"].is_file():
            return (_summary_row(run, job, "skipped",
                                 "--metrics-only but no raw pixel table exists"),
                    None)
        warn.warn(f"{job['raw_file'].name} is older than its inputs; metrics "
                  "will be derived from the stale raw table (--metrics-only).")
        raw_fresh = True

    if args.raw_only:
        status = "extracted" if extract_stats is not None else "cached"
        n_px = extract_stats["n_pixels"] if extract_stats else None
        return _summary_row(run, job, status, None, n_pixels=n_px), None

    # ========== Metrics (from the saved raw table, never the ortho) ==========
    if metrics_fresh and extract_stats is None:
        metrics = pd.read_parquet(job["metrics_file"])
        stats = _stats_from_metrics(job, metrics)
        return (_summary_row(run, job, "cached", None,
                             n_pixels=stats["n_pixels"],
                             n_plots=stats["n_plots_with_pixels"]),
                stats)

    metrics = compute_plot_metrics(job["raw_file"])
    # +++++ Attach run metadata (+ optional trial-info columns) +++++
    for key in ["node", "project", "site", "sensor", "date", "run"]:
        metrics[key] = run[key]
    metrics["EM_Region"] = job["region"]
    if job["gpro_nu"] is not None:
        metrics["gpro_nu"] = job["gpro_nu"]
    trial_cols = [c for c in plotshp.columns
                  if c not in ("geometry",) and c != "plot_id"]
    if trial_cols:
        metrics = metrics.merge(
            pd.DataFrame(plotshp[["plot_id"] + trial_cols]),
            on="plot_id", how="left")
    metrics.to_parquet(job["metrics_file"], index=False)

    stats = _stats_from_metrics(job, metrics)
    if extract_stats is not None:
        stats.update({k: v for k, v in extract_stats.items() if k != "n_pixels"})
    status = "extracted" if extract_stats is not None else "metrics_refreshed"
    return (_summary_row(run, job, status, None,
                         n_pixels=stats["n_pixels"],
                         n_plots=stats["n_plots_with_pixels"]),
            stats)


# ==================================================================================
def extract_ortho_pixels(
        ortho: pathlib.Path,
        plotshp: gpd.GeoDataFrame,
        raw_file: pathlib.Path,
        strategy: str = "plot",
        block_size: int = 24,
        keep_xy: bool = False,
        max_window_mb: float = 1024.0,
    ) -> Dict[str, Any]:
    """Stream per-plot pixels from an orthomosaic into a parquet table.

    Plots are read through per-polygon bounding-box windows:
    ``clip_box`` (a windowed read) then one exact ``clip`` per polygon
    (the proven QA00 panel pattern). ``strategy="block"`` reads one
    window per block of adjacent polygons instead and clips each plot
    from the in-memory window — benchmarked slower on the 16 GB GOBI
    test ortho (631 s vs 369 s; the block bbox drags in inter-plot gap
    pixels across every band). Rows are appended per plot through a
    :class:`pyarrow.parquet.ParquetWriter`, so peak memory is one
    window plus one plot's rows.

    Parameters
    ----------
    ortho : pathlib.Path
        The ENVI ``.bin`` orthomosaic.
    plotshp : geopandas.GeoDataFrame
        Validated plot polygons with a ``plot_id`` column.
    raw_file : pathlib.Path
        Output parquet path (parent created if missing).
    strategy : {'plot', 'block'}, optional
        Read strategy. Default ``"plot"``.
    block_size : int, optional
        Plots per block for the block strategy. Default 24.
    keep_xy : bool, optional
        Retain per-pixel ``x``/``y`` columns. Default False.
    max_window_mb : float, optional
        A block whose bbox window would exceed this cap (native dtype)
        is processed per plot instead. Default 1024.

    Returns
    -------
    dict
        Extraction stats: ``n_pixels``, ``n_plots_total``,
        ``n_plots_with_pixels``, ``n_plots_empty``, ``n_bands``,
        ``wavelength_range_nm``, ``elapsed_s``.

    Raises
    ------
    ValueError
        If the raster has no CRS or *strategy* is unknown.
    """
    if strategy not in ("block", "plot"):
        raise ValueError(f"Unknown read strategy '{strategy}'.")
    t0 = pd.Timestamp.now()

    # +++++ Per-band wavelengths + native dtype from the raster metadata +++++
    wavelengths, src_dtype = cf.band_wavelengths(ortho)
    np_dtype = np.dtype(src_dtype)

    # +++++ Open lazily (masked=True -> nodata becomes NaN) +++++
    ds = rioxarray.open_rasterio(ortho, masked=True)
    try:
        crs = ds.rio.crs  # type: ignore[union-attr]
        if crs is None:
            raise ValueError(f"Raster {ortho} does not have a CRS defined.")
        plots = plotshp.to_crs(crs)[["plot_id", "geometry"]]
        # Sort into spatial reading order so blocks are compact windows.
        plots = plots.assign(
            _minx=plots.geometry.bounds["minx"], _miny=plots.geometry.bounds["miny"]
        ).sort_values(["_miny", "_minx"]).drop(columns=["_minx", "_miny"])

        bytes_per_px = int(ds.sizes["band"]) * np_dtype.itemsize
        res_x, res_y = map(abs, ds.rio.resolution())  # type: ignore[union-attr]

        # +++++ Build the read blocks +++++
        if strategy == "plot":
            blocks = [plots.iloc[[i]] for i in range(len(plots))]
        else:
            blocks = [plots.iloc[i:i + block_size]
                      for i in range(0, len(plots), block_size)]

        raw_file.parent.mkdir(parents=False, exist_ok=True)
        writer: Optional[pq.ParquetWriter] = None
        n_pixels = 0
        plots_with_px: set = set()
        try:
            for block in tqdm(blocks, desc=f"{ortho.stem} ({strategy})", leave=False):
                minx, miny, maxx, maxy = block.total_bounds
                window_mb = ((maxx - minx) / res_x) * ((maxy - miny) / res_y) \
                    * bytes_per_px / 2 ** 20
                if strategy == "block" and window_mb > max_window_mb:
                    warn.warn(
                        f"Block window would be {window_mb:.0f} MB "
                        f"(> {max_window_mb:.0f} MB cap); reading its "
                        f"{len(block)} plots individually.")
                    sub_blocks = [block.iloc[[i]] for i in range(len(block))]
                else:
                    sub_blocks = [block]
                for sub in sub_blocks:
                    try:
                        window = ds.rio.clip_box(*sub.total_bounds)  # type: ignore[union-attr]
                        window = window.load()
                    except Exception as er:  # rioxarray raises several types here
                        warn.warn(f"Could not read window for plots "
                                  f"{sub['plot_id'].tolist()} from {ortho.name}: "
                                  f"{er}. Skipping.")
                        continue
                    for _, prow in sub.iterrows():
                        df = _clip_plot(window, prow, crs, wavelengths,
                                        np_dtype, keep_xy)
                        if df is None or df.empty:
                            continue
                        table = pa.Table.from_pandas(df, preserve_index=False)
                        if writer is None:
                            writer = pq.ParquetWriter(raw_file, table.schema)
                        writer.write_table(table)
                        n_pixels += int(df["band"].value_counts().iloc[0])
                        plots_with_px.add(prow["plot_id"])
                    del window
        finally:
            if writer is not None:
                writer.close()
    finally:
        # Close the GDAL handle now; open handles at interpreter shutdown
        # trigger a harmless-but-noisy "Error in sys.excepthook" teardown race.
        ds.close()

    if writer is None:
        raise ValueError(
            f"No plot polygons yielded pixels from {ortho}. Check that the "
            "plot geometries overlap the raster extent.")

    wl = [v for v in wavelengths.values() if np.isfinite(v)]
    return ({
        "n_pixels": n_pixels,
        "n_plots_total": len(plots),
        "n_plots_with_pixels": len(plots_with_px),
        "n_plots_empty": len(plots) - len(plots_with_px),
        "n_bands": len(wavelengths),
        "wavelength_range_nm": ([float(min(wl)), float(max(wl))] if wl else None),
        "elapsed_s": float((pd.Timestamp.now() - t0).total_seconds()),
    })


# ==================================================================================
def _clip_plot(
        window: Any,
        prow: pd.Series,
        crs: Any,
        wavelengths: Dict[int, float],
        np_dtype: np.dtype,
        keep_xy: bool,
    ) -> Optional[pd.DataFrame]:
    """Clip one plot polygon out of an in-memory window into a long table.

    Parameters
    ----------
    window : xarray.DataArray
        In-memory raster window covering the polygon.
    prow : pandas.Series
        Plot row with ``plot_id`` and ``geometry``.
    crs : Any
        The raster CRS (rasterio CRS object).
    wavelengths : dict of int to float
        Band index to wavelength (nm) mapping.
    np_dtype : numpy.dtype
        Native on-disk dtype to restore after the masked read.
    keep_xy : bool
        Retain per-pixel ``x``/``y`` columns.

    Returns
    -------
    pandas.DataFrame or None
        Long-format rows (``plot_id``, ``band``, ``wavelength``,
        ``value``); None when the clip failed.
    """
    try:
        sub = window.rio.clip([prow.geometry], crs, drop=True)
    except Exception as er:  # rioxarray raises several types here
        warn.warn(f"Could not clip plot {prow['plot_id']}: {er}. Skipping plot.")
        return None
    df = sub.to_dataframe(name="value").reset_index()
    df = df.dropna(subset=["value"])
    if df.empty:
        return None
    df = df.drop(columns=["spatial_ref"], errors="ignore")
    if not keep_xy:
        df = df.drop(columns=["x", "y"], errors="ignore")
    # +++++ Restore the native dtype (masked read promotes to float) +++++
    if np.issubdtype(np_dtype, np.integer):
        df["value"] = df["value"].round().astype(np_dtype)
    df["band"] = df["band"].astype(np.int16)
    df["wavelength"] = df["band"].map(wavelengths).astype(np.float32)
    df["plot_id"] = prow["plot_id"]
    cols = ["plot_id", "band", "wavelength", "value"]
    if keep_xy:
        cols += ["x", "y"]
    return df[cols]


# ==================================================================================
def compute_plot_metrics(raw_file: pathlib.Path) -> pd.DataFrame:
    """Compute per plot x band metrics from a saved raw pixel table.

    Streams the parquet record batches (the raw table is written in
    plot order) and buffers one plot at a time, so the 16 GB ortho is
    never touched and memory stays at one plot's rows.

    Parameters
    ----------
    raw_file : pathlib.Path
        The raw pixel parquet from :func:`extract_ortho_pixels`.

    Returns
    -------
    pandas.DataFrame
        One row per plot x band: ``plot_id``, ``band``, ``wavelength``,
        ``mean``, ``median``, ``std``, ``count``, ``valid_fraction``
        (band count over the plot's best-covered band).
    """
    print(f"Computing plot metrics from {raw_file.name} ...")
    pf = pq.ParquetFile(raw_file)
    out: List[pd.DataFrame] = []
    buffer: List[pd.DataFrame] = []
    current: Optional[Any] = None

    def _flush() -> None:
        if not buffer:
            return
        pdf = pd.concat(buffer, ignore_index=True)
        g = pdf.groupby("band", sort=True).agg(
            wavelength=("wavelength", "first"),
            mean=("value", "mean"),
            median=("value", "median"),
            std=("value", "std"),
            count=("value", "size"),
        ).reset_index()
        g["valid_fraction"] = g["count"] / g["count"].max()
        g.insert(0, "plot_id", current)
        out.append(g)
        buffer.clear()

    for batch in pf.iter_batches(columns=["plot_id", "band", "wavelength", "value"]):
        pdf = batch.to_pandas()
        for pid, grp in pdf.groupby("plot_id", sort=False):
            if current is not None and pid != current:
                _flush()
            current = pid
            buffer.append(grp)
    _flush()
    return pd.concat(out, ignore_index=True)


# ==================================================================================
def write_run_report(
        run: Dict[str, Any],
        run_stats: List[Dict[str, Any]],
        plotshp: gpd.GeoDataFrame,
        cfg: PE01Config,
        args: argparse.Namespace,
    ) -> None:
    """Write the per-run markdown overview report with embedded figures.

    Parameters
    ----------
    run : dict
        Run dict from :func:`locate_ortho_runs`.
    run_stats : list of dict
        Per-region stats from :func:`process_ortho`.
    plotshp : geopandas.GeoDataFrame
        Validated plot polygons (for the footprint figure).
    cfg : PE01Config
        Tunable settings (figure folder name).
    args : argparse.Namespace
        Parsed command-line arguments (``skipplot``).

    Returns
    -------
    None
    """
    figures: List[Tuple[str, pathlib.Path]] = []
    if not args.skipplot:
        run["figures_dir"].mkdir(parents=False, exist_ok=True)
        figures.append(("Plot footprints (pixel counts)",
                        plot_footprint_figure(run, run_stats, plotshp)))
        for stats in run_stats:
            figures.append((f"Per-plot mean spectra — {stats['region']}",
                            plot_mean_spectra_figure(run, stats)))

    # ========== Assemble the markdown ==========
    date_str = (pd.Timestamp(run["date"]).date().isoformat()
                if run["date"] is not None else "unknown")
    lines = [
        f"# Plot extraction report — {run['sensor']} {date_str} {run['run']}",
        "",
        f"- **Project:** {run['project']}",
        f"- **Site:** {run['site']}",
        f"- **Plot file:** `{plotshp.attrs['plot_file'].name}`",
        f"- **Generated:** {pd.Timestamp.now(tz='UTC').isoformat()} "
        f"by `{pathlib.Path(__file__).name}` {__version__}",
        "",
        "## Extraction statistics",
        "",
    ]
    stat_rows = []
    for stats in run_stats:
        wl = stats.get("wavelength_range_nm")
        stat_rows.append({
            "EM region": stats["region"],
            "plots found": stats.get("n_plots_total"),
            "plots extracted": stats.get("n_plots_with_pixels"),
            "plots empty": stats.get("n_plots_empty"),
            "pixels": stats.get("n_pixels"),
            "px/plot (median)": stats.get("median_px_per_plot"),
            "bands": stats.get("n_bands"),
            "wavelengths (nm)": (f"{wl[0]:.0f}-{wl[1]:.0f}" if wl else "n/a"),
            "valid fraction (median)": stats.get("median_valid_fraction"),
        })
    lines.append(cf.markdown_table(pd.DataFrame(stat_rows)))
    lines.append("")
    for title, figpath in figures:
        rel = figpath.relative_to(run["report_file"].parent).as_posix()
        lines += [f"## {title}", "", f"![{title}]({rel})", ""]
    run["report_file"].write_text("\n".join(lines), encoding="utf-8")
    print(f"Report written: {run['report_file']}")


# ==================================================================================
def plot_footprint_figure(
        run: Dict[str, Any],
        run_stats: List[Dict[str, Any]],
        plotshp: gpd.GeoDataFrame,
    ) -> pathlib.Path:
    """Save the plot-footprint overview choropleth (pixels per plot).

    Parameters
    ----------
    run : dict
        Run dict (figure folder, metadata).
    run_stats : list of dict
        Per-region stats; the first region's metrics table supplies the
        per-plot pixel counts.
    plotshp : geopandas.GeoDataFrame
        Plot polygons.

    Returns
    -------
    pathlib.Path
        The saved PNG path (no ``%`` in the name — markdown-preview safe).
    """
    metrics = run_stats[0]["metrics"]
    px = metrics.groupby("plot_id")["count"].max().rename("pixels")
    gdf = plotshp.merge(px, on="plot_id", how="left")
    fig, ax = plt.subplots(figsize=(10, 8))
    gdf.plot(column="pixels", ax=ax, legend=True, cmap="viridis",
             missing_kwds={"color": "lightgrey", "label": "no pixels"})
    ax.set_title(f"{run['sensor']} {run['run']} — pixels per plot "
                 f"({run_stats[0]['region']})")
    ax.set_aspect("equal")
    outpath = run["figures_dir"] / "plot_footprints.png"
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outpath


# ==================================================================================
def plot_mean_spectra_figure(
        run: Dict[str, Any],
        stats: Dict[str, Any],
    ) -> pathlib.Path:
    """Save the per-plot mean-spectra panel for one EM region.

    One thin line per plot (mean value per band) — a fast visual check
    for outlier plots and artefact bands.

    Parameters
    ----------
    run : dict
        Run dict (figure folder, metadata).
    stats : dict
        Region stats containing the ``metrics`` DataFrame.

    Returns
    -------
    pathlib.Path
        The saved PNG path.
    """
    metrics = stats["metrics"]
    fig, ax = plt.subplots(figsize=(12, 6))
    x_col = "wavelength" if metrics["wavelength"].notna().any() else "band"
    for _, grp in metrics.groupby("plot_id"):
        ax.plot(grp[x_col], grp["mean"], color="tab:green", alpha=0.05, lw=0.6)
    ax.set_xlabel("Wavelength (nm)" if x_col == "wavelength" else "Band index")
    ax.set_ylabel("Mean pixel value")
    ax.set_title(f"{run['sensor']} {run['run']} {stats['region']} — "
                 f"per-plot mean spectra ({metrics['plot_id'].nunique()} plots)")
    outpath = run["figures_dir"] / f"mean_spectra_{stats['region']}.png"
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outpath


# ==================================================================================
def _stats_from_metrics(
        job: Dict[str, Any],
        metrics: pd.DataFrame,
    ) -> Dict[str, Any]:
    """Derive the report stats block from a metrics table.

    Parameters
    ----------
    job : dict
        Per-ortho job dict (for the region label).
    metrics : pandas.DataFrame
        Metrics table from :func:`compute_plot_metrics`.

    Returns
    -------
    dict
        Stats consumed by :func:`write_run_report` (includes the metrics
        DataFrame itself under ``"metrics"``).
    """
    per_plot_px = metrics.groupby("plot_id")["count"].max()
    wl = metrics["wavelength"].dropna()
    return ({
        "region": job["region"],
        "metrics": metrics,
        "n_pixels": int(metrics["count"].sum() / max(metrics["band"].nunique(), 1)
                        ) if not metrics.empty else 0,
        "n_plots_with_pixels": int(metrics["plot_id"].nunique()),
        "n_bands": int(metrics["band"].nunique()),
        "median_px_per_plot": float(per_plot_px.median()) if len(per_plot_px) else None,
        "median_valid_fraction": (
            float(metrics["valid_fraction"].median()) if not metrics.empty else None),
        "wavelength_range_nm": ([float(wl.min()), float(wl.max())]
                                if len(wl) else None),
    })


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
        run: Dict[str, Any],
        job: Dict[str, Any],
        status: str,
        reason: Optional[str],
        n_pixels: Optional[int] = None,
        n_plots: Optional[int] = None,
    ) -> Dict[str, Any]:
    """Build one end-of-run summary row.

    Parameters
    ----------
    run : dict
        Run dict from :func:`locate_ortho_runs`.
    job : dict
        Per-ortho job dict.
    status : str
        Outcome label (``extracted``, ``cached``, ``skipped``, ...).
    reason : str or None
        Issue text for skipped rows.
    n_pixels : int, optional
        Pixels extracted.
    n_plots : int, optional
        Plots that received pixels.

    Returns
    -------
    dict
        Row for :func:`_print_run_summary`.
    """
    date = run.get("date")
    return ({
        "project": run.get("project"),
        "sensor": run.get("sensor"),
        "date": date.strftime("%Y-%m-%d") if date is not None and pd.notna(date) else None,
        "run": run.get("run"),
        "region": job.get("region"),
        "n_pixels": n_pixels,
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
    columns = ["project", "sensor", "date", "run", "region", "n_pixels",
               "n_plots", "status", "reason"]
    df = pd.DataFrame(rows, columns=columns)
    if df.empty:
        print("\nNo runs to summarise.")
        return df

    disp = df.copy()
    for col in ["n_pixels", "n_plots"]:
        disp[col] = disp[col].apply(
            lambda v: "" if v is None or pd.isna(v) else f"{int(v)}")
    disp["reason"] = disp["reason"].fillna("")

    skipped = disp[disp["status"] == "skipped"]
    reported = disp[disp["status"] != "skipped"]
    if not skipped.empty:
        print(f"\nSKIPPED ({len(skipped)}):")
        print(skipped[["project", "sensor", "date", "run", "region", "status",
                       "reason"]].to_string(index=False))
    if not reported.empty:
        print(f"\nREPORTED ({len(reported)}):")
        print(reported.to_string(index=False))
    return df


# ==================================================================================
if __name__ == '__main__':
    # ========== Parse args ==========
    parser = argparse.ArgumentParser(
        description="Extract per-plot pixel spectra from GRYFN hyperspectral .bin orthomosaics.")
    parser.add_argument("--path", type=str, default=None, help="The folder to crawl for orthomosaics. By default it will search from the root dir of the git repo.")
    parser.add_argument("--plot-variant", type=str, default=None, help="Select a plot-file variant ({YYYYSiteName}_plots_{variant}[_vNN].geojson) instead of the mandatory main plot file. See the Plot_Layout spec (wiki Key-Files).")
    parser.add_argument("--join-trial-info", default=False, action="store_true", help="Join Documentation/Trial_Info/{YYYYSiteName}_trial_info.csv onto the plots via plot_id; the trial columns are carried into the metrics tables.")
    parser.add_argument("-f", "--force", default=False, action="store_true", help="Force re-extraction from the ortho even when outputs are up to date.")
    parser.add_argument("--raw-only", default=False, action="store_true", help="Only produce the raw per-pixel tables (skip metrics + report).")
    parser.add_argument("--metrics-only", default=False, action="store_true", help="Only (re)compute the per-plot metrics tables and report from existing raw tables; the ortho is never opened.")
    parser.add_argument("--read-strategy", type=str, default="plot", choices=["plot", "block"], help="One GDAL window per plot (default; benchmarked 369 s vs 631 s for block on the 16 GB GOBI test ortho) or one window per block of adjacent plots.")
    parser.add_argument("--block-size", type=int, default=24, help="Plots per read block for the block strategy. Default 24.")
    parser.add_argument("--keep-xy", default=False, action="store_true", help="Retain the per-pixel x and y coordinate columns (raster CRS) in the raw tables. By default these are dropped.")
    parser.add_argument("-s", "--skipplot", default=False, action="store_true", help="Skip the report figure generation (the markdown report is still written, without embeds).")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[], help="One or more directory names to exclude from the crawl. e.g. --exclude-dir 2025_TestData")
    parser.add_argument("--allow-multi-gpro", default=False, action="store_true", help="Process runs that contain more than one .gpro folder instead of skipping them. Debugging only; outputs get a _gproN suffix.")
    parser.add_argument("-v", "--verbose", default=False, action="store_true", help="Enable verbose output.")
    args = parser.parse_args()

    if args.raw_only and args.metrics_only:
        parser.error("--raw-only and --metrics-only are mutually exclusive.")

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
    main(args, path)
