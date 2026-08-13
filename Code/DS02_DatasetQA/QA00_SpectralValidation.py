"""Spectral Validation — ELM/VAL panel spectra extraction (single-run spectral QC).

This script automatically crawls the dataset file structure and looks for the
ELM and validation panel vector files. For each run it extracts the panel
pixels from the hyperspectral orthomosaics into long-format spectra tables
saved in ``<run>/T1_proc/QC_data/QC_Spectral_Tables/``.

Panel files must follow the official AerialDataQC naming convention
``QC_{ELM|VAL}[_{TargetIdentifier}]_Panels[_{Extra}].geojson`` (GeoJSON
preferred, shapefile accepted) and live in ``<run>/T1_proc/QC_data/``.
See Protocols/QA/QAprocess/AerialDataQC.md in the
APPN-Field-Protocols-and-Pipelines repo.

Extracted tables follow the protocol column list (``band``, ``wavelength``,
``value``, ``Panel_ref``, ``node``, ``project``, ``site``, ``sensor``,
``date``, ``run``, ``panel_name``, ``EM_Region``, ``gpro_nu``) plus
``target_type`` (ELM or VAL), ``target_id`` (optional set identifier, e.g.
blue/yellow), ``panel_set`` (physical set identified from the Panel_ref
signature: Gryfn4P, Gryfn2P, or unknown) and a boolean ``Valid_Range`` QC
flag. Tables are named
``QC_{ELM|VAL}[_{id}]_spectra_{VNIR|SWIR}[_gproN][_{extra}].{parquet|csv}``.

For every run a QC report (``QC_data/QC_spectra_report.json``) and one
saved figure per target (``QC_data/QC_plots/<target>_spectra.png``) are
also produced. The report currently contains descriptive statistics only
(``status.result = "not_evaluated"``); the known-good reference-set
equivalence test (APEx_SensorCalibration ET00 TOST / ET03 Wasserstein) will
plug into :func:`build_run_report` once that reference set exists.

Notes
-----
The script expects to be run from within a git repository, or with a
``--path`` argument pointing to the dataset root.

Raster extraction is cached: a table is only regenerated when it is
missing or older than its inputs (panel file or orthomosaic). Use
``--force`` to override.

Command-line Arguments
----------------------
--path : str, optional
    The path of the folder to look for QA shapefiles. Defaults to the
    root directory of the git repository.
--skipplot : flag
    Skip the per-run figure generation (reports are still written).
--reference-config : str, optional
    YAML sidecar describing the known-good reference spectra set.
    Not implemented yet; reserved for the ET00/ET03 equivalence test.

Notes
-----
Multi-run comparison (figures across runs/sites/nodes, --load-dir /
--save-dir sharing) lives in ``QA02_SpectralRunComparison.py``.
"""

# ==============================================================================

__title__ = "Spectral validation"
__author__ = "Arden Burrell"
__version__ = "v2.2(13.08.2026)"
__email__ = "arden.burrell@sydney.edu.au"


# ==============================================================================
# ========== Import core packages ==========
import os
import re
import sys
import json
import argparse
import pathlib
from dataclasses import dataclass
from typing import Dict, List, Any, Tuple, Optional, Callable

# ========== Import other packages ==========
import git
from git import exc as git_exc
import numpy as np
import pandas as pd
import rioxarray
import geopandas as gpd
from tqdm import tqdm
import warnings as warn
import matplotlib.pyplot as plt
import seaborn as sns

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
import Code.functions.spectral_qc as sq


# ==================================================================================
@dataclass(frozen=True)
class QAConfig:
    """Tunable settings for one spectral-QC invocation.

    All fixed facts and thresholds live on this object so callers pass a
    single ``cfg`` argument through the pipeline rather than relying on
    module-level constants. Use :func:`default_config` to build the
    default instance and ``dataclasses.replace`` for overrides.

    Attributes
    ----------
    schema_version : float
        Extracted-table schema version. Bump when the table columns
        change in a non-backwards-compatible way.
    valid_sensors : tuple of str
        Sensor platform folder names handled by this script.
    required_columns : tuple of str
        Columns every extracted spectra table must contain.
    radiance_int_max : int
        An integer table whose max value is below this triggers the
        reflectance-vs-radiance warning (int tables are 0-10000 scaled).
    radiance_float_max : float
        A float table whose max value is above this triggers the
        reflectance-vs-radiance warning (float tables are 0-1 scaled).
    report_filename : str
        Name of the per-run QC report JSON written into ``QC_data/``.
    plots_dirname : str
        Name of the figure folder inside ``QC_data/``.
    """
    schema_version: float = 2.2
    valid_sensors: Tuple[str, ...] = ("GOBI", "CALVIS")
    required_columns: Tuple[str, ...] = (
        "band", "wavelength", "value", "Panel_ref", "sensor",
        "EM_Region", "gpro_nu", "target_type", "panel_set")
    radiance_int_max: int = 100
    radiance_float_max: float = 13.0
    report_filename: str = "QC_spectra_report.json"
    plots_dirname: str = "QC_plots"

    def bad_wavelengths(self) -> Dict[str, Dict[str, List[Tuple[float, float]]]]:
        """Return known-bad wavelength ranges (nm) per sensor and EM region.

        Delegates to :func:`Code.functions.spectral_qc.default_bad_wavelengths`
        (shared with QA02) — see that function for the range rationale.

        Returns
        -------
        dict of str to dict of str to list of tuple of float
            ``{sensor: {EM_Region: [(lo_nm, hi_nm), ...]}}`` (inclusive).
        """
        return sq.default_bad_wavelengths()


def default_config() -> QAConfig:
    """Return the default :class:`QAConfig` for this tool."""
    return QAConfig()


# ==================================================================================
def main(
        args: argparse.Namespace,
        path: pathlib.Path,
    ) -> pd.DataFrame:
    """Run the ELM validation extraction and per-run QC reporting pipeline.

    Searches the provided path for QC panel vector files, groups them by
    run, extracts the spectra tables, and writes a QC report JSON plus a
    spectra figure into each run's ``QC_data/`` folder.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments.
    path : pathlib.Path
        Root directory to search for QC panel files.

    Returns
    -------
    pandas.DataFrame
        One row per run/target summarising the report outcome.
    """
    cfg = default_config()

    # ========== Load the (future) known-good reference set ==========
    reference_set = load_reference_set(
        pathlib.Path(args.reference_config) if args.reference_config else None)

    # ========== Find the panel vector files and group them by run ==========
    Panel_files = locate_qc_panels(
        path, file_type=args.type, exclude_dirs=args.exclude_dir,
        verbose=args.verbose, valid_sensors=cfg.valid_sensors,
        allow_multi_gpro=args.allow_multi_gpro)
    runs: Dict[pathlib.Path, List[Dict[str, Any]]] = {}
    for panel in Panel_files:
        runs.setdefault(panel["path"].parents[2], []).append(panel)

    summary_rows: List[Dict[str, Any]] = []

    # ========== Extract spectra + build the per-run QC report ==========
    for _run_dir, run_panels in tqdm(runs.items(), total=len(runs), desc="Processing runs"):
        run_tables: List[pd.DataFrame] = []
        for panel in run_panels:
            run_tables.extend(extract_panel_spectra(panel, args, cfg, path))

        meta = {k: run_panels[0][k] for k in
                ["node", "project", "site", "sensor", "date", "run"]}
        if len(run_tables) == 0:
            summary_rows.append({
                **_display_meta(meta),
                "target": "; ".join(p["panel_name"] for p in run_panels),
                "status": "skipped",
                "reason": "no spectra tables extracted"})
            continue

        # +++++ Report + figure live beside the panel files in QC_data +++++
        run_df = pd.concat(run_tables, ignore_index=True)
        report = build_run_report(run_df, meta, cfg, reference_set=reference_set)
        qc_dir = run_panels[0]["path"].parent
        _save_run_report(report, qc_dir / cfg.report_filename, verbose=args.verbose)
        if not args.skipplot:
            plot_run_spectra(run_df, qc_dir / cfg.plots_dirname, cfg)
        summary_rows.extend(_summary_rows(meta, report))

    # ========== Print the end-of-run summary ==========
    summary = _print_run_summary(summary_rows)
    return summary


# ==================================================================================
def load_reference_set(config_path: Optional[pathlib.Path]) -> Optional[Dict[str, Any]]:
    """Load the known-good reference spectra set (stub).

    Placeholder for the future reference-set loader. The YAML sidecar will
    point at known-good spectra tables and per-EM-region equivalence
    margins once the statistical test under development in
    APEx_SensorCalibration (ET00 mean-TOST / ET03 Wasserstein-1) is
    finalised.

    Parameters
    ----------
    config_path : pathlib.Path or None
        Path to the reference-set YAML sidecar, or None when no reference
        set is configured.

    Returns
    -------
    dict or None
        The loaded reference set, or None when *config_path* is None.

    Raises
    ------
    NotImplementedError
        Whenever *config_path* is provided (loader not implemented yet).
    """
    if config_path is None:
        return None
    raise NotImplementedError(
        "Reference-set loading is not implemented yet. Once the ET00/ET03 "
        "equivalence test (APEx_SensorCalibration) and the known-good run set "
        "are finalised, this loader will parse the YAML sidecar and return the "
        "reference spectra + margins consumed by build_run_report().")


# ==================================================================================
def build_run_report(
        run_df: pd.DataFrame,
        meta: Dict[str, Any],
        cfg: QAConfig,
        reference_set: Optional[Dict[str, Any]] = None,
        test_callable: Optional[Callable[[pd.DataFrame, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
    """Build the per-run spectral QC report dictionary.

    Currently produces descriptive statistics per target and EM region
    (residuals of the per-band mean reflectance against the nominal
    ``Panel_ref`` value, with known-bad bands excluded). The pass/fail
    verdict is deliberately ``"not_evaluated"`` until the known-good
    reference set exists.

    Parameters
    ----------
    run_df : pd.DataFrame
        All extracted spectra tables for one run, concatenated.
    meta : dict
        Run metadata (``node``, ``project``, ``site``, ``sensor``,
        ``date``, ``run``).
    cfg : QAConfig
        Tunable settings (schema version, bad bands).
    reference_set : dict, optional
        Known-good reference spectra + margins (from
        :func:`load_reference_set`). None until implemented.
    test_callable : callable, optional
        Future equivalence test with signature
        ``test(run_df, reference_set) -> dict`` returning at least a
        ``"result"`` key (e.g. pass/fail/inconclusive per the ET00/ET03
        verdict logic). None until implemented.

    Returns
    -------
    dict
        JSON-serialisable report (see ``QC_spectra_report.json``).
    """
    # ========== Descriptive stats per target x EM region ==========
    bad_lookup = cfg.bad_wavelengths()
    targets: Dict[str, Any] = {}
    for (target, region), tdf in run_df.groupby(["panel_name", "EM_Region"]):
        sensor = str(tdf["sensor"].iloc[0])
        bad = bad_lookup.get(sensor, {}).get(str(region), [])
        stats = _target_region_stats(tdf, bad)
        stats["panel_set"] = (str(tdf["panel_set"].iloc[0])
                              if "panel_set" in tdf.columns else "unknown")
        targets.setdefault(str(target), {})[str(region)] = stats

    # ========== Assemble the report ==========
    report = ({
        "schema_version": cfg.schema_version,
        "generated": pd.Timestamp.now(tz="UTC").isoformat(),
        "run": {k: (str(v) if v is not None else None) for k, v in meta.items()},
        "status": _evaluate_status(run_df, reference_set, test_callable),
        "targets": targets,
    })
    return report


# ==================================================================================
def _evaluate_status(
        run_df: pd.DataFrame,
        reference_set: Optional[Dict[str, Any]],
        test_callable: Optional[Callable[[pd.DataFrame, Dict[str, Any]], Dict[str, Any]]],
    ) -> Dict[str, Any]:
    """Evaluate the run's QC status via the pluggable equivalence test.

    Parameters
    ----------
    run_df : pd.DataFrame
        All extracted spectra tables for one run, concatenated.
    reference_set : dict or None
        Known-good reference spectra + margins.
    test_callable : callable or None
        Equivalence test; see :func:`build_run_report`.

    Returns
    -------
    dict
        ``{"result": ..., ...}``; ``"not_evaluated"`` while the test or
        the reference set is missing.
    """
    if reference_set is None or test_callable is None:
        return ({
            "result": "not_evaluated",
            "reason": (
                "No known-good reference set available yet. Descriptive "
                "statistics only; the equivalence test under development in "
                "APEx_SensorCalibration (ET00 mean-TOST / ET03 Wasserstein-1) "
                "will be wired in here once the reference set exists."),
        })
    return test_callable(run_df, reference_set)


# ==================================================================================
def _target_region_stats(
        df: pd.DataFrame,
        bad_ranges: List[Tuple[float, float]],
    ) -> Dict[str, Any]:
    """Descriptive residual statistics for one target x EM-region table.

    Residuals are per-band mean reflectance (percent) minus the nominal
    ``Panel_ref`` value, computed over the good bands only (bands whose
    wavelength falls outside the known-bad ranges).

    Parameters
    ----------
    df : pd.DataFrame
        Extracted spectra rows for a single ``panel_name`` / ``EM_Region``
        combination.
    bad_ranges : list of tuple of float
        Inclusive ``(lo_nm, hi_nm)`` wavelength ranges to exclude.

    Returns
    -------
    dict
        ``n_bands``, ``bad_wavelengths_excluded_nm``, ``n_bands_excluded``,
        and per-``Panel_ref`` pixel counts, valid-range fraction, and
        residual statistics (mean/std/rmse plus the worst band).
    """
    d = df.assign(refl_pct=sq.reflectance_pct(df["value"]))
    bad_mask = sq.bad_wavelength_mask(d["wavelength"], bad_ranges)
    good = d[~bad_mask]

    panels: Dict[str, Any] = {}
    for ref, rdf in good.groupby("Panel_ref"):
        n_bands = int(rdf["band"].nunique())
        per_band = rdf.groupby("band").agg(
            mean_refl=("refl_pct", "mean"),
            wavelength=("wavelength", "first"))
        resid = per_band["mean_refl"] - float(ref) # type: ignore
        worst = resid.abs().idxmax()
        panels[str(ref)] = ({
            "n_pixels": int(round(len(rdf) / max(n_bands, 1))),
            "valid_range_fraction": (
                float(rdf["Valid_Range"].mean()) if "Valid_Range" in rdf else None),
            "mean_residual_pct": float(resid.mean()),
            "std_residual_pct": float(resid.std()),
            "rmse_residual_pct": float(np.sqrt((resid ** 2).mean())),
            "worst_band": ({
                "band": int(worst), # type: ignore
                "wavelength_nm": float(per_band.loc[worst, "wavelength"]), # type: ignore
                "residual_pct": float(resid.loc[worst]), # type: ignore
            }),
        })

    return ({
        "n_bands": int(d["band"].nunique()),
        "bad_wavelengths_excluded_nm": [list(r) for r in bad_ranges],
        "n_bands_excluded": int(d.loc[bad_mask, "band"].nunique()),
        "panels": panels,
    })


# ==================================================================================
def _save_run_report(
        report: Dict[str, Any],
        outpath: pathlib.Path,
        verbose: bool = False,
    ) -> None:
    """Write the per-run QC report dictionary to a JSON file.

    Parameters
    ----------
    report : dict
        Report from :func:`build_run_report`.
    outpath : pathlib.Path
        Destination JSON path (inside the run's ``QC_data/`` folder).
    verbose : bool, optional
        Print the saved path. Default is False.

    Returns
    -------
    None
    """
    with open(outpath, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    if verbose:
        tqdm.write(f"Saved QC report: {outpath}")


# ==================================================================================
def plot_run_spectra(
        run_df: pd.DataFrame,
        plots_dir: pathlib.Path,
        cfg: QAConfig,
    ) -> None:
    """Save one spectra figure per target for a single run.

    Plots per-panel reflectance against wavelength (percentile-interval
    band across pixels), one facet per EM region, with dashed lines at the
    nominal ``Panel_ref`` values. More figure types (e.g. residuals vs the
    known-good reference) will be added once the reference set exists.

    Parameters
    ----------
    run_df : pd.DataFrame
        All extracted spectra tables for one run, concatenated.
    plots_dir : pathlib.Path
        The run's ``QC_data/QC_plots`` folder; created if missing.
    cfg : QAConfig
        Tunable settings (bad-band definitions).

    Returns
    -------
    None
    """
    plots_dir.mkdir(parents=False, exist_ok=True)
    sns.set_style("whitegrid")
    df = run_df.assign(refl_pct=sq.reflectance_pct(run_df["value"]))
    # Fall back to band index if the rasters carried no wavelength metadata
    x_col = "wavelength" if df["wavelength"].notna().any() else "band"

    # +++++ Mask known-bad wavelengths (NaN -> line gap) so they don't distort the axis +++++
    bad_lookup = cfg.bad_wavelengths()
    sensor = str(df["sensor"].iloc[0])
    for region, ranges in bad_lookup.get(sensor, {}).items():
        region_rows = df["EM_Region"] == region
        df.loc[region_rows & sq.bad_wavelength_mask(df["wavelength"], ranges), "refl_pct"] = np.nan

    for target, tdf in df.groupby("panel_name"):
        refs = sorted(tdf["Panel_ref"].unique())
        palette = dict(zip(refs, sns.color_palette("colorblind", len(refs))))
        g = sns.relplot(
            data=tdf, x=x_col, y="refl_pct",
            col="EM_Region", hue="Panel_ref", palette=palette,
            kind="line", errorbar="pi",
            facet_kws={"sharex": False, "sharey": True})
        # +++++ Dashed nominal reference line per panel +++++
        for ax in g.axes.flat:
            for ref in refs:
                ax.axhline(float(ref), color=palette[ref], linestyle="--",
                           linewidth=0.8, alpha=0.7)
            # Reflectance >100% is non-physical; clamp so artefact spikes
            # (visible at the clip edge) don't compress the real spectra.
            ax.set_ylim(-5, 120)
        g.set_axis_labels(
            "Wavelength (nm)" if x_col == "wavelength" else "Band index",
            "Reflectance (%)")
        row0 = tdf.iloc[0]
        g.figure.suptitle(
            f"{row0['sensor']} {row0['run']} {pd.Timestamp(row0['date']).date()} — {target}",
            y=0.98, fontweight="bold")
        g.figure.subplots_adjust(top=0.88)
        outpath = plots_dir / f"{target}_spectra.png"
        g.figure.savefig(outpath.as_posix(), dpi=150)
        plt.close(g.figure)


# ==================================================================================
def _display_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Format run metadata for the summary table.

    Parameters
    ----------
    meta : dict
        Run metadata (``project``, ``sensor``, ``date``, ``run``, ...).

    Returns
    -------
    dict
        Display-ready subset with the date as ``YYYY-MM-DD`` or None.
    """
    date = meta.get("date")
    return ({
        "project": meta.get("project"),
        "sensor": meta.get("sensor"),
        "date": date.strftime("%Y-%m-%d") if date is not None and pd.notna(date) else None,
        "run": meta.get("run"),
    })


# ==================================================================================
def _summary_rows(meta: Dict[str, Any], report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten a run report into one summary row per target.

    Parameters
    ----------
    meta : dict
        Run metadata.
    report : dict
        Report from :func:`build_run_report`.

    Returns
    -------
    list of dict
        Rows for :func:`_print_run_summary`.
    """
    rows = []
    for target, regions in report["targets"].items():
        residuals = [p["mean_residual_pct"]
                     for r in regions.values() for p in r["panels"].values()]
        n_px = sum(p["n_pixels"]
                   for r in regions.values() for p in r["panels"].values())
        worst = max(residuals, key=abs) if residuals else None
        panel_set = next((r.get("panel_set") for r in regions.values()), None)
        rows.append({
            **_display_meta(meta),
            "target": target,
            "set": panel_set,
            "regions": "+".join(sorted(regions)),
            "n_px": n_px,
            "worst_resid_pct": worst,
            "status": report["status"]["result"],
            "reason": None,
        })
    return rows


# ==================================================================================
def _print_run_summary(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """Print the end-of-run summary tables and return the DataFrame.

    Rows with ``status == "skipped"`` are printed separately from
    reported rows (QA01 pattern). Once the equivalence test lands, the
    reported group will split further into passed/failed.

    Parameters
    ----------
    rows : list of dict
        Rows from :func:`_summary_rows` (plus skipped-run rows).

    Returns
    -------
    pandas.DataFrame
        The full summary with a fixed column order.
    """
    columns = ["project", "sensor", "date", "run", "target", "set", "regions",
               "n_px", "worst_resid_pct", "status", "reason"]
    df = pd.DataFrame(rows, columns=columns)
    if df.empty:
        print("\nNo runs to summarise.")
        return df

    disp = df.copy()
    disp["worst_resid_pct"] = disp["worst_resid_pct"].apply(
        lambda v: "" if v is None or pd.isna(v) else f"{v:+.2f}")
    disp["n_px"] = disp["n_px"].apply(
        lambda v: "" if v is None or pd.isna(v) else f"{int(v)}")
    for col in ["reason", "regions", "target", "set"]:
        disp[col] = disp[col].fillna("")

    skipped  = disp[disp["status"] == "skipped"]
    reported = disp[disp["status"] != "skipped"]

    if not skipped.empty:
        cols = ["project", "sensor", "date", "run", "target", "status", "reason"]
        print(f"\nSKIPPED ({len(skipped)}):")
        print(skipped[cols].to_string(index=False))
    if not reported.empty:
        cols = ["project", "sensor", "date", "run", "target", "set", "regions",
                "n_px", "worst_resid_pct", "status"]
        print(f"\nREPORTED ({len(reported)}):")
        print(reported[cols].to_string(index=False))
    return df


# ==================================================================================
def extract_panel_spectra(
        panel: Dict[str, Any],
        args: argparse.Namespace,
        cfg: QAConfig,
        root_path: pathlib.Path,
    ) -> List[pd.DataFrame]:
    """Extract spectral data for a given panel from associated rasters.

    Reads the panel vector file, then iterates over each raster entry in
    ``panel["rasters"]``.  A raster is (re)processed via
    :func:`_process_raster` only when its output table is missing or
    older than its inputs (panel file or orthomosaic), or when
    ``--force`` is set.  Each output file is then loaded and returned
    as a list of DataFrames.

    Parameters
    ----------
    panel : dict
        Dictionary produced by :func:`locate_qc_panels` containing
        panel metadata and raster information.
    args : argparse.Namespace
        Parsed command-line arguments.  Relevant flags:

        - ``args.force`` -- re-create output files even if up to date.
        - ``args.type``  -- ``"csv"`` or ``"parquet"``.
        - ``args.skip_processing`` -- never process, only load.
    cfg : QAConfig
        Tunable settings for this invocation.
    root_path : pathlib.Path
        Repository/data root used for display-friendly relative paths.

    Returns
    -------
    list of pd.DataFrame
        One DataFrame per successfully loaded raster output file.
    """
    # ========== Load the shape files ==========
    shpdf     = gpd.read_file(panel["path"])
    # ========== Validate the vector file structure ==========
    expected_columns = ["geometry", "Panel_ref"]
    if not all(col in shpdf.columns for col in expected_columns):
        raise ValueError(
            f"QC panel file {panel['path']} does not have the expected columns "
            f"{expected_columns}. Found columns: {list(shpdf.columns)}. "
            "Fix the file to match the AerialDataQC protocol before re-running.")

    if shpdf.crs is None:
        raise ValueError(f"Shapefile {panel['path']} does not have a CRS defined. Please set a CRS on the shapefile before running this script.")

    # ========== Identify the physical panel set from its Panel_ref signature ==========
    # The filename encodes usage (QC_VAL_north, QC_VAL_blue, ...); the nominal
    # reflectances identify the hardware so QA02 can compare like with like.
    panel["panel_set"] = sq.identify_panel_set(shpdf["Panel_ref"])
    if panel["panel_set"] == "unknown":
        warn.warn(
            f"Panel file {panel['path'].name} has Panel_ref values "
            f"{sorted(shpdf['Panel_ref'].unique().tolist())} which do not match "
            f"any standard APPN panel set {list(sq.known_panel_sets())}. "
            "Recording panel_set='unknown'; check the file if this set should "
            "be a standard one.")

    QC_tables: List[pd.DataFrame] = []

    # ========== Open dataset(s) ==========
    for ras in panel["rasters"].values():
        # +++++ Skip processing when the output is newer than its inputs +++++
        up_to_date = cf.outputs_up_to_date(
            [ras["outfile"]], [panel["path"], ras["InputRaster"]])
        if args.skip_processing:
            if not ras["outfile"].is_file():
                tqdm.write(f"Skipping raster {ras['InputRaster']} (no existing output file). Use without --skip-processing to generate it.")
                continue
        elif args.force or not up_to_date:
            try:
                _process_raster(
                    ras, shpdf, panel, root_path,
                    file_type=args.type, keep_xy=args.keep_xy)
            except Exception as er:
                tqdm.write(f"Error processing raster {ras['InputRaster']}: {er}. Skipping raster.")
                continue

        # ========== Load the data ==========
        try:
            if args.type == "csv":
                df = pd.read_csv(ras["outfile"])
            elif args.type == "parquet":
                df = pd.read_parquet(ras["outfile"])
        
        except Exception as er: 
            warn.warn(f"Could not read output file {ras['outfile']} for raster {ras['InputRaster']}. Error: {er}. May require manual inspection of the file and raster. Skipping file.")
            continue

        # +++++ Auto-migrate tables written before the current schema +++++
        if (not args.skip_processing and not args.force
                and set(cfg.required_columns) - set(df.columns)):
            tqdm.write(f"Output file {ras['outfile'].name} predates the current schema; regenerating.")
            try:
                _process_raster(
                    ras, shpdf, panel, root_path,
                    file_type=args.type, keep_xy=args.keep_xy)
                df = (pd.read_csv(ras["outfile"]) if args.type == "csv"
                      else pd.read_parquet(ras["outfile"]))
            except Exception as er:
                tqdm.write(f"Error regenerating {ras['outfile']}: {er}. Skipping raster.")
                continue
        
        df, check = _check_table_structure(
            panel, ras, df, cfg, no_radiance_check=args.no_radiance_check)
        if check:
            QC_tables.append(df)
        else:
            warn.warn(f"DataFrame for raster {ras['InputRaster']} does not meet QC requirements.")
            continue
    return QC_tables


def _check_table_structure(
        panel: Dict[str, Any],
        ras: Dict[str, Any],
        df: pd.DataFrame,
        cfg: QAConfig,
        no_radiance_check: bool = False,
    ) -> Tuple[pd.DataFrame, bool]:
    """Check if the DataFrame has the expected structure for QC tables.

    Parameters
    ----------
    panel : dict
        Panel metadata dictionary (from :func:`locate_qc_panels`).
    ras : dict
        Raster entry for the table being checked.
    df : pd.DataFrame
        The DataFrame to check.
    cfg : QAConfig
        Tunable settings (required columns, radiance thresholds).
    no_radiance_check : bool, optional
        Disable the reflectance-vs-radiance range check. Default False.

    Returns
    -------
    pd.DataFrame
        The (unmodified) DataFrame.
    bool
        True if the DataFrame has the expected structure, False otherwise.
    """
    valid = True
    # ========== Require the standard output columns ==========
    missing = set(cfg.required_columns) - set(df.columns)
    if missing:
        warn.warn(
            f"Output file {ras['outfile']} is missing required columns {missing}. "
            "Re-run with --force to regenerate it with the current schema.")
        return df, False

    # ========== Check if the Dataframe has values in the expected ranges ==========
    # This is a check for reflectance vs radiance
    if panel["sensor"] in cfg.valid_sensors and not no_radiance_check:
        if pd.api.types.is_integer_dtype(df["value"]):
            if df["value"].max() < cfg.radiance_int_max:
                warn.warn(f"Maximum value in DataFrame for raster {ras['InputRaster']} is less than {cfg.radiance_int_max}. This may indicate that the values are in reflectance rather than radiance, which is unexpected for this sensor. Please check the processing step and ensure that the correct values are being extracted. Skipping file.")
                valid = False
        elif pd.api.types.is_float_dtype(df["value"]):
            if df["value"].max() > cfg.radiance_float_max:
                warn.warn(f"Maximum value in DataFrame for raster {ras['InputRaster']} is greater than {cfg.radiance_float_max}. This may indicate that the values are in reflectance rather than radiance, which is unexpected for this sensor. Please check the processing step and ensure that the correct values are being extracted. Skipping file.")
                valid = False
    return df, valid


def _process_raster(
        ras: Dict[str, Any],
        shpdf: gpd.GeoDataFrame,
        panel: Dict[str, Any],
        root_path: pathlib.Path,
        file_type: str = "parquet",
        keep_xy: bool = False,
    ) -> None:
    """Extract panel-polygon pixels from a raster and save the long table.

    Opens the raster lazily, then for each panel polygon clips to the
    polygon's bounding box first (a windowed read of a few hundred
    pixels) before masking to the exact geometry. This avoids reading
    the full multi-GB orthomosaic: only the pixels around each panel
    ever leave the disk (~20x faster than a whole-raster clip on a
    16 GB orthomosaic).

    Parameters
    ----------
    ras : dict
        Raster entry produced by :func:`locate_qc_panels` with keys
        ``"InputRaster"`` (*pathlib.Path*), ``"outfile"``
        (*pathlib.Path*), ``"type"`` (*str*), and ``"gpro_nu"``
        (*int*).
    shpdf : geopandas.GeoDataFrame
        Panel vector geometries with a ``Panel_ref`` column.
    panel : dict
        Panel metadata dictionary (from :func:`locate_qc_panels`).
        Values for keys ``"node"``, ``"project"``, ``"site"``,
        ``"sensor"``, ``"date"``, ``"run"``, ``"panel_name"``,
        ``"target_type"``, and ``"target_id"`` are written as columns
        in the output file.
    root_path : pathlib.Path
        Repository/data root used for display-friendly relative paths.
    file_type : str, optional
        ``"csv"`` or ``"parquet"``. Default is ``"parquet"``.
    keep_xy : bool, optional
        If True, retain the per-pixel ``x`` and ``y`` coordinate columns
        (in the raster's native CRS) in the output table. Default False.

    Returns
    -------
    None

    Raises
    ------
    ValueError
        If the raster does not have a CRS defined, or no panel polygon
        yielded any pixels.
    """
    # +++++ open the raster dataset lazily (masked=True -> nodata becomes NaN) +++++
    _ras_display = pathlib.Path(ras["InputRaster"]).relative_to(root_path)
    tqdm.write(f"Processing raster: {_ras_display} started at {pd.Timestamp.now()}.")
    ds  = rioxarray.open_rasterio(ras["InputRaster"], masked=True)
    crs = ds.rio.crs # type: ignore
    if crs is None:
        raise ValueError(f"Raster {ras['InputRaster']} does not have a CRS defined. Please check the raster file and ensure it has a defined CRS.")

    # +++++ Per-band wavelengths + native dtype from the raster metadata +++++
    wavelengths, src_dtype = cf.band_wavelengths(ras["InputRaster"])

    # +++++ Clip each polygon via its own bounding box (windowed read) +++++
    shpdf_r = shpdf.to_crs(crs)
    frames: List[pd.DataFrame] = []
    try:
        for idx, row in shpdf_r.iterrows():
            try:
                sub = ds.rio.clip_box(*row.geometry.bounds) # type: ignore
                sub = sub.rio.clip([row.geometry], shpdf_r.crs, drop=True)
            except Exception as er:
                warn.warn(
                    f"Could not clip polygon {idx} (Panel_ref={row['Panel_ref']}) "
                    f"from {_ras_display}: {er}. Skipping polygon.")
                continue
            df = sub.to_dataframe(name="value").reset_index()
            df = df.dropna(subset=["value"])
            df = df.drop(columns=["spatial_ref"], errors="ignore")
            if not keep_xy:
                df = df.drop(columns=["x", "y"], errors="ignore")
            df["Panel_ref"] = row["Panel_ref"]
            frames.append(df)
    finally:
        # Close the GDAL handle now; open handles at interpreter shutdown
        # trigger a harmless-but-noisy "Error in sys.excepthook" teardown race.
        ds.close()

    if len(frames) == 0:
        raise ValueError(
            f"No panel polygons yielded pixels from {ras['InputRaster']}. "
            "Check that the panel geometries overlap the raster extent.")
    gdf = pd.concat(frames, ignore_index=True)

    # +++++ Restore the native dtype (masked read promotes to float) +++++
    _np_dtype = np.dtype(src_dtype)
    if np.issubdtype(_np_dtype, np.integer):
        gdf["value"] = gdf["value"].round().astype(_np_dtype)

    # +++++ Add the wavelength and panel metadata columns +++++
    gdf["wavelength"] = gdf["band"].map(wavelengths)
    for vname in ["node", "project", "site", "sensor", "date", "run",
                  "panel_name", "target_type", "target_id", "panel_set"]:
        # Skip if the value is not in the panel dict for some reason
        if vname in panel:
            gdf[vname] = panel[vname]
    # Add the EM range type and gpro number from the raster dict to the DataFrame
    gdf["EM_Region"] = ras["type"]
    gdf["gpro_nu"]   = ras["gpro_nu"] # this only matter if there a multiple gpros
    # +++++ Add a boolean QC check for the expected +++++
    # check if value column is int or float
    if pd.api.types.is_integer_dtype(gdf["value"]):
        gdf["Valid_Range"] = (gdf["value"] > 0) & (gdf["value"] <= 10000)
    elif pd.api.types.is_float_dtype(gdf["value"]):
        gdf["Valid_Range"] = (gdf["value"] > 0) & (gdf["value"] <= 1.0)
    else:
        gdf["Valid_Range"] = False

    # ========= Save the DataFrame to file ==========
    if file_type == "csv":
        gdf.to_csv(ras["outfile"].as_posix(), index=False)
    elif file_type == "parquet":
        gdf.to_parquet(ras["outfile"].as_posix(), index=False)


def locate_qc_panels(
        path: pathlib.Path,
        file_type: str = "parquet",
        exclude_dirs: Optional[List[str]] = None,
        verbose: bool = False,
        valid_sensors: Tuple[str, ...] = ("GOBI", "CALVIS"),
        allow_multi_gpro: bool = False,
    ) -> List[Dict[str, Any]]:
    """Find spectral validation panel vector files in the given directory tree.

    Recursively searches ``path`` for QC panel files following the official
    AerialDataQC naming convention
    ``QC_{ELM|VAL}[_{TargetIdentifier}]_Panels[_{Extra}].geojson`` (GeoJSON
    preferred, shapefile accepted) stored under ``<run>/T1_proc/QC_data/``,
    and returns a list of dictionaries containing panel metadata and
    associated raster information.

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively.
    file_type : str, optional
        Extension of the extracted-table output files (``"parquet"`` or
        ``"csv"``). Default is ``"parquet"``.
    exclude_dirs : list of str, optional
        Directory names to exclude; any panel whose path contains one of
        these names is skipped. Default is None (no exclusions).
    verbose : bool, optional
        Print extra diagnostic messages. Default is False.
    valid_sensors : tuple of str, optional
        Sensor platform folder names to accept. Defaults to
        ``("GOBI", "CALVIS")``.
    allow_multi_gpro : bool, optional
        Process runs that contain more than one ``.gpro`` folder instead
        of skipping them. Intended for debugging only: with multiple
        ``.gpro`` folders the product set is ambiguous, so extracted
        spectra (and future accuracy reports) may be misleading.
        Default is False (skip with a warning).

    Returns
    -------
    list of dict
        Each dictionary contains the following keys:

        - **path** (*pathlib.Path*) -- Path to the panel vector file.
        - **panel_name** (*str*) -- Stem of the panel vector file.
        - **target_type** (*str*) -- ``"ELM"`` or ``"VAL"``.
        - **target_id** (*str or None*) -- Optional target identifier
          (e.g. ``blue``/``yellow``); None for the single-set names.
        - **node**, **project**, **site**, **sensor**, **run** (*str*) --
          Metadata parsed from the APPN folder structure.
        - **date** (*pd.Timestamp*) -- Flight date.
        - **outdir** (*pathlib.Path*) -- Output directory for spectral tables.
        - **rasters** (*dict*) -- Mapping of raster names to dicts with
          keys ``"InputRaster"``, ``"outfile"``, ``"type"``, and
          ``"gpro_nu"``.

    Raises
    ------
    ValueError
        If no QC panel files are found in ``path``.
    NotImplementedError
        If a sensor name is not handled by the current implementation.
    """
    # +++++ Find all the panel files (geojson preferred, shp accepted) +++++
    print(f"Scanning directory for panel files and rasters. {pd.Timestamp.now()}")

    # Official pattern: QC_{TargetType}[_{TargetIdentifier}]_Panels[_{Extra}]
    # (AerialDataQC protocol); only ELM and VAL target types carry panels.
    name_re       = re.compile(r"^QC_(ELM|VAL)(?:_(.+?))?_Panels(?:_(.+))?$")
    shp_files     = [f for f in path.rglob("QC_*.shp") if name_re.match(f.stem)]
    geojson_files = [f for f in path.rglob("QC_*.geojson") if name_re.match(f.stem)]

    # +++++ Prefer geojson when both share the same parent dir + stem +++++
    geojson_keys = {(f.parent, f.stem) for f in geojson_files}
    shp_files    = [f for f in shp_files if (f.parent, f.stem) not in geojson_keys]
    files        = sorted(geojson_files + shp_files)

    if len(files) == 0:
        raise ValueError(
            f"No QC panel files found in {path}. Please check the path and file "
            "naming conventions. Expected pattern: "
            "QC_{ELM|VAL}[_{TargetIdentifier}]_Panels[_{Extra}].geojson (or .shp) "
            "under <run>/T1_proc/QC_data/ (see the AerialDataQC protocol).")

    # ========== Filter out excluded directories ==========
    if exclude_dirs:
        exclude_set = set(exclude_dirs)
        before = len(files)
        files = [f for f in files if not (set(p.name for p in f.parents) & exclude_set)]
        if verbose and len(files) < before:
            print(f"Excluded {before - len(files)} panel file(s) matching --exclude-dir {exclude_dirs}")

    pan_list    = [] # List of dicts with information about the panel files
    # ========== loop over each project and write out files ==========
    for panel in files:
        # ========== Enforce the official storage location: <run>/T1_proc/QC_data/ ==========
        if panel.parent.name != "QC_data" or panel.parents[1].name != "T1_proc":
            if verbose:
                tqdm.write(f"Skipping {panel}: not under T1_proc/QC_data (see the AerialDataQC protocol).")
            continue

        # ========== Require exactly one .gpro per run ==========
        # Multiple .gpro folders usually mean the run is being actively
        # debugged/reprocessed; extracting spectra (and later accuracy
        # reports) from an ambiguous product set could be misleading.
        gpro_dirs = sorted(panel.parents[1].glob("*.gpro"))
        if len(gpro_dirs) > 1:
            if allow_multi_gpro:
                warn.warn(
                    f"Found {len(gpro_dirs)} .gpro folders in {panel.parents[1]} "
                    f"({[g.name for g in gpro_dirs]}). Processing anyway because "
                    "--allow-multi-gpro is set; treat the extracted spectra as "
                    "debugging output, not QC results.")
            else:
                warn.warn(
                    f"Found {len(gpro_dirs)} .gpro folders in {panel.parents[1]} "
                    f"({[g.name for g in gpro_dirs]}). Multiple .gpro folders in a "
                    "single run usually indicate an issue being actively debugged; "
                    "skipping this panel until the run is resolved to one .gpro "
                    "(or use --allow-multi-gpro to process it anyway).")
                continue

        # ========== Parse the APPN folder structure for metadata ==========
        parsed = cf.parse_APPN_dataset_path(panel)
        sensor = parsed["sensor"]
        if sensor not in valid_sensors:
            if verbose:
                warn.warn(f"Found panel file for sensor {sensor} which is not in the list of valid sensors {valid_sensors}. Skipping file: {panel}")
            continue

        # ========== Parse the panel filename for target metadata ==========
        match = name_re.match(panel.stem)
        assert match is not None  # guaranteed by the rglob filter above
        target_type, target_id, extra = match.group(1), match.group(2), match.group(3)

        # ========== Make a dict of information ==========
        p_dict = ({
            "path": panel,
            "panel_name": panel.stem,
            "target_type": target_type,
            "target_id": target_id,
            "outdir": panel.parent / "QC_Spectral_Tables",
            "node": parsed["node"],
            "project": parsed["project"],
            "site": parsed["site_folder"],
            "sensor": sensor,
            "date": parsed["date"],
            "run": parsed["run_folder"],
        })

        # ========= Make the output directory if it doesn't exist ==========
        p_dict["outdir"].mkdir(parents=False, exist_ok=True)

        # ========== Locate the raster data ==========
        rasters = ({})

        # +++++ Define which ortho types to search for, per sensor +++++
        if sensor in ["GOBI", "CALVIS"]:
            ortho_types = ["VNIR"]
            if sensor == "CALVIS":
                ortho_types.append("SWIR")
        else:
            raise NotImplementedError(f"Sensor {sensor} is not implemented. Valid sensors are {valid_sensors}. Please check the sensor name in the path and the list of valid sensors.")

        skip_panel = False
        for otype in ortho_types:
            orthos = list(panel.parents[1].glob(f"*.gpro/products/*_{otype}_Orthomosaic.bin"))
            if len(orthos) == 0:
                if verbose:
                    tqdm.write(f"No {otype} orthomosaic found for panel {panel}. Expected to find a file matching *.gpro/products/*_{otype}_Orthomosaic.bin in {panel.parents[1]}. Skipping {otype}.")
                skip_panel = True
                break
            elif len(orthos) > 1:
                if verbose:
                    tqdm.write(f"Multiple {otype} orthomosaics found for panel {panel}. Expected to find only one file matching *.gpro/products/*_{otype}_Orthomosaic.bin in {panel.parents[1]}. {orthos}")
            for nu, ortho in enumerate(orthos):
                # +++++ Protocol-style output name +++++
                stem_parts = ["QC", target_type]
                if target_id:
                    stem_parts.append(target_id)
                stem_parts += ["spectra", otype]
                if len(orthos) > 1:
                    stem_parts.append(f"gpro{nu}")
                if extra:
                    stem_parts.append(extra)
                name    = "_".join(stem_parts)
                outfile = p_dict["outdir"] / f"{name}.{file_type}"
                rasters[f"{otype}{nu}"] = ({
                    "InputRaster": ortho,
                    "outfile": outfile,
                    "type": otype,
                    "gpro_nu": nu,
                })
        if skip_panel:
            continue

        # ========= Check if rasters is empty ==========
        if not rasters:
            if verbose:
                tqdm.write(f"No rasters found for panel {panel}. Skipping panel.")
            continue

        # ========== Add the rasters to the dict ==========
        p_dict["rasters"] = rasters
        pan_list.append(p_dict)
    return pan_list


# ==================================================================================
if __name__ == '__main__':
    # ========== Parse args ==========
    parser = argparse.ArgumentParser(description="Extract ELM/VAL panel spectra from hyperspectral orthomosaics (single-run spectral QC).")
    parser.add_argument("--path", type=str, default=None, help="The path of the folder to look for QA panel files. By default it will search from the root dir of the git repo")
    parser.add_argument("-f","--force", default=False, action="store_true", help="Force the re-creation of output files even if they are up to date. Default is to skip files that are newer than their inputs (panel file and orthomosaic).")
    parser.add_argument("--type", type=str, default="parquet", choices=["parquet", "csv"], help="The file type for the output files. Default is parquet for more efficient storage and faster read/write times, but can be set to csv. Note that .parquet files will require additional dependencies to read and write.")
    parser.add_argument("-s","--skipplot", default=False, action="store_true", help="Skip the per-run figure generation. Reports and extracted tables are still produced.")
    parser.add_argument("--skip-processing", default=False, action="store_true", help="Never process rasters, only load existing output files for reporting. Useful for report-only re-runs.")
    parser.add_argument("-v", "--verbose", default=False, action="store_true", help="Enable verbose output for debugging and additional information during processing.")
    parser.add_argument("--no-radiance-check", default=False, action="store_true", help="Disable the reflectance vs radiance range check. Useful when processing data known to be in reflectance units.")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[], help="One or more directory names to exclude from the panel search. Any panel whose path contains a directory matching one of these names will be skipped. e.g. --exclude-dir 2025_TestData 2026_TestData")
    parser.add_argument("--keep-xy", default=False, action="store_true", help="Retain the per-pixel x and y coordinate columns (in the raster's native CRS) in the extracted spectra tables. By default these are dropped.")
    parser.add_argument("--allow-multi-gpro", default=False, action="store_true", help="Process runs that contain more than one .gpro folder instead of skipping them. Debugging only: the product set is ambiguous, so treat the extracted spectra as debugging output, not QC results.")
    parser.add_argument("--reference-config", type=str, default=None, help="YAML sidecar describing the known-good reference spectra set for the equivalence test. NOT IMPLEMENTED YET; reserved for the ET00/ET03 test under development in APEx_SensorCalibration.")

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
                f"This script was called from an unknown path ({os.getcwd()}). Must be in a git repo or provide a valid --path argument. Original error: {err}"
            ) from err

    # ========= Check if the provided path exists ==========
    # Resolve to absolute BEFORE chdir, otherwise a relative --path breaks.
    path = pathlib.Path(search_root).resolve()
    if not path.is_dir():
        raise NotADirectoryError(f"The provided path does not exist: {path}")
    os.chdir(path)

    # ========== Parse Args to main function ==========
    main(args, path)