"""Spectral check (QC02) — ELM/VAL panel spectra extraction (single-run spectral QC).

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

For every run the dual-file QC report contract (``QC_PIPELINE_PLAN.md``
§2) is written: ``QC_data/QC02_SpectralCheck_summary.yaml`` plus
``QC_data/QC02_SpectralCheck/QC02_SpectralCheck_detail.json`` (the
descriptive statistics embed under ``spectral_report``), and per
target x EM region the DHR overlay and delta figures
(``QC_data/QC02_SpectralCheck/QC_plots/*_dhr_{overlay,delta}.png``,
both carrying the observed p5-p95 percentile envelope).
Pre-contract loose outputs (``QC_spectra_report.json``) migrate into
the subfolder on first touch and retired ``*_spectra.png`` figures
(superseded by the overlay) are deleted; ``QC_Spectral_Tables/`` stays
at the top level. The run status stays
``not_evaluated``; the known-good reference-set
equivalence test (APEx_SensorCalibration ET00 TOST / ET03 Wasserstein)
will plug into :func:`build_run_report` once that reference set exists.

A value of exactly 0 in the extracted tables is the nodata sentinel
(e.g. SWIR gaps over a panel), not a real reflectance: zeros are
masked out of every statistic, the DHR comparison and the figures, and
the per-panel nodata fraction is reported (``nodata_zero_*`` advisory
checks; panels that are entirely nodata grade their stats
``all_nodata`` instead of NaN-propagating). Runs that carry two ELM
panel sets are supported: each ELM target resolves its physical set by
signature (the gpro pin only identifies the corrected set) and every
target keeps its own stats, checks and figures.

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
--save-dir sharing) lives in ``QA02_SpectralComparison.py``.
"""

# ==============================================================================

__title__ = "Spectral check"
__author__ = "Arden Burrell"
__version__ = "v3.4(01.09.2026)"
__email__ = "arden.burrell@sydney.edu.au"


# ==============================================================================
# ========== Import core packages ==========
import os
import re
import sys
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
import matplotlib.ticker as mticker
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
import Code.functions.qc_report as qr


# ==================================================================================
def main(
        args: argparse.Namespace,
        path: pathlib.Path,
    ) -> pd.DataFrame:
    """Run the ELM validation extraction and per-run QC reporting pipeline.

    Searches the provided path for QC panel vector files, groups them by
    run, extracts the spectra tables, and writes the contract QC report
    plus the DHR overlay/delta figures into each run's ``QC_data/``
    folder.

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

    # ========== Load the DHR-comparison limits (§5, advisory) ==========
    dhr_spec, dhr_snapshot = load_spectral_limits(pathlib.Path(args.spec))

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

        # +++++ Contract report + figures live in the QC02 subfolder (§4) +++++
        run_df = pd.concat(run_tables, ignore_index=True)
        report = build_run_report(run_df, meta, cfg, reference_set=reference_set,
                                  spec=dhr_spec)
        qc_dir = run_panels[0]["path"].parent
        _migrate_legacy_outputs(qc_dir, cfg, verbose=args.verbose)
        # +++++ Observed-vs-expected DHR comparison (§5b, advisory) +++++
        dhr = build_dhr_comparison(run_df, meta, qc_dir, cfg,
                                   spec=dhr_spec, snapshot=dhr_snapshot,
                                   skipplot=args.skipplot,
                                   verbose=args.verbose)
        # figures first: the contract artifact list globs them
        _write_contract_report(qc_dir, meta, report, cfg, dhr=dhr,
                               verbose=args.verbose)
        summary_rows.extend(_summary_rows(meta, report))

    # ========== Print the end-of-run summary ==========
    summary = _print_run_summary(summary_rows)
    return summary


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
    nodata_warn_fraction : float
        The advisory ``nodata_zero_*`` check grades ``warning`` when
        any panel's nodata (0 = nodata sentinel) fraction over good
        bands exceeds this.
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
    nodata_warn_fraction: float = 0.05
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
        spec: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
    """Build the per-run spectral QC report dictionary.

    Currently produces descriptive statistics per target and EM region
    (residuals of the per-band mean *and median* reflectance against the
    nominal ``Panel_ref`` value, with known-bad bands excluded) plus the
    per-panel homogeneity block (``spectral_qc.panel_homogeneity``). The
    pass/fail verdict is deliberately ``"not_evaluated"`` until the
    known-good reference set exists.

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
    spec : dict, optional
        Parsed ``spectral_limits.yml``; its per-EM-region
        ``homogeneity`` block grades the panel flags. None computes the
        homogeneity statistics without flags.

    Returns
    -------
    dict
        JSON-serialisable report (see ``QC_spectra_report.json``).
    """
    # ========== Descriptive stats per target x EM region ==========
    bad_lookup = cfg.bad_wavelengths()
    homog_lookup = (spec or {}).get("homogeneity", {})
    targets: Dict[str, Any] = {}
    for (target, region), tdf in run_df.groupby(["panel_name", "EM_Region"]):
        sensor = str(tdf["sensor"].iloc[0])
        bad = bad_lookup.get(sensor, {}).get(str(region), [])
        stats = _target_region_stats(tdf, bad,
                                     homog_thresholds=homog_lookup.get(str(region)))
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
        homog_thresholds: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
    """Descriptive residual statistics for one target x EM-region table.

    Residuals are per-band mean (and median) reflectance (percent) minus
    the nominal ``Panel_ref`` value, computed over the good bands only
    (bands whose wavelength falls outside the known-bad ranges) with the
    0 = nodata sentinel masked out. Both aggregations are always
    reported: their divergence is itself a contamination signal
    (design record: ``QC_PIPELINE_PLAN.md`` §7 Phase 3). Each panel also carries the
    ``homogeneity`` distribution-shape block from
    :func:`Code.functions.spectral_qc.panel_homogeneity` plus its
    ``nodata_zero_fraction``; a panel whose good-band samples are all
    nodata reports ``all_nodata: True`` with None statistics instead of
    NaN-propagating.

    Parameters
    ----------
    df : pd.DataFrame
        Extracted spectra rows for a single ``panel_name`` / ``EM_Region``
        combination.
    bad_ranges : list of tuple of float
        Inclusive ``(lo_nm, hi_nm)`` wavelength ranges to exclude.
    homog_thresholds : dict, optional
        Per-EM-region ``homogeneity`` block of ``spectral_limits.yml``.
        None computes the statistics without clean/suspect flags.

    Returns
    -------
    dict
        ``n_bands``, ``bad_wavelengths_excluded_nm``, ``n_bands_excluded``,
        and per-``Panel_ref`` pixel counts, nodata fraction, valid-range
        fraction, residual statistics (mean/median/std/rmse plus the
        worst band) and the ``homogeneity`` block (all None with
        ``all_nodata: True`` when nothing survives the nodata mask).
    """
    d = df.assign(refl_pct=sq.reflectance_pct(df["value"]))
    bad_mask = sq.bad_wavelength_mask(d["wavelength"], bad_ranges)
    zero_mask = sq.zero_nodata_mask(d["value"])
    scored = d[~bad_mask]  # good-band rows, nodata still included
    good = scored[~zero_mask.loc[scored.index]]
    homogeneity = sq.panel_homogeneity(good, thresholds=homog_thresholds)

    panels: Dict[str, Any] = {}
    for ref, rdf_all in scored.groupby("Panel_ref"):
        nodata_fraction = float(zero_mask.loc[rdf_all.index].mean())
        rdf = rdf_all[~zero_mask.loc[rdf_all.index]]
        valid_fraction = (float(rdf_all["Valid_Range"].mean())
                          if "Valid_Range" in rdf_all else None)
        if rdf.empty:
            # +++++ All samples are the 0 = nodata sentinel: not_evaluated +++++
            panels[str(ref)] = ({
                "n_pixels": 0,
                "nodata_zero_fraction": nodata_fraction,
                "all_nodata": True,
                "valid_range_fraction": valid_fraction,
                "mean_residual_pct": None,
                "median_residual_pct": None,
                "std_residual_pct": None,
                "rmse_residual_pct": None,
                "worst_band": None,
                "homogeneity": None,
            })
            continue
        n_bands = int(rdf["band"].nunique())
        per_band = rdf.groupby("band").agg(
            mean_refl=("refl_pct", "mean"),
            median_refl=("refl_pct", "median"),
            wavelength=("wavelength", "first"))
        resid = per_band["mean_refl"] - float(ref) # type: ignore
        resid_med = per_band["median_refl"] - float(ref) # type: ignore
        worst = resid.abs().idxmax()
        panels[str(ref)] = ({
            "n_pixels": int(round(len(rdf) / max(n_bands, 1))),
            "nodata_zero_fraction": nodata_fraction,
            "all_nodata": False,
            "valid_range_fraction": valid_fraction,
            "mean_residual_pct": float(resid.mean()),
            "median_residual_pct": float(resid_med.mean()),
            "std_residual_pct": float(resid.std()),
            "rmse_residual_pct": float(np.sqrt((resid ** 2).mean())),
            "worst_band": ({
                "band": int(worst), # type: ignore
                "wavelength_nm": float(per_band.loc[worst, "wavelength"]), # type: ignore
                "residual_pct": float(resid.loc[worst]), # type: ignore
            }),
            "homogeneity": homogeneity.get(str(int(float(ref)))), # type: ignore
        })

    return ({
        "n_bands": int(d["band"].nunique()),
        "bad_wavelengths_excluded_nm": [list(r) for r in bad_ranges],
        "n_bands_excluded": int(d.loc[bad_mask, "band"].nunique()),
        "panels": panels,
    })


# ==================================================================================
def _migrate_legacy_outputs(
        qc_dir: pathlib.Path,
        cfg: QAConfig,
        verbose: bool = False,
    ) -> None:
    """Tidy pre-contract and retired outputs in the run's QC folder (§4).

    Legacy runs hold ``QC_spectra_report.json`` at the top of
    ``QC_data/``; it is renamed into ``QC_data/QC02_SpectralCheck/`` so
    crawls never see duplicates (``QC_Spectral_Tables/`` deliberately
    stays at the top level). Retired ``*_spectra.png`` figures
    (superseded by the DHR overlay, operator decision 2026-08-26) are
    deleted wherever they sit.

    Parameters
    ----------
    qc_dir : pathlib.Path
        The run's ``T1_proc/QC_data/`` folder.
    cfg : QAConfig
        Active settings (legacy report/plots names).
    verbose : bool, optional
        Print each migration/deletion. Default False.

    Returns
    -------
    None
    """
    script_dir = qc_dir / "QC02_SpectralCheck"
    src, dst = qc_dir / cfg.report_filename, script_dir / cfg.report_filename
    if src.is_file() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        if verbose:
            tqdm.write(f"  migrated legacy {src.name} -> "
                       f"{dst.parent.relative_to(qc_dir)}/")
    for plots in (qc_dir / cfg.plots_dirname, script_dir / cfg.plots_dirname):
        for fig in sorted(plots.glob("*_spectra.png")):
            fig.unlink()
            if verbose:
                tqdm.write(f"  deleted retired figure {fig.name}")


# ==================================================================================
def _write_contract_report(
        qc_dir: pathlib.Path,
        meta: Dict[str, Any],
        report: Dict[str, Any],
        cfg: QAConfig,
        dhr: Optional[Dict[str, Any]] = None,
        verbose: bool = False,
    ) -> None:
    """Write the §2 contract report wrapping the spectral-stats payload.

    The run status stays ``not_evaluated`` until the ET00/ET03
    equivalence test and its reference set exist: the single
    ``reference_equivalence`` check grades ``not_checked`` (or
    good/fail once the pluggable test returns a verdict), the DHR
    comparison checks (§5b) are advisory, and the full
    descriptive-statistics report is embedded in the detail JSON
    under ``spectral_report``.

    Parameters
    ----------
    qc_dir : pathlib.Path
        The run's ``T1_proc/QC_data/`` folder.
    meta : dict
        Run metadata (node/project/site/sensor/date/run).
    report : dict
        Payload from :func:`build_run_report` (schema, status, targets).
    cfg : QAConfig
        Active settings (table schema version recorded in the detail).
    dhr : dict, optional
        DHR-comparison payload from :func:`build_dhr_comparison`
        (advisory checks, delta stats, artifacts, config snapshot).
    verbose : bool, optional
        Print the write. Default False.

    Returns
    -------
    None
    """
    run_meta = {k: (str(v) if v is not None else None)
                for k, v in meta.items()}
    contract = qr.new_report("QC02_SpectralCheck", __version__, run=run_meta)
    result = report.get("status", {}).get("result")
    status = {"pass": "good", "fail": "fail"}.get(result, "not_checked")
    qr.add_check(
        contract, "reference_equivalence", status,
        note=report.get("status", {}).get("reason"))
    for name, check_status, kwargs in _nodata_checks(report, cfg):
        qr.add_check(contract, name, check_status, **kwargs)
    for name, check_status, kwargs in _homogeneity_checks(report):
        qr.add_check(contract, name, check_status, **kwargs)
    contract["spectral_report"] = report
    contract["config"] = {"table_schema_version": cfg.schema_version}
    contract["artifacts"] = sorted(
        f"QC_Spectral_Tables/{p.name}"
        for p in (qc_dir / "QC_Spectral_Tables").glob("QC_*_spectra_*"))
    if dhr is not None:
        for name, check_status, kwargs in dhr["checks"]:
            qr.add_check(contract, name, check_status, **kwargs)
        contract["dhr_comparison"] = {
            "panel_set": dhr["panel_set"],
            "references": dhr["references"],
            "delta_stats": dhr["delta_stats"],
        }
        contract["config"]["spectral_limits"] = dhr["config"]
        contract["artifacts"] += dhr["artifacts"]
    summary_path, _ = qr.write_report(qc_dir, contract)
    if verbose:
        tqdm.write(f"Saved contract report: {summary_path}")


# ==================================================================================
def _nodata_checks(
        report: Dict[str, Any],
        cfg: QAConfig,
    ) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Build the advisory per-EM-region nodata (0 = nodata) contract checks.

    One check per EM region, collapsed worst-wins across the run's panel
    targets so the summary YAML stays glanceable: ``warning`` when any
    panel of any target is entirely nodata (its stats are ``all_nodata``)
    or exceeds ``cfg.nodata_warn_fraction``, ``good`` otherwise.
    Offending targets/panels are named in the check value; the per-panel
    fractions live in ``spectral_report`` in the detail JSON.

    Parameters
    ----------
    report : dict
        Payload from :func:`build_run_report`.
    cfg : QAConfig
        Active settings (``nodata_warn_fraction``).

    Returns
    -------
    list of tuple
        ``(name, status, kwargs)`` items for ``qr.add_check``. All
        advisory — the run status stays ``not_evaluated``.
    """
    per_region: Dict[str, Dict[str, Any]] = {}
    for target, regions in report.get("targets", {}).items():
        label = _target_label(str(target))
        for region, stats in regions.items():
            panels = stats.get("panels", {})
            if not panels:
                continue
            agg = per_region.setdefault(str(region), {
                "dead": [], "over": [], "worst_frac": 0.0,
                "worst_desc": None, "n_panels": 0, "n_targets": 0})
            agg["n_targets"] += 1
            agg["n_panels"] += len(panels)
            fracs = {ref: float(p.get("nodata_zero_fraction") or 0.0)
                     for ref, p in panels.items()}
            worst_ref = max(fracs, key=fracs.__getitem__)
            if fracs[worst_ref] >= agg["worst_frac"]:
                agg["worst_frac"] = fracs[worst_ref]
                agg["worst_desc"] = f"{label} panel {worst_ref}"
            dead = sorted(r for r, p in panels.items()
                          if p.get("all_nodata"))
            if dead:
                agg["dead"].append(f"{label} {', '.join(dead)}")
            elif fracs[worst_ref] > cfg.nodata_warn_fraction:
                agg["over"].append(
                    f"{label} panel {worst_ref} ({fracs[worst_ref]:.1%})")
    checks: List[Tuple[str, str, Dict[str, Any]]] = []
    for region, agg in per_region.items():
        name = f"nodata_zero_{region.lower()}"
        if agg["dead"]:
            checks.append((name, "warning", {
                "advisory": True,
                "value": "panel(s) entirely nodata: "
                         + "; ".join(agg["dead"]),
                "note": "0 = nodata sentinel; residual/homogeneity stats "
                        "for those panels are not evaluated - see "
                        "nodata_zero_fraction in spectral_report",
            }))
        elif agg["over"]:
            checks.append((name, "warning", {
                "advisory": True,
                "value": "high nodata fraction: " + "; ".join(agg["over"]),
                "threshold": f"nodata fraction <= "
                             f"{cfg.nodata_warn_fraction:.0%} per panel",
            }))
        else:
            checks.append((name, "good", {
                "advisory": True,
                "value": f"max nodata fraction {agg['worst_frac']:.1%} "
                         f"({agg['n_panels']} panel(s), "
                         f"{agg['n_targets']} target(s))",
            }))
    return checks


# ==================================================================================
def _homogeneity_checks(
        report: Dict[str, Any],
    ) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Build the advisory per-EM-region homogeneity contract checks.

    One check per EM region, collapsed worst-wins across the run's panel
    targets: ``warning`` when any panel of any target grades ``suspect``
    (suspect targets/panels are named in the check value), ``good`` when
    all evaluated panels are clean, ``not_checked`` when no panel had
    thresholds (flags all None). The per-panel homogeneity blocks live
    in ``spectral_report`` in the detail JSON.

    Parameters
    ----------
    report : dict
        Payload from :func:`build_run_report`.

    Returns
    -------
    list of tuple
        ``(name, status, kwargs)`` items for ``qr.add_check``. All
        advisory — the run status stays ``not_evaluated``.
    """
    per_region: Dict[str, Dict[str, Any]] = {}
    for target, regions in report.get("targets", {}).items():
        label = _target_label(str(target))
        for region, stats in regions.items():
            agg = per_region.setdefault(str(region), {
                "suspects": [], "n_evaluated": 0, "n_targets": 0,
                "worst_frac": 0.0, "worst_desc": None})
            blocks = {ref: p.get("homogeneity")
                      for ref, p in stats.get("panels", {}).items()}
            flags = {ref: (b or {}).get("flag") for ref, b in blocks.items()}
            evaluated = {ref: f for ref, f in flags.items() if f is not None}
            if not evaluated:
                continue
            agg["n_targets"] += 1
            agg["n_evaluated"] += len(evaluated)
            suspects = sorted(r for r, f in evaluated.items()
                              if f == "suspect")
            if not suspects:
                continue
            agg["suspects"].append(f"{label} {', '.join(suspects)}")
            for ref in suspects:
                frac = float((blocks[ref] or {})
                             .get("fraction_bands_flagged") or 0.0)
                if frac >= agg["worst_frac"]:
                    agg["worst_frac"] = frac
                    agg["worst_desc"] = f"{label} {ref}"
    checks: List[Tuple[str, str, Dict[str, Any]]] = []
    for region, agg in per_region.items():
        name = f"homogeneity_{region.lower()}"
        if agg["n_evaluated"] == 0:
            checks.append((name, "not_checked",
                           {"advisory": True,
                            "note": "no homogeneity thresholds for this "
                                    "EM region in spectral_limits.yml"}))
        elif agg["suspects"]:
            checks.append((name, "warning", {
                "advisory": True,
                "value": f"suspect panel(s): {'; '.join(agg['suspects'])} "
                         f"(worst {agg['worst_desc']}: "
                         f"{agg['worst_frac']:.0%} of bands flagged)",
                "note": "per-band distribution shape (shadow/mixed-edge/"
                        "hotspot tell); see the homogeneity blocks in "
                        "spectral_report",
            }))
        else:
            checks.append((name, "good",
                           {"advisory": True,
                            "value": f"{agg['n_evaluated']} panel(s) clean "
                                     f"({agg['n_targets']} target(s))"}))
    return checks


# ==================================================================================
def load_spectral_limits(
        spec_path: pathlib.Path,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Load the DHR-comparison limits YAML with its config snapshot (§5).

    Parameters
    ----------
    spec_path : pathlib.Path
        Path to ``spectral_limits.yml`` (repo-relative by default).

    Returns
    -------
    tuple
        ``(spec, snapshot)`` — the parsed limits plus the ``{"path",
        "sha256"}`` provenance snapshot, or ``(None, None)`` with a
        warning when the file is missing (bias checks then grade
        ``not_checked``).
    """
    if not spec_path.is_file():
        warn.warn(f"Spectral limits {spec_path} missing - DHR bias checks "
                  "will report not_checked.")
        return None, None
    loaded = qr.load_thresholds(spec_path.name, thresholds_dir=spec_path.parent)
    return loaded["spec"], {"path": loaded["path"], "sha256": loaded["sha256"]}


# ==================================================================================
def build_dhr_comparison(
        run_df: pd.DataFrame,
        meta: Dict[str, Any],
        qc_dir: pathlib.Path,
        cfg: QAConfig,
        spec: Optional[Dict[str, Any]],
        snapshot: Optional[Dict[str, Any]],
        skipplot: bool = False,
        verbose: bool = False,
    ) -> Dict[str, Any]:
    """Observed-vs-expected DHR comparison for one run (§5b, ex DT01).

    Resolves the physical panel set per target (single-ELM runs pin the
    ELM set from the gpro; dual-ELM runs resolve every ELM target by
    signature because the pin only identifies the corrected set; VAL
    excludes the pinned set), builds the per-target comparison tables
    (observed percentiles vs interpolated DHR, ``delta_pct``,
    ``bad_band``) with the 0 = nodata sentinel masked out, computes
    per-panel delta statistics, saves tables + overlay/delta figures
    into the QC02 subfolder (dual-ELM runs also get one cross-check
    figure per EM region overlaying the ELM targets' deltas per
    brightness level), and returns the advisory contract checks
    (bias findings collapse worst-wins to one ``dhr_bias_<region>``
    check per EM region; per-target detail stays in ``delta_stats``
    and ``references``).

    Parameters
    ----------
    run_df : pd.DataFrame
        All extracted spectra tables for one run, concatenated.
    meta : dict
        Run metadata (node/project/site/sensor/date/run).
    qc_dir : pathlib.Path
        The run's ``T1_proc/QC_data/`` folder.
    cfg : QAConfig
        Active settings (bad-band lookup).
    spec : dict or None
        Parsed ``spectral_limits.yml`` (None grades bias checks
        ``not_checked``).
    snapshot : dict or None
        Threshold-spec provenance for the config snapshot.
    skipplot : bool, optional
        Skip the overlay/delta figures. Default False.
    verbose : bool, optional
        Print per-target diagnostics. Default False.

    Returns
    -------
    dict
        ``{"checks", "delta_stats", "references", "panel_set",
        "artifacts", "config"}`` for :func:`_write_contract_report`.
        All checks are advisory — the run status stays
        ``not_evaluated``.
    """
    script_dir = qc_dir / "QC02_SpectralCheck"
    out: Dict[str, Any] = {"checks": [], "delta_stats": [], "references": {},
                           "artifacts": [], "config": snapshot}

    # ========== gpro set pin + panel_set_pinned check (§5b rules 1/3) ==========
    set_key, prefixes = sq.gpro_panel_set(qc_dir.parent)
    n_elm_targets = int(run_df.loc[
        run_df["target_type"].astype(str).str.upper() == "ELM",
        "panel_name"].nunique())
    dual_elm = n_elm_targets > 1
    out["panel_set"] = {"gpro_pin": set_key, "prefixes": prefixes,
                        "n_elm_targets": n_elm_targets}
    if len(prefixes) > 1:
        out["checks"].append((
            "panel_set_pinned", "warning",
            {"advisory": True, "value": ", ".join(prefixes),
             "note": "gpro pipelines reference multiple panel sets - "
                     "cross-over between sets flown concurrently"}))
    elif set_key is None:
        out["checks"].append((
            "panel_set_pinned", "not_checked",
            {"advisory": True,
             "note": "no panel target references in gpro pipelines"}))
    else:
        try:
            sq.resolve_panel_set(meta["node"], set(), set_key=set_key)
            out["checks"].append((
                "panel_set_pinned", "good",
                {"advisory": True, "value": set_key,
                 "note": (f"dual-ELM run ({n_elm_targets} ELM targets): the "
                          "pin identifies the gpro-corrected set only; ELM "
                          "targets resolve by signature")
                 if dual_elm else None}))
        except FileNotFoundError as err:
            out["checks"].append((
                "panel_set_pinned", "warning",
                {"advisory": True, "value": set_key, "note": str(err)}))

    # ========== Per target x EM region comparison ==========
    # Bias findings accumulate per EM region and collapse to one advisory
    # check per region after the loop (summary stays glanceable).
    bias_regions: Dict[str, Dict[str, Any]] = {}
    # dual-ELM cross-check: ELM comps stashed per region for the figure
    elm_comps: Dict[str, Dict[str, Any]] = {}
    for (target, ttype, region), tdf in run_df.groupby(
            ["panel_name", "target_type", "EM_Region"]):
        label = _target_label(str(target))
        agg = bias_regions.setdefault(str(region),
                                      {"skipped": [], "graded": []})
        is_elm = str(ttype).upper() == "ELM"
        signature = {str(int(float(r))) for r in tdf["Panel_ref"].unique()}
        try:
            # Single-ELM runs resolve ELM by the gpro pin. Dual-ELM runs
            # resolve every ELM target by signature: the pin only says
            # which set the gpro corrected with, not which vector file
            # is which (identical 2024-batch curves resolve via
            # identical_candidates; differing curves stay a hard error).
            # VAL excludes the pinned ELM set (each node fields two
            # 4-panel sets, so elimination identifies the other one).
            set_dir, resolution = sq.resolve_panel_set(
                meta["node"], signature,
                set_key=set_key if (is_elm and not dual_elm) else None,
                exclude_key=None if is_elm else set_key)
            comp, refs = _compare_target_region(
                tdf, set_dir, str(tdf["sensor"].iloc[0]), str(region), cfg)
        except (FileNotFoundError, LookupError) as err:
            agg["skipped"].append((label, str(err)))
            if verbose:
                tqdm.write(f"  {target}/{region}: DHR skipped - {err}")
            continue
        out["references"][f"{target}|{region}"] = {
            "resolution": resolution, "panels": refs}
        if is_elm:
            rec = elm_comps.setdefault(str(region), {
                "sensor": str(tdf["sensor"].iloc[0]), "targets": {}})
            rec["targets"][label] = comp

        stats = _dhr_delta_stats(comp)
        stats.insert(0, "panel_name", str(target))
        stats.insert(1, "EM_Region", str(region))
        out["delta_stats"].extend(stats.to_dict(orient="records"))

        # +++++ Tables (P7 parquet) +++++
        script_dir.mkdir(parents=True, exist_ok=True)
        stem = cf.safe_filename_component(f"{target}_{region}")
        comp_path = script_dir / f"DHR_{stem}_comparison.parquet"
        stats_path = script_dir / f"DHR_{stem}_delta_stats.parquet"
        comp.to_parquet(comp_path, index=False)
        stats.to_parquet(stats_path, index=False)
        out["artifacts"] += [f"QC02_SpectralCheck/{comp_path.name}",
                             f"QC02_SpectralCheck/{stats_path.name}"]

        # +++++ Figures +++++
        if not skipplot:
            figs = _plot_dhr_figures(comp, str(target), str(region),
                                     script_dir / cfg.plots_dirname,
                                     sensor=str(tdf["sensor"].iloc[0]))
            out["artifacts"] += [
                f"QC02_SpectralCheck/{cfg.plots_dirname}/{f.name}"
                for f in figs]

        # +++++ Advisory bias grading (full region, bad bands masked) +++++
        full = stats[stats["region"] == "full"]
        if full.empty or spec is None:
            agg["skipped"].append((label, "no limits spec" if spec is None
                                   else "no usable bands"))
            continue
        worst_idx = full["bias_pct"].abs().idxmax()
        worst = full.loc[worst_idx]
        limit = (spec.get("bias_abs_pct", {}).get(str(region), {})
                 .get("warn_above"))
        status = "not_checked" if limit is None else (
            "warning" if abs(float(worst["bias_pct"])) > float(limit)
            else "good")
        method_note = (f"set resolved by {resolution['method']}"
                       + (f" ({', '.join(resolution['candidates'])})"
                          if len(resolution["candidates"]) > 1 else ""))
        agg["limit"] = limit
        agg["graded"].append({
            "label": label, "status": status,
            "bias": float(worst["bias_pct"]),
            "panel": str(worst["Panel_ref"]),
            "serial": str(worst["serial"]),
            "method": method_note,
        })

    # ========== Dual-ELM cross-check figure (per EM region) ==========
    if not skipplot:
        for region, rec in elm_comps.items():
            if len(rec["targets"]) < 2:
                continue
            fig_path = _plot_dual_elm_delta(
                rec["targets"], str(region),
                script_dir / cfg.plots_dirname, sensor=rec["sensor"])
            out["artifacts"].append(
                f"QC02_SpectralCheck/{cfg.plots_dirname}/{fig_path.name}")

    # ========== Collapse to one advisory bias check per EM region ==========
    for region, agg in bias_regions.items():
        name = f"dhr_bias_{region.lower()}"
        skipped_note = "; ".join(
            f"{lbl}: {reason}" for lbl, reason in agg["skipped"]) or None
        if not agg["graded"]:
            out["checks"].append((name, "not_checked",
                                  {"advisory": True, "note": skipped_note}))
            continue
        graded = agg["graded"]
        status = ("warning" if any(g["status"] == "warning" for g in graded)
                  else "good" if any(g["status"] == "good" for g in graded)
                  else "not_checked")
        worst_g = max(graded, key=lambda g: abs(g["bias"]))
        limit = agg.get("limit")
        methods = list(dict.fromkeys(g["method"] for g in graded))
        note_parts = (methods if len(methods) == 1 else
                      [f"{g['label']}: {g['method']}" for g in graded])
        if skipped_note:
            note_parts.append(f"skipped {skipped_note}")
        out["checks"].append((name, status, {
            "advisory": True,
            "value": f"worst |bias| {abs(worst_g['bias']):.2f} % "
                     f"({worst_g['label']} panel {worst_g['panel']}, "
                     f"{worst_g['serial']})",
            "threshold": (f"|bias| <= {limit} % per panel, bad bands masked"
                          if limit is not None else None),
            "note": "; ".join(note_parts),
        }))
    return out


# ==================================================================================
def _target_label(target: str) -> str:
    """Compact label naming a panel target inside a collapsed check line.

    Parameters
    ----------
    target : str
        Panel-file label (``QC_VAL_Gryfn4P_Panels``).

    Returns
    -------
    str
        e.g. ``val_gryfn4p``.
    """
    core = re.sub(r"^QC_", "", target)
    core = re.sub(r"_?Panels?", "", core)
    return cf.safe_filename_component(core).lower()


# ==================================================================================
def _compare_target_region(
        tdf: pd.DataFrame,
        set_dir: pathlib.Path,
        sensor: str,
        region: str,
        cfg: QAConfig,
    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Build one target x region observed-vs-expected comparison table.

    Parameters
    ----------
    tdf : pd.DataFrame
        Extracted spectra rows for one ``panel_name`` / ``EM_Region``.
    set_dir : pathlib.Path
        Resolved panel-set folder.
    sensor : str
        Sensor platform (bad-band lookup key).
    region : str
        EM region (bad-band lookup key).
    cfg : QAConfig
        Active settings.

    Returns
    -------
    tuple
        ``(comparison, references)`` — the DT01-schema table
        (``Panel_ref, wavelength, obs_med_pct, obs_p5_pct, obs_p95_pct,
        n_px, exp_pct, delta_pct, serial, bad_band, EM_Region``) and the
        per-panel DHR provenance mapping. The 0 = nodata sentinel is
        masked before the percentiles; panels with no real samples drop
        out of the table (their nodata fraction is reported by the
        ``nodata_zero_*`` checks).

    Raises
    ------
    LookupError
        When every sample in *tdf* is nodata (nothing to compare).
    """
    real = tdf[~sq.zero_nodata_mask(tdf["value"])]
    if real.empty:
        raise LookupError(
            "all extracted samples are the 0 = nodata sentinel for this "
            "target/region - nothing to compare against the DHR")
    grouped = (real.assign(refl_pct=sq.reflectance_pct(real["value"]))
               .groupby(["Panel_ref", "wavelength"])["refl_pct"]
               .agg(obs_med_pct="median",
                    obs_p5_pct=lambda v: float(np.percentile(v, 5)),
                    obs_p95_pct=lambda v: float(np.percentile(v, 95)),
                    n_px="count")
               .reset_index())
    frames: List[pd.DataFrame] = []
    references: Dict[str, Any] = {}
    for ref, gdf in grouped.groupby("Panel_ref"):
        code = str(int(float(ref)))  # type: ignore[arg-type]
        dhr, prov = sq.load_panel_dhr(set_dir, code)
        gdf = gdf.copy()
        gdf["exp_pct"] = np.interp(
            gdf["wavelength"], dhr["wavelength_nm"],
            dhr["reflectance"] * 100.0)
        gdf["serial"] = prov["serial"]
        references[code] = prov
        frames.append(gdf)
    comp = pd.concat(frames, ignore_index=True)
    comp["delta_pct"] = comp["obs_med_pct"] - comp["exp_pct"]
    bad = cfg.bad_wavelengths().get(sensor, {}).get(region, [])
    comp["bad_band"] = sq.bad_wavelength_mask(comp["wavelength"], bad)
    comp["EM_Region"] = region
    return comp, references


# ==================================================================================
def _dhr_delta_stats(comp: pd.DataFrame) -> pd.DataFrame:
    """Per-panel delta statistics over named spectral regions.

    Bad bands are excluded before the roll-up (DT01 convention).

    Parameters
    ----------
    comp : pd.DataFrame
        Comparison table from :func:`_compare_target_region`.

    Returns
    -------
    pd.DataFrame
        Columns: ``Panel_ref, serial, region, n_bands, bias_pct,
        rmse_pct, mae_pct, max_abs_pct``.
    """
    regions = {"blue": (400.0, 500.0), "green": (500.0, 600.0),
               "red": (600.0, 690.0), "red_edge": (690.0, 750.0),
               "nir": (750.0, 1000.0), "full": (400.0, 2500.0)}
    rows: List[Dict[str, Any]] = []
    good = comp[~comp["bad_band"]]
    for ref, gdf in good.groupby("Panel_ref"):
        for name, (lo, hi) in regions.items():
            sel = gdf[(gdf["wavelength"] >= lo) & (gdf["wavelength"] <= hi)]
            if sel.empty:
                continue
            d = sel["delta_pct"].to_numpy(dtype=float)
            rows.append({
                "Panel_ref": str(ref), "serial": str(sel["serial"].iloc[0]),
                "region": name, "n_bands": int(len(sel)),
                "bias_pct": float(np.mean(d)),
                "rmse_pct": float(np.sqrt(np.mean(d ** 2))),
                "mae_pct": float(np.mean(np.abs(d))),
                "max_abs_pct": float(np.max(np.abs(d))),
            })
    return pd.DataFrame(rows)


# ==================================================================================
def _plot_dhr_figures(
        comp: pd.DataFrame,
        target: str,
        region: str,
        plots_dir: pathlib.Path,
        sensor: str = "",
    ) -> List[pathlib.Path]:
    """Save the overlay and delta figures for one target x region.

    Styled to match the QA02 comparison figures: bold titles/labels,
    dashed major gridlines, frameless legends, tight bounding box, and
    a symlog y-axis on the delta figure so small systematic offsets
    stay readable next to artefact spikes. Bad bands render as gaps
    (matplotlib lines over NaN-masked values, DT01 convention).

    Parameters
    ----------
    comp : pd.DataFrame
        Comparison table from :func:`_compare_target_region`.
    target : str
        Panel-file label (figure title + filename stem).
    region : str
        EM region.
    plots_dir : pathlib.Path
        Figure folder (created if missing).
    sensor : str, optional
        Sensor label for the QA02-style suptitle. Default "".

    Returns
    -------
    list of pathlib.Path
        The two saved figure paths.
    """
    plots_dir.mkdir(parents=True, exist_ok=True)
    sns.set_style("whitegrid")
    plt.rcParams.update({
        "font.size": 11,
        "axes.titleweight": "bold",
        "axes.labelweight": "bold",
        "figure.titleweight": "bold",
    })
    stem = cf.safe_filename_component(f"{target}_{region}")
    title_meta = ((f"Sensor: {sensor}, " if sensor else "")
                  + f"Target: {target}, EM range: {region}")
    masked = comp.copy()
    masked.loc[masked["bad_band"],
               ["obs_med_pct", "obs_p5_pct", "obs_p95_pct", "delta_pct"]] = np.nan
    panels = sorted(masked["Panel_ref"].unique(), key=float)
    # QA02 tiered palette (CARTO Bold <=10) keyed on panel refs
    palette = dict(zip([str(p) for p in panels],
                       cf.resolve_run_palette([str(p) for p in panels]).values()))

    # +++++ Overlay: observed vs expected, faceted per panel (2 columns) +++++
    ncols = min(2, len(panels))
    nrows = int(np.ceil(len(panels) / ncols))
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(5.0 * ncols, 3.6 * nrows),
                             sharex=True, sharey=True, squeeze=False)
    for ax, ref in zip(axes.flat, panels):
        sub = masked[masked["Panel_ref"] == ref].sort_values("wavelength")
        colour = palette[str(ref)]
        ax.fill_between(sub["wavelength"], sub["obs_p5_pct"],
                        sub["obs_p95_pct"], color=colour, alpha=0.3,
                        label="obs p5-p95")
        ax.plot(sub["wavelength"], sub["obs_med_pct"], color=colour, lw=1.5,
                label="obs median")
        ax.plot(sub["wavelength"], sub["exp_pct"], "k--", lw=1.2,
                label=f"DHR {sub['serial'].iloc[0]}")
        ax.set_title(f"Panel_ref = {ref}")
        ax.grid(True, which="major", linestyle="--", linewidth=0.5,
                color="0.4")
        ax.grid(False, which="minor")
    for ax in axes.flat[len(panels):]:
        ax.set_visible(False)
    for ax in axes[-1]:
        ax.set_xlabel("Wavelength (nm)")
    for row in axes:
        row[0].set_ylabel("Reflectance (%)")
    axes[0][0].legend(fontsize=8, frameon=False)
    fig.suptitle(f"{title_meta} — observed vs manufacturer DHR", y=0.98)
    fig.tight_layout()
    overlay = plots_dir / f"{stem}_dhr_overlay.png"
    fig.savefig(overlay, dpi=150, bbox_inches="tight")
    plt.close(fig)

    # +++++ Delta: observed - expected with percentile envelope (symlog) +++++
    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    for ref in panels:
        sub = masked[masked["Panel_ref"] == ref].sort_values("wavelength")
        colour = palette[str(ref)]
        ax.plot(sub["wavelength"], sub["delta_pct"], lw=1.2, color=colour,
                label=f"panel {ref}")
        ax.fill_between(sub["wavelength"],
                        sub["obs_p5_pct"] - sub["exp_pct"],
                        sub["obs_p95_pct"] - sub["exp_pct"],
                        color=colour, alpha=0.25, linewidth=0)
    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel("Observed - expected (pp reflectance)")
    ax.set_title(f"{title_meta} — delta vs DHR (bad bands masked)")
    # Symlog keeps small systematic offsets readable next to spikes (QA02)
    ax.set_yscale("symlog")
    ax.yaxis.set_major_formatter(mticker.ScalarFormatter())
    ax.yaxis.get_major_formatter().set_scientific(False)
    ax.axhline(0, color="0.4", lw=0.6)
    ax.minorticks_on()
    ax.yaxis.set_minor_locator(mticker.SymmetricalLogLocator(
        ax.yaxis.get_transform(), subs=np.arange(2, 10)))
    ax.grid(True, which="both", linestyle="--", linewidth=0.5, color="0.4")
    ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    delta = plots_dir / f"{stem}_dhr_delta.png"
    fig.savefig(delta, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return [overlay, delta]


# ==================================================================================
def _plot_dual_elm_delta(
        comps: Dict[str, pd.DataFrame],
        region: str,
        plots_dir: pathlib.Path,
        sensor: str = "",
    ) -> pathlib.Path:
    """Dual-ELM cross-check: per-brightness-level DHR deltas overlaid.

    One subfigure per panel brightness level (``Panel_ref``), each
    overlaying the ``delta_pct`` line (+ p5-p95 envelope) of every ELM
    target in the run, so the two ELM panel captures can be compared
    level-by-level. Styling matches :func:`_plot_dhr_figures` (symlog
    y-axis, bad bands rendered as gaps).

    Parameters
    ----------
    comps : dict
        ``{target_label: comparison table}`` from
        :func:`_compare_target_region`, one entry per ELM target.
    region : str
        EM region (title + filename).
    plots_dir : pathlib.Path
        Figure folder (created if missing).
    sensor : str, optional
        Sensor label for the suptitle. Default "".

    Returns
    -------
    pathlib.Path
        The saved figure path.
    """
    plots_dir.mkdir(parents=True, exist_ok=True)
    sns.set_style("whitegrid")
    plt.rcParams.update({
        "font.size": 11,
        "axes.titleweight": "bold",
        "axes.labelweight": "bold",
        "figure.titleweight": "bold",
    })
    labels = sorted(comps)
    palette = dict(zip(labels, cf.resolve_run_palette(labels).values()))
    refs = sorted({float(r) for c in comps.values()
                   for r in c["Panel_ref"].unique()})
    ncols = min(2, len(refs))
    nrows = int(np.ceil(len(refs) / ncols))
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(5.0 * ncols, 3.6 * nrows),
                             sharex=True, sharey=True, squeeze=False)
    for ax, ref in zip(axes.flat, refs):
        for label in labels:
            comp = comps[label]
            sub = (comp[comp["Panel_ref"].astype(float) == ref]
                   .sort_values("wavelength").copy())
            if sub.empty:
                continue
            sub.loc[sub["bad_band"],
                    ["obs_med_pct", "obs_p5_pct", "obs_p95_pct",
                     "delta_pct"]] = np.nan
            colour = palette[label]
            ax.plot(sub["wavelength"], sub["delta_pct"], lw=1.2,
                    color=colour,
                    label=f"{label} ({sub['serial'].iloc[0]})")
            ax.fill_between(sub["wavelength"],
                            sub["obs_p5_pct"] - sub["exp_pct"],
                            sub["obs_p95_pct"] - sub["exp_pct"],
                            color=colour, alpha=0.25, linewidth=0)
        ax.set_title(f"Panel_ref = {ref:g}")
        ax.axhline(0, color="0.4", lw=0.6)
        ax.set_yscale("symlog")
        ax.yaxis.set_major_formatter(mticker.ScalarFormatter())
        ax.yaxis.get_major_formatter().set_scientific(False)
        ax.grid(True, which="both", linestyle="--", linewidth=0.5,
                color="0.4")
    for ax in axes.flat[len(refs):]:
        ax.set_visible(False)
    for ax in axes[-1]:
        ax.set_xlabel("Wavelength (nm)")
    fig.supylabel("Observed - expected (pp reflectance)",
                  fontweight="bold", fontsize=11)
    axes[0][0].legend(fontsize=8, frameon=False)
    fig.suptitle((f"Sensor: {sensor}, " if sensor else "")
                 + f"EM range: {region} — dual-ELM delta vs DHR "
                   "(bad bands masked)", y=0.98)
    fig.tight_layout()
    fpath = plots_dir / (
        f"{cf.safe_filename_component(f'DualELM_{region}')}_dhr_delta.png")
    fig.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return fpath


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
                     for r in regions.values() for p in r["panels"].values()
                     if p["mean_residual_pct"] is not None]
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
    reported rows (QC00 pattern). Once the equivalence test lands, the
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
    # This is a check for reflectance vs radiance. The 0 = nodata sentinel
    # is excluded first; an entirely-nodata table passes through so the
    # nodata reporting can see it (it carries no range information).
    real = df.loc[~sq.zero_nodata_mask(df["value"]), "value"]
    if panel["sensor"] in cfg.valid_sensors and not no_radiance_check and len(real):
        if pd.api.types.is_integer_dtype(df["value"]):
            if real.max() < cfg.radiance_int_max:
                warn.warn(f"Maximum value in DataFrame for raster {ras['InputRaster']} is less than {cfg.radiance_int_max}. This may indicate that the values are in reflectance rather than radiance, which is unexpected for this sensor. Please check the processing step and ensure that the correct values are being extracted. Skipping file.")
                valid = False
        elif pd.api.types.is_float_dtype(df["value"]):
            if real.max() > cfg.radiance_float_max:
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
        if not parsed["valid"]:
            warn.warn(
                f"Skipping {panel}: invalid APPN folder structure - "
                + " ".join(parsed["errors"]))
            continue
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
    parser.add_argument("--spec", type=str, default="reference/thresholds/spectral_limits.yml", help="DHR-comparison limits YAML relative to the repo root (advisory dhr_bias checks report not_checked if missing).")

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
    # Anchor a relative --spec to the repo root (the chdir below moves
    # the cwd into the crawl target, not the repo).
    if not pathlib.Path(args.spec).is_absolute():
        args.spec = str(pathlib.Path(_git_root) / args.spec)
    os.chdir(path)

    # ========== Parse Args to main function ==========
    main(args, path)