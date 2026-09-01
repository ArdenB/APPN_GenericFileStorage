"""Multi-run spectral QC comparison (QA02).

Gathers the per-run panel spectra tables produced by
``QC02_SpectralCheck.py`` (``QC_Spectral_Tables/QC_*_spectra_*``)
across every run under the given path, optionally appends tables received
from other nodes (``--load-dir``), aligns wavelengths onto a shared
reference grid, and produces three cross-run figures per
(sensor group x target x EM region), faceted by panel with one line per
run:

- ``*_refl.png``      observed reflectance + dashed expected-DHR curves
- ``*_accuracy.png``  observed - expected DHR (pp, symlog)
- ``*_precision.png`` residual vs the cross-run mean (% refl, symlog)

Platforms that share an EM region (e.g. the CALVIS/GOBI shared Headwall
VNIR) are pooled into one figure set by default, with the platform shown
via line style and the run label; ``--split-platforms`` restores
per-platform figures. The expected DHR comes from the per-run
observed-vs-expected artifacts QC02 writes
(``QC_data/QC02_SpectralCheck/DHR_*_comparison / _delta_stats``
parquets, plan §5b), which are also aggregated into combined
``all_runs`` tables and the advisory within-day panel-bias drift check
(bias walk across a day's runs, correlated against run order and QC01's
solar geometry).

Where results are saved depends on the level of the path provided:

- node folder    -> ``<Node>/Documents/QAReports/``
- project folder -> ``<Project>/Documentation/QAReports/``
- anything else  -> ``--output-dir`` is required.

``--no-save`` displays the figures interactively instead of saving them.

Pairwise distribution statistics (Wasserstein-1 distances, seasonal
drift) are planned but not yet implemented; they depend on the
equivalence test under development in APEx_SensorCalibration (ET00/ET03).

Command-line Arguments
----------------------
--path : str, optional
    Node/project folder to crawl for extracted spectra tables. Defaults
    to the root directory of the git repository.
--output-dir : str, optional
    Where to save figures. Required when --path is not a node or
    project folder (unless --no-save).
--no-save : flag
    Show figures interactively instead of saving them.
--load-dir : str, optional
    Also load spectra tables from this folder (searched recursively,
    e.g. a container produced by --save-dir on another node).
--save-dir : str, optional
    Build a portable spectral-accuracy container in this directory:
    every gathered spectra table (``tables/``), the per-run QC02 QC
    reports (``reports/``) and figures (``figures/``), plus the
    comparison figures produced by this script
    (``comparison_figures/``).
--start-date : str, optional
    Only include runs on or after this date (e.g. 2026-08-01 or 20260801).
--end-date : str, optional
    Only include runs on or before this date.
--errorbar : {pi, sd, none}
    Spread band around each run line. Default pi.
--split-platforms : flag
    Keep sensor platforms in separate figures instead of pooling
    platforms that share an EM region.
--include-runs : {untriaged, degraded, failed}, optional
    Cumulative severity ladder for runs flagged in ``RunOverview.csv``.
    Default: clean runs only. ``untriaged`` also includes Issues runs
    with open TODO/wip tickets; ``degraded`` adds confirmed
    caution/failed tickets; ``failed`` adds RunFailed runs.
--include-duplicates : flag
    Include runs flagged ``DuplicateRun`` (orthogonal to
    --include-runs).
--spec : str, optional
    Spectral-limits YAML (drift thresholds) relative to the repo root.
"""

# ==============================================================================

__title__ = "Spectral run comparison"
__author__ = "Arden Burrell"
__version__ = "v1.7(26.08.2026)"
__email__ = "arden.burrell@sydney.edu.au"

# ==============================================================================
# ========== Import core packages ==========
import os
import sys
import json
import shutil
import argparse
import pathlib
from typing import Dict, List, Any, Tuple, Optional

# ========== Import other packages ==========
import git
from git import exc as git_exc
import numpy as np
import pandas as pd
from tqdm import tqdm
import warnings as warn
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import seaborn as sns

# ========== Resolve git root (must happen before importing Code.functions.*) ==========
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
import Code.functions.issue_yaml as iy
import Code.functions.spectral_qc as sq
import Code.functions.qc_report as qr


# ==================================================================================
def main(
        args: argparse.Namespace,
        path: pathlib.Path,
    ) -> None:
    """Run the multi-run spectral comparison pipeline.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments.
    path : pathlib.Path
        Node/project folder to crawl for extracted spectra tables.

    Returns
    -------
    None
    """
    # ========== Resolve where the outputs go ==========
    qa_root = cf.resolve_qareports_dir(path, args.output_dir, args.no_save)
    scope = cf.scope_label(path)
    out_dir = None
    if qa_root is not None:
        out_dir = qa_root / f"QA02_SpectralComparison_{scope}"
        _clean_stale_outputs(qa_root, out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

    # ========== Gather the extracted spectra tables ==========
    tables = gather_spectra_tables(
        path, file_type=args.type, exclude_dirs=args.exclude_dir,
        include_runs=args.include_runs,
        include_duplicates=args.include_duplicates,
        verbose=args.verbose)
    if args.load_dir is not None:
        tables.extend(load_external_spectra(pathlib.Path(args.load_dir), args.type))
    tables = filter_tables_by_date(tables, args.start_date, args.end_date)
    if len(tables) == 0:
        raise ValueError(
            f"No usable spectra tables found under {path}"
            + (f" or {args.load_dir}" if args.load_dir else "")
            + (" within the requested date window"
               if (args.start_date or args.end_date) else "")
            + ". Run QC02_SpectralCheck.py first to extract them, or widen "
            "--include-runs / --include-duplicates if runs were excluded "
            "above.")

    # ========== Save copies to --save-dir if provided ==========
    save_dir = pathlib.Path(args.save_dir) if args.save_dir is not None else None
    if save_dir is not None:
        save_spectra_copies(tables, save_dir, args.type)

    # ========== DHR aggregation: tables + drift check (ex DT01, plan §7) ==========
    dhr = aggregate_dhr_comparisons(
        path, out_dir, spec_path=pathlib.Path(args.spec),
        exclude_dirs=args.exclude_dir,
        include_runs=args.include_runs,
        include_duplicates=args.include_duplicates,
        verbose=args.verbose)

    # ========== Combine, align wavelengths, and label the runs ==========
    df = prepare_comparison_frame(
        tables, split_platforms=args.split_platforms, verbose=args.verbose)
    print(f"Prepared {len(df):,} rows across {df['run_label'].nunique()} run(s), "
          f"{df['sensor'].nunique()} sensor(s).")

    # ========== Join each run's expected DHR onto the pixel frame ==========
    df, serial_check = join_expected_dhr(df, dhr["comp"])
    dhr["checks"].append(serial_check)

    # ========== Cross-run comparison figures ==========
    plot_comparison_spectra(
        df, plot_dir=out_dir, show=args.no_save,
        errorbar=args.errorbar,
        bad_wavelengths=sq.default_bad_wavelengths(),
        copy_dir=(save_dir / "comparison_figures") if save_dir else None,
        verbose=args.verbose)

    # ========== Cross-run statistics (future) ==========
    cross_run_stats(df)

    # ========== Contract report (§2, scope-labelled) ==========
    if qa_root is not None and out_dir is not None:
        report = qr.new_report("QA02_SpectralComparison", __version__, run={
            "scope_path": str(path),
            "n_runs": int(df["run_label"].nunique()),
            "n_sensors": int(df["sensor"].nunique()),
            "n_dhr_runs": dhr["n_runs"],
        })
        report["scope"] = scope
        qr.add_check(
            report, "cross_run_stats", "not_checked",
            note=("pairwise Wasserstein-1 / seasonal-drift statistics "
                  "pending the ET00/ET03 equivalence test "
                  "(APEx_SensorCalibration)"))
        for name, check_status, kwargs in dhr["checks"]:
            qr.add_check(report, name, check_status, **kwargs)
        if dhr["config"] is not None:
            report["config"] = {"spectral_limits": dhr["config"]}
        report["artifacts"] = sorted(
            f"{out_dir.name}/{p.name}"
            for p in out_dir.iterdir() if p.is_file())
        qr.write_report(qa_root, report)

    if out_dir is not None:
        print(f"\nAll comparison figures saved to: {out_dir}")
    else:
        print("\n*** NOTHING WAS SAVED (--no-save): figures were displayed only. ***")


# ==================================================================================
def _clean_stale_outputs(
        qa_root: pathlib.Path,
        out_dir: pathlib.Path,
    ) -> None:
    """Delete figures from naming schemes this version no longer writes.

    Covers the pre-contract flat figures in the routed reports folder,
    the ``*_refl_percent``/``*_residual_percent`` pair, and the retired
    ``QA02_DHR_*`` aggregate family (superseded by the
    ``*_refl``/``*_accuracy``/``*_precision`` set).

    Parameters
    ----------
    qa_root : pathlib.Path
        The routed ``QAReports/`` folder.
    out_dir : pathlib.Path
        The scoped subfolder the figures live in.

    Returns
    -------
    None
    """
    stale_patterns = ["*_refl_percent.png", "*_residual_percent.png",
                      "QA02_DHR_*_delta_by_run.png",
                      "QA02_DHR_*_obs_vs_expected.png"]
    stale: List[pathlib.Path] = []
    for folder in (qa_root, out_dir):
        if folder.is_dir():
            for pat in stale_patterns:
                stale.extend(folder.glob(pat))
    for fpath in stale:
        fpath.unlink()
    if stale:
        print(f"  removed {len(stale)} stale figure(s) from earlier "
              "QA02 versions")


# ==================================================================================
def gather_spectra_tables(
        path: pathlib.Path,
        file_type: str = "parquet",
        exclude_dirs: Optional[List[str]] = None,
        include_runs: Optional[str] = None,
        include_duplicates: bool = False,
        verbose: bool = False,
    ) -> List[pd.DataFrame]:
    """Find and load every extracted spectra table under *path*.

    Searches for ``QC_*_spectra_*.<type>`` files inside
    ``QC_Spectral_Tables`` folders (the QC02 output convention) and
    loads the ones that pass schema validation. Runs flagged in their
    date folder's ``RunOverview.csv`` are excluded unless opted in
    (see :func:`Code.functions.issue_yaml.run_exclusion`).

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively.
    file_type : str, optional
        ``"parquet"`` or ``"csv"``. Default ``"parquet"``.
    exclude_dirs : list of str, optional
        Directory names to exclude from the search.
    include_runs : str or None, optional
        ``--include-runs`` severity ladder level (None = clean only).
    include_duplicates : bool, optional
        Include runs flagged ``DuplicateRun``. Default False.
    verbose : bool, optional
        Print per-file diagnostics. Default False.

    Returns
    -------
    list of pd.DataFrame
        Validated spectra tables.
    """
    print(f"Scanning {path} for extracted spectra tables. {pd.Timestamp.now()}")
    files = sorted(f for f in path.rglob(f"QC_*_spectra_*.{file_type}")
                   if f.parent.name == "QC_Spectral_Tables")
    if exclude_dirs:
        exclude_set = set(exclude_dirs)
        files = [f for f in files
                 if not (set(p.name for p in f.parents) & exclude_set)]
    files = _exclude_flagged_runs(files, include_runs, include_duplicates)
    print(f"Found {len(files)} spectra table(s).")

    tables: List[pd.DataFrame] = []
    for fpath in tqdm(files, desc="Loading spectra tables"):
        df = _load_and_validate_table(fpath, file_type, verbose=verbose)
        if df is not None:
            tables.append(df)
    return tables


# ==================================================================================
def _exclude_flagged_runs(
        files: List[pathlib.Path],
        include_runs: Optional[str],
        include_duplicates: bool,
    ) -> List[pathlib.Path]:
    """Drop artifacts whose run is excluded by its RunOverview.csv flags.

    Both QC02 artifact families sit at
    ``<date>/<run>/T1_proc/QC_data/<subdir>/<file>``, so the run folder
    is always ``parents[3]``. One line per excluded run is printed with
    the flag that re-includes it.

    Parameters
    ----------
    files : list of pathlib.Path
        Candidate artifact paths.
    include_runs : str or None
        ``--include-runs`` severity ladder level (None = clean only).
    include_duplicates : bool
        Include runs flagged ``DuplicateRun``.

    Returns
    -------
    list of pathlib.Path
        The paths whose runs are included.
    """
    kept: List[pathlib.Path] = []
    excluded: Dict[pathlib.Path, str] = {}
    for fpath in files:
        run_dir = fpath.parents[3]
        if run_dir in excluded:
            continue
        reason = iy.run_exclusion(run_dir.parent, run_dir.name,
                                  include_runs=include_runs,
                                  include_duplicates=include_duplicates)
        if reason is None:
            kept.append(fpath)
        else:
            excluded[run_dir] = reason
    for run_dir, reason in sorted(excluded.items()):
        print(f"  EXCLUDED {run_dir}: {reason}")
    return kept


# ==================================================================================
def filter_tables_by_date(
        tables: List[pd.DataFrame],
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> List[pd.DataFrame]:
    """Keep only the tables whose run date falls inside the window.

    Each table holds a single run, so its first ``date`` value is
    compared against the (inclusive) ``start_date``/``end_date`` bounds.
    Tables whose date cannot be parsed are kept with a warning.

    Parameters
    ----------
    tables : list of pd.DataFrame
        Validated spectra tables.
    start_date, end_date : str or None
        Inclusive bounds in any ``pd.to_datetime``-parseable form
        (e.g. ``2026-08-01`` or ``20260801``). None = unbounded.

    Returns
    -------
    list of pd.DataFrame
        The tables within the window.

    Raises
    ------
    ValueError
        If a bound cannot be parsed or start_date > end_date.
    """
    if start_date is None and end_date is None:
        return tables
    bounds = {}
    for name, val in [("start-date", start_date), ("end-date", end_date)]:
        if val is not None:
            bounds[name] = pd.to_datetime(val, errors="coerce")
            if pd.isna(bounds[name]):
                raise ValueError(f"Could not parse --{name} '{val}'.")
    start = bounds.get("start-date")
    end = bounds.get("end-date")
    if start is not None and end is not None and start > end:
        raise ValueError(f"--start-date {start.date()} is after --end-date {end.date()}.")

    kept: List[pd.DataFrame] = []
    for t in tables:
        run_date = pd.to_datetime(str(t["date"].iloc[0]), errors="coerce")
        if pd.isna(run_date):
            warn.warn(
                f"Could not parse run date '{t['date'].iloc[0]}' "
                f"({t['panel_name'].iloc[0]}); keeping the table.")
            kept.append(t)
            continue
        if (start is None or run_date >= start) and (end is None or run_date <= end):
            kept.append(t)
    print(f"Date filter [{start_date or '...'} to {end_date or '...'}]: "
          f"kept {len(kept)} of {len(tables)} table(s).")
    return kept


# ==================================================================================
def _load_and_validate_table(
        fpath: pathlib.Path,
        file_type: str,
        verbose: bool = False,
    ) -> Optional[pd.DataFrame]:
    """Load one spectra table and check the comparison schema.

    Parameters
    ----------
    fpath : pathlib.Path
        Table path.
    file_type : str
        ``"parquet"`` or ``"csv"``.
    verbose : bool, optional
        Print the reason when a file is skipped. Default False.

    Returns
    -------
    pd.DataFrame or None
        The table, or None when unreadable / missing required columns.
    """
    required = {"band", "wavelength", "value", "Panel_ref", "sensor",
                "EM_Region", "node", "site", "date", "run", "panel_name",
                "target_type"}
    try:
        if file_type == "csv":
            df = pd.read_csv(fpath)
        else:
            df = pd.read_parquet(fpath)
    except Exception as er:
        warn.warn(f"Could not read {fpath}: {er}. Skipping file.")
        return None
    missing = required - set(df.columns)
    if missing:
        if verbose:
            tqdm.write(
                f"Skipping {fpath.name}: missing columns {sorted(missing)} "
                "(old schema? re-run QC02_SpectralCheck.py with --force).")
        return None
    if df.empty:
        if verbose:
            tqdm.write(f"Skipping {fpath.name}: no rows.")
        return None
    # Remember where the table came from so --save-dir can bundle the
    # sibling per-run QC report and figures alongside it.
    df.attrs["source_path"] = str(fpath)
    return df


# ==================================================================================
def load_external_spectra(
        load_dir: pathlib.Path,
        file_type: str,
    ) -> List[pd.DataFrame]:
    """Load spectra tables received from other nodes/collaborators.

    The directory is searched recursively, so it can point either at a
    flat folder of tables or at the root of a container produced by
    ``--save-dir`` (whose tables live under ``tables/``).

    Parameters
    ----------
    load_dir : pathlib.Path
        Directory containing the external tables.
    file_type : str
        ``"parquet"`` or ``"csv"``.

    Returns
    -------
    list of pd.DataFrame
        Validated tables.

    Raises
    ------
    NotADirectoryError
        If *load_dir* does not exist or is not a directory.
    """
    if not load_dir.is_dir():
        raise NotADirectoryError(
            f"The --load-dir path does not exist or is not a directory: {load_dir}")
    files = sorted(load_dir.rglob(f"*.{file_type}"))
    if len(files) == 0:
        warn.warn(
            f"No .{file_type} files found in --load-dir {load_dir}. "
            f"Ensure the files match the --type setting (currently '{file_type}').")
        return []
    loaded: List[pd.DataFrame] = []
    skipped = 0
    for fpath in tqdm(files, desc="Loading external spectra"):
        df = _load_and_validate_table(fpath, file_type, verbose=True)
        if df is None:
            skipped += 1
            continue
        loaded.append(df)
    print(f"Loaded {len(loaded)} external spectra file(s) from {load_dir}"
          + (f" ({skipped} skipped)" if skipped else ""))
    return loaded


# ==================================================================================
def save_spectra_copies(
        tables: List[pd.DataFrame],
        save_dir: pathlib.Path,
        file_type: str,
    ) -> None:
    """Build a portable spectral-accuracy container in *save_dir*.

    Copies every gathered spectra table into ``tables/`` plus, where the
    table's source run can be located on disk, the per-run QC02 QC
    report (``QC_spectra_report.json``) into ``reports/`` and the
    per-run figures (``QC_plots/*.png``) into ``figures/``. Filenames
    are built from the table metadata so files from different nodes,
    projects, sensors, and dates stay uniquely identifiable.

    Parameters
    ----------
    tables : list of pd.DataFrame
        Spectra tables (from :func:`gather_spectra_tables` /
        :func:`load_external_spectra`).
    save_dir : pathlib.Path
        Container root. Created (with parents) if missing.
    file_type : str
        ``"csv"`` or ``"parquet"``.

    Returns
    -------
    None
    """
    tables_dir = save_dir / "tables"
    reports_dir = save_dir / "reports"
    figures_dir = save_dir / "figures"
    tables_dir.mkdir(parents=True, exist_ok=True)
    counts = {"tables": 0, "reports": 0, "figures": 0}
    done_runs: set = set()
    for df in tables:
        if df.empty:
            continue
        row = df.iloc[0]
        stem = _table_copy_stem(row)
        outpath = tables_dir / f"{stem}.{file_type}"
        if file_type == "csv":
            df.to_csv(outpath.as_posix(), index=False)
        else:
            df.to_parquet(outpath.as_posix(), index=False)
        counts["tables"] += 1

        # +++++ Sibling per-run QC report + figures (once per run) +++++
        source = df.attrs.get("source_path")
        if source is None:
            continue
        qc_dir = pathlib.Path(source).parent.parent  # QC_Spectral_Tables -> QC_data
        if qc_dir in done_runs:
            continue
        done_runs.add(qc_dir)
        run_stem = _run_copy_stem(row)
        # contract detail JSON preferred; pre-migration legacy accepted
        script_dir = qc_dir / "QC02_SpectralCheck"
        report_candidates = [
            script_dir / "QC02_SpectralCheck_detail.json",
            script_dir / "QC_spectra_report.json",
            qc_dir / "QC_spectra_report.json",
        ]
        report = next((p for p in report_candidates if p.is_file()), None)
        if report is not None:
            reports_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(report, reports_dir / f"{run_stem}_{report.name}")
            counts["reports"] += 1
        fig_dirs = [script_dir / "QC_plots", qc_dir / "QC_plots"]
        figs = next((sorted(d.glob("*_spectra.png"))
                     for d in fig_dirs if d.is_dir()
                     and any(d.glob("*_spectra.png"))), [])
        for fig in figs:
            figures_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fig, figures_dir / f"{run_stem}_{fig.name}")
            counts["figures"] += 1
    print(f"Container built at {save_dir}: {counts['tables']} table(s), "
          f"{counts['reports']} report(s), {counts['figures']} run figure(s).")


# ==================================================================================
def _table_copy_stem(row: pd.Series) -> str:
    """Build the unique filename stem for one spectra table copy.

    Parameters
    ----------
    row : pd.Series
        First row of the table (carries the run metadata).

    Returns
    -------
    str
        Underscore-joined metadata stem.
    """
    parts = []
    for col in ["node", "project", "site", "sensor", "date", "run",
                "EM_Region", "gpro_nu", "panel_name"]:
        val = row.get(col, None)
        if val is None:
            continue
        if hasattr(val, "strftime"):
            val = val.strftime("%Y%m%d")
        parts.append(str(val))
    return "_".join(parts)


# ==================================================================================
def _run_copy_stem(row: pd.Series) -> str:
    """Build the per-run filename prefix for copied reports/figures.

    Parameters
    ----------
    row : pd.Series
        First row of a table from the run.

    Returns
    -------
    str
        Underscore-joined run-level metadata (no panel/EM-region parts).
    """
    parts = []
    for col in ["node", "project", "site", "sensor", "date", "run"]:
        val = row.get(col, None)
        if val is None:
            continue
        if hasattr(val, "strftime"):
            val = val.strftime("%Y%m%d")
        parts.append(str(val))
    return "_".join(parts)


# ==================================================================================
def prepare_comparison_frame(
        tables: List[pd.DataFrame],
        split_platforms: bool = False,
        verbose: bool = False,
    ) -> pd.DataFrame:
    """Combine tables, snap wavelengths, and build run labels/residuals.

    Wavelengths are snapped onto a shared per-sensor-group/EM-region
    reference grid (see :func:`Code.functions.spectral_qc.snap_wavelengths`)
    so runs from different sensor units group onto the same axis. By
    default, platforms observing the same EM region are pooled into one
    ``sensor_group`` (e.g. the CALVIS/GOBI shared Headwall VNIR) so the
    cross-run mean is the pooled cross-platform reference; the snapping
    unit is the node's platform-mounted sensor so physically different
    units still snap rather than being assumed identical. Run labels
    include only the metadata that differs across the frame (node/site/
    platform are added only when more than one is present).

    Parameters
    ----------
    tables : list of pd.DataFrame
        Validated spectra tables.
    split_platforms : bool, optional
        Keep each sensor platform as its own ``sensor_group`` (separate
        figures and references). Default False (pool platforms).
    verbose : bool, optional
        Print snap diagnostics. Default False.

    Returns
    -------
    pd.DataFrame
        Long frame with ``refl_pct``, ``residual_pct`` (deviation from
        the cross-run mean spectrum at each snapped wavelength), snapped
        ``wavelength`` (+ ``raw_wavelength``), ``target_group`` (physical
        panel set where identified, filename otherwise) and ``run_label``
        columns. Rows carrying the 0 = nodata sentinel (or NaN values)
        are dropped so they cannot pollute the means/residuals.
    """
    # +++++ Normalise reflectance to percent per table (dtype-dependent) +++++
    frames = [t.assign(refl_pct=sq.reflectance_pct(t["value"])) for t in tables]
    df = pd.concat(frames, ignore_index=True)

    # ========== Drop the 0 = nodata sentinel (QC02 convention) ==========
    nodata = sq.zero_nodata_mask(df["value"])
    if nodata.any():
        warn.warn(
            f"Dropping {int(nodata.sum()):,} of {len(df):,} rows "
            f"({float(nodata.mean()):.1%}) with 0 = nodata sentinel values "
            "(missing raster data over the panels, e.g. SWIR gaps).")
        df = df[~nodata]

    # ========== Pool platforms sharing an EM region (default) ==========
    # e.g. the CALVIS/GOBI shared Headwall VNIR: one figure set, with the
    # platform shown via line style + run label (--split-platforms undoes).
    if split_platforms:
        df["sensor_group"] = df["sensor"].astype(str)
    else:
        df["sensor_group"] = df.groupby("EM_Region")["sensor"].transform(
            lambda s: "-".join(sorted(s.astype(str).unique())))

    # ========== Snap wavelengths onto the shared reference grid ==========
    # unit = a node's platform-mounted sensor, so pooled platforms with
    # physically different units still snap instead of being assumed equal
    df["unit_key"] = df["node"].astype(str) + "/" + df["sensor"].astype(str)
    df = sq.snap_wavelengths(df, unit_col="unit_key",
                             sensor_col="sensor_group", verbose=verbose)

    # ========== Comparison group: physical panel set, not filename ==========
    # The same Gryfn4P hardware may be called QC_VAL_north in one flight and
    # QC_VAL_blue in another; grouping on target_type + panel_set compares
    # like with like. Unknown/legacy tables fall back to the filename.
    if "panel_set" not in df.columns:
        df["panel_set"] = "unknown"
    df["panel_set"] = df["panel_set"].fillna("unknown")
    known = df["panel_set"] != "unknown"
    df["target_group"] = np.where(
        known,
        df["target_type"].astype(str) + " " + df["panel_set"].astype(str),
        df["panel_name"].astype(str))
    n_unknown = int((~known).sum())
    if n_unknown:
        warn.warn(
            f"{n_unknown:,} rows have panel_set='unknown' (non-standard "
            "Panel_ref signature or pre-v2.2 table; re-run QC02 to migrate). "
            "They are grouped by filename instead.")

    # ========== Compact run labels: only include what differs ==========
    date_str = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y%m%d")
    date_str = date_str.fillna(df["date"].astype(str))
    label_cols = []
    if df["node"].nunique() > 1:
        label_cols.append(df["node"].astype(str))
    # same-named sites recur across projects on one node (I.A.Watson),
    # so project must disambiguate the label wherever it differs
    if "project" in df.columns and df["project"].nunique() > 1:
        label_cols.append(df["project"].astype(str))
    if df["site"].nunique() > 1:
        label_cols.append(df["site"].astype(str))
    # two platforms can share a date + run number: always disambiguate
    if df["sensor"].nunique() > 1:
        label_cols.append(df["sensor"].astype(str))
    label_cols.append(date_str)
    label_cols.append(df["run"].astype(str))
    if "gpro_nu" in df.columns and df["gpro_nu"].nunique() > 1:
        label_cols.append("g" + df["gpro_nu"].astype(str))
    df["run_label"] = label_cols[0].str.cat(label_cols[1:], sep=" ")

    # ========== Split duplicate same-set targets flown in one run ==========
    df = _split_duplicate_targets(df)

    # ========== Residual: deviation from the cross-run mean per wavelength ==========
    # Reference = mean of the per-run means at each snapped wavelength, so
    # every run carries equal weight regardless of pixel count. Pooled
    # platforms share one reference (the QC02/QC03 xplat convention).
    keys = ["sensor_group", "target_group", "EM_Region", "Panel_ref", "wavelength"]
    run_means = df.groupby(keys + ["run_label"], observed=True)["refl_pct"].mean()
    xrun_ref = run_means.groupby(level=keys, observed=True).mean().rename("_xrun_ref")
    df = df.join(xrun_ref, on=keys)
    df["residual_pct"] = df["refl_pct"] - df["_xrun_ref"]
    df = df.drop(columns="_xrun_ref")
    return df


# ==================================================================================
def _split_duplicate_targets(df: pd.DataFrame) -> pd.DataFrame:
    """Keep duplicate same-set targets within a run as distinct lines.

    Two physical sets of the same ``panel_set`` flown in a single run
    (e.g. ``QC_ELM_north`` + ``QC_ELM_south``) share ``target_group``
    and ``run_label``, so without intervention seaborn would silently
    average them into one line. Where a (``run_label``,
    ``target_group``) pair contains more than one ``panel_name``, the
    ``target_id`` (or an enumerated ``s1``/``s2`` fallback) is appended
    to ``run_label`` so each set keeps its own line and residual mean.

    Parameters
    ----------
    df : pd.DataFrame
        Frame with ``run_label``, ``target_group`` and ``panel_name``
        columns (``target_id`` optional).

    Returns
    -------
    pd.DataFrame
        Same frame with ``run_label`` disambiguated where needed.
    """
    dup = df.groupby(["run_label", "target_group"], observed=True
                     )["panel_name"].transform("nunique") > 1
    if not dup.any():
        return df
    ids = (df["target_id"].astype("string")
           if "target_id" in df.columns
           else pd.Series(pd.NA, index=df.index, dtype="string"))
    # +++++ Fall back to s1/s2/... where target_id is missing/ambiguous +++++
    for (label, group), g in df.loc[dup].groupby(
            ["run_label", "target_group"], observed=True):
        g_ids = ids.loc[g.index]
        if g_ids.notna().all() and (
                g_ids.groupby(g["panel_name"], observed=True).nunique() == 1
                ).all() and g_ids.nunique() == g["panel_name"].nunique():
            continue
        ranks = {p: f"s{i + 1}"
                 for i, p in enumerate(sorted(g["panel_name"].unique()))}
        ids.loc[g.index] = g["panel_name"].map(ranks)
        warn.warn(
            f"Run '{label}' has {len(ranks)} '{group}' targets without "
            f"distinct target_ids; labelling them {sorted(ranks.values())} "
            "by panel filename.")
    df.loc[dup, "run_label"] = (
        df.loc[dup, "run_label"] + " " + ids.loc[dup].astype(str))
    return df


# ==================================================================================
def join_expected_dhr(
        df: pd.DataFrame,
        comp: Optional[pd.DataFrame],
    ) -> Tuple[pd.DataFrame, Tuple[str, str, Dict[str, Any]]]:
    """Join each run's expected DHR onto the pixel frame.

    For every (node, project, site, sensor, date, run, panel file, EM
    region, panel code) group with a QC02 DHR artifact, the expected
    reflectance curve is interpolated onto the snapped wavelengths and
    the per-pixel accuracy ``dhr_delta_pct = refl_pct - exp_pct`` is
    computed. Each run is only ever compared against the DHR its own
    node resolved (there is no cross-node fallback), so combined scopes
    holding different hardware truths stay correct by construction.

    Also grades the advisory serial-mixing check: signature-based
    ``target_group``s that span more than one physical set serial keep a
    single (blended) cross-run precision reference, so mixing is
    reported rather than split (operator choice 2026-08-26).

    Parameters
    ----------
    df : pd.DataFrame
        Frame from :func:`prepare_comparison_frame`.
    comp : pd.DataFrame or None
        Combined DHR comparison frame from
        :func:`aggregate_dhr_comparisons` (None when no artifacts).

    Returns
    -------
    tuple
        ``(df, check)`` -- *df* with added ``exp_pct``, ``serial`` and
        ``dhr_delta_pct`` columns, and a ``(name, status, kwargs)``
        check tuple for the contract report.
    """
    df = df.copy()
    df["exp_pct"] = np.nan
    df["serial"] = pd.Series(pd.NA, index=df.index, dtype="string")
    if comp is None:
        df["dhr_delta_pct"] = np.nan
        return df, ("dhr_serial_mixing", "not_checked",
                    {"advisory": True,
                     "note": "no per-run DHR artifacts to join"})

    # +++++ Normalised join keys (comp's parsed site drops the year prefix) +++++
    keys = ["node", "project", "site_key", "sensor", "date_key",
            "run_number", "panel_name", "EM_Region", "Panel_ref"]
    df["site_key"] = df["site"].astype(str).str.replace(
        r"^\d{4}", "", regex=True)
    df["date_key"] = pd.to_datetime(df["date"], errors="coerce"
                                    ).dt.strftime("%Y%m%d")
    df["run_number"] = (df["run"].astype(str)
                        .str.extract(r"(\d+)", expand=False)
                        .fillna(-1).astype(int))
    comp = comp.assign(
        site_key=comp["site"].astype(str),
        date_key=comp["date"].astype(str),
        run_number=comp["run_number"].astype(int))

    # +++++ Interpolate each run's expected curve onto the snapped grid +++++
    groups = df.groupby(keys, observed=True).groups
    n_hit = 0
    for gkey, cg in comp.groupby(keys, observed=True):
        idx = groups.get(gkey)
        if idx is None:
            continue
        curve = (cg.dropna(subset=["exp_pct"])
                 .drop_duplicates("wavelength").sort_values("wavelength"))
        if curve.empty:
            continue
        df.loc[idx, "exp_pct"] = np.interp(
            df.loc[idx, "wavelength"], curve["wavelength"], curve["exp_pct"])
        df.loc[idx, "serial"] = str(curve["serial"].iloc[0])
        n_hit += len(idx)
    df["dhr_delta_pct"] = df["refl_pct"] - df["exp_pct"]
    df = df.drop(columns=["site_key", "date_key", "run_number"])
    print(f"Expected DHR joined onto {n_hit:,} of {len(df):,} pixel rows "
          f"({n_hit / len(df):.0%}).")

    # ========== Advisory: signature groups spanning >1 hardware set ==========
    with_serial = df[df["serial"].notna()]
    if with_serial.empty:
        return df, ("dhr_serial_mixing", "not_checked",
                    {"advisory": True,
                     "note": "no runs matched a DHR artifact"})
    sets = (with_serial.assign(
                serial_set=with_serial["serial"].str.split("-").str[0])
            .groupby(["sensor_group", "target_group"], observed=True)
            ["serial_set"].unique())
    mixed = {key: sorted(v) for key, v in sets.items() if len(v) > 1}
    if not mixed:
        return df, ("dhr_serial_mixing", "good",
                    {"advisory": True,
                     "note": "each target group resolves to one physical "
                             "set serial"})
    desc = "; ".join(f"{grp} ({sg}): sets {', '.join(v)}"
                     for (sg, grp), v in mixed.items())
    warn.warn(
        "Signature-grouped targets span multiple DHR serial sets - the "
        f"cross-run precision reference blends hardware: {desc}")
    return df, ("dhr_serial_mixing", "warning",
                {"advisory": True,
                 "value": desc,
                 "note": "signature-only grouping keeps sets together: "
                         "precision blends hardware; accuracy and the "
                         "dashed expected curves stay per-hardware"})


# ==================================================================================
def plot_comparison_spectra(
        df: pd.DataFrame,
        plot_dir: Optional[pathlib.Path],
        show: bool = False,
        errorbar: str = "pi",
        bad_wavelengths: Optional[Dict[str, Dict[str, List[Tuple[float, float]]]]] = None,
        copy_dir: Optional[pathlib.Path] = None,
        verbose: bool = False,
    ) -> None:
    """Draw the cross-run spectra figures (QC01-style refinements).

    For every (sensor group, target, EM region) up to three figures are
    produced, faceted by panel with one line per run:

    - ``refl``      observed reflectance, with the expected DHR overlaid
                    as dashed black curves (deduped: identical expected
                    spectra collapse onto one line)
    - ``accuracy``  observed - expected DHR (pp, symlog); skipped when
                    no run in the group resolved a DHR
    - ``precision`` residual vs the cross-run mean (% refl, symlog)

    Pooled platforms are told apart by line style (``style="sensor"``)
    and the run label. Symlog y-axes keep small systematic offsets
    readable next to artefact spikes.
    Known-bad wavelength ranges are forced to a hard 0 so they render
    as an unmissable dip: seaborn line plots drop NaN rows and bridge
    straight across, so a NaN "mask" silently disappears
    (APEx_SensorCalibration ``zero_bad_bands`` convention). A single
    palette dict is used for all figures so a run keeps its colour
    everywhere (CARTO Bold <=10 runs / Tableau_20 <=20 / glasbey_dark
    beyond).

    Parameters
    ----------
    df : pd.DataFrame
        Frame from :func:`prepare_comparison_frame`.
    plot_dir : pathlib.Path or None
        Directory to save figures; None saves nothing.
    show : bool, optional
        Display each figure interactively. Default False.
    errorbar : str, optional
        ``"pi"`` (default), ``"sd"`` or ``"none"``.
    bad_wavelengths : dict, optional
        ``{sensor: {EM_Region: [(lo_nm, hi_nm), ...]}}`` ranges to mask.
    copy_dir : pathlib.Path, optional
        Also save each figure into this directory (the --save-dir
        container's ``comparison_figures/``). Default None.
    verbose : bool, optional
        Print per-figure diagnostics. Default False.

    Returns
    -------
    None
    """
    sns.set_style("whitegrid")
    plt.rcParams.update({
        "font.size": 11,
        "axes.titleweight": "bold",
        "axes.labelweight": "bold",
        "figure.titleweight": "bold",
    })

    # +++++ One palette for the whole call: stable colours across figures +++++
    palette = sq.resolve_run_palette(list(df["run_label"].unique()))

    # +++++ Zero known-bad wavelengths (hard dip; NaN would be bridged) +++++
    data = df.copy()
    if bad_wavelengths:
        for sensor, regions in bad_wavelengths.items():
            for region, ranges in regions.items():
                rows = (data["sensor"] == sensor) & (data["EM_Region"] == region)
                cut = rows & sq.bad_wavelength_mask(data["wavelength"], ranges)
                # NaN deltas (no DHR) must stay NaN, not become fake zeros
                for col in ["refl_pct", "residual_pct", "dhr_delta_pct"]:
                    data.loc[cut & data[col].notna(), col] = 0.0

    expected: set = set()   # filenames this run writes
    patterns: set = set()   # sensor-group-agnostic globs for those figures
    for (sensor, group, region), sub in data.groupby(
            ["sensor_group", "target_group", "EM_Region"]):
        style_col = "sensor" if sub["sensor"].nunique() > 1 else None
        for var, label, suffix in [
                ("refl_pct", "Reflectance (%)", "refl"),
                ("dhr_delta_pct", "Obs - expected DHR (pp)", "accuracy"),
                ("residual_pct", "Residual vs cross-run mean (% refl)",
                 "precision")]:
            if var == "dhr_delta_pct" and not sub[var].notna().any():
                continue  # no run in this group resolved a DHR
            _make_comparison_figure(
                sub, str(sensor), str(group), str(region), var, label,
                suffix=suffix, palette=palette, plot_dir=plot_dir, show=show,
                errorbar=errorbar, style_col=style_col,
                copy_dir=copy_dir, verbose=verbose)
            parts = [cf.safe_filename_component(v)
                     for v in (str(sensor), str(group), str(region), suffix)]
            expected.add("_".join(parts) + ".png")
            patterns.add("*_" + "_".join(parts[1:]) + ".png")

    # +++++ Same logical figure under an outdated sensor-group prefix +++++
    # (scope contents or --split-platforms changed) must not linger
    for dest in (plot_dir, copy_dir):
        if dest is None or not dest.is_dir():
            continue
        for pat in sorted(patterns):
            for fpath in dest.glob(pat):
                if fpath.name not in expected:
                    fpath.unlink()
                    print(f"  removed superseded {fpath.name}")


# ==================================================================================
def _make_comparison_figure(
        sub: pd.DataFrame,
        sensor: str,
        target: str,
        region: str,
        var: str,
        var_label: str,
        suffix: str,
        palette: Dict[str, Any],
        plot_dir: Optional[pathlib.Path],
        show: bool,
        errorbar: str,
        style_col: Optional[str] = None,
        copy_dir: Optional[pathlib.Path] = None,
        verbose: bool = False,
    ) -> None:
    """Draw and save one faceted cross-run figure.

    Parameters
    ----------
    sub : pd.DataFrame
        Rows for one (sensor group, target, EM region).
    sensor, target, region : str
        Labels for the title/filename (*sensor* is the sensor group).
    var : str
        Column plotted on the y-axis (non-reflectance vars get symlog).
        The reflectance figure also overlays the expected DHR curves.
    var_label : str
        Axis label for *var*.
    suffix : str
        Filename suffix (``refl``/``accuracy``/``precision``).
    palette : dict
        Shared ``{run_label: colour}`` map.
    plot_dir : pathlib.Path or None
        Save directory; None = don't save.
    show : bool
        Display the figure interactively.
    errorbar : str
        ``"pi"``, ``"sd"`` or ``"none"``.
    style_col : str, optional
        Column mapped to line style (``"sensor"`` when platforms are
        pooled). Default None.
    copy_dir : pathlib.Path, optional
        Extra directory to also save the figure into (--save-dir
        container). Default None.
    verbose : bool, optional
        Print the output path. Default False.

    Returns
    -------
    None
    """
    is_symlog = var != "refl_pct"
    present = set(sub["run_label"].dropna().astype(str).unique())
    hue_order = [h for h in palette if h in present]
    print(f"Plotting {var_label} for sensor: {sensor}, target: {target}, region: {region}")
    g = sns.relplot(
        data=sub,
        x="wavelength", y=var,
        col="Panel_ref", col_wrap=2,
        hue="run_label", hue_order=hue_order, palette=palette,
        style=style_col,
        kind="line",
        errorbar=None if errorbar == "none" else errorbar,
    )
    g.set_xlabels("Wavelength (nm)")
    g.set_ylabels(var_label)
    if g.legend is not None:
        g.legend.set_frame_on(False)
        if style_col is None:
            g.legend.set_title("Run")
        plt.setp(g.legend.get_texts(), fontfamily="monospace")
    g.figure.suptitle(
        f"Sensor: {sensor}, Target: {target}, EM range: {region}",
        y=0.98, fontweight="bold")
    g.figure.subplots_adjust(top=0.92)

    if is_symlog:
        # Symlog keeps small systematic offsets readable next to spikes
        g.set(yscale="symlog")
        for ax in g.axes.flat:
            ax.yaxis.set_major_formatter(mticker.ScalarFormatter())
            ax.yaxis.get_major_formatter().set_scientific(False)
            ax.axhline(0, color="0.4", lw=0.6)
            ax.minorticks_on()
            ax.yaxis.set_minor_locator(mticker.SymmetricalLogLocator(
                ax.yaxis.get_transform(), subs=np.arange(2, 10)))
            ax.grid(True, which="both", linestyle="--", linewidth=0.5, color="0.4")
    else:
        # +++++ Linear: major gridlines only (minor grid is too busy) +++++
        for ax in g.axes.flat:
            ax.grid(True, which="major", linestyle="--", linewidth=0.5, color="0.4")
            ax.grid(False, which="minor")

    if var == "refl_pct" and "exp_pct" in sub.columns:
        _overlay_expected_dhr(g, sub)

    if plot_dir is not None or copy_dir is not None:
        parts = [cf.safe_filename_component(v)
                 for v in (sensor, target, region, suffix)]
        fname = f"{'_'.join(parts)}.png"
        for dest in (plot_dir, copy_dir):
            if dest is None:
                continue
            dest.mkdir(parents=True, exist_ok=True)
            outpath = dest / fname
            g.figure.savefig(outpath.as_posix(), dpi=150, bbox_inches="tight")
            if verbose:
                print(f"  saved {outpath}")
    if show:
        plt.show()
    plt.close(g.figure)


# ==================================================================================
def _overlay_expected_dhr(
        g: sns.FacetGrid,
        sub: pd.DataFrame,
    ) -> None:
    """Overlay dashed expected-DHR curves on a reflectance facet grid.

    One black dashed curve per *distinct* expected spectrum per facet:
    serials whose curves are numerically identical (e.g. the same Gryfn
    panel values shared across nodes) collapse onto a single line
    labelled with every matching serial; genuinely different DHRs (e.g.
    AU's Gryfn4) keep separate lines with distinct dash patterns.

    Parameters
    ----------
    g : sns.FacetGrid
        The relplot grid (facets keyed by ``Panel_ref``).
    sub : pd.DataFrame
        Rows for the figure's (sensor group, target, EM region), with
        ``exp_pct``/``serial`` from :func:`join_expected_dhr`.

    Returns
    -------
    None
    """
    linestyles = ["--", ":", "-."]
    for ref, ax in g.axes_dict.items():
        pdf = sub[(sub["Panel_ref"] == ref) & sub["exp_pct"].notna()]
        if pdf.empty:
            continue
        # +++++ Dedupe: identical expected spectra collapse onto one line +++++
        curves: Dict[tuple, Tuple[List[str], pd.DataFrame]] = {}
        for serial, sdf in pdf.groupby("serial"):
            curve = (sdf.groupby("wavelength", as_index=False)["exp_pct"]
                     .first().sort_values("wavelength"))
            key = tuple(zip(np.round(curve["wavelength"], 2),
                            np.round(curve["exp_pct"], 3)))
            if key in curves:
                curves[key][0].append(str(serial))
            else:
                curves[key] = ([str(serial)], curve)
        handles = []
        for i, (serials, curve) in enumerate(curves.values()):
            line, = ax.plot(
                curve["wavelength"], curve["exp_pct"],
                color="k", ls=linestyles[i % len(linestyles)], lw=1.3,
                zorder=1, label="DHR " + "/".join(sorted(serials)))
            handles.append(line)
        ax.legend(handles=handles, loc="best", fontsize=7, frameon=False)


# ==================================================================================
def cross_run_stats(df: pd.DataFrame) -> None:
    """Cross-run statistical comparison (placeholder).

    Will implement pairwise Wasserstein-1 distances between runs and
    seasonal drift diagnostics once the equivalence test and margins
    under development in APEx_SensorCalibration (ET00 mean-TOST / ET03
    Wasserstein-1) are finalised. The prepared frame already carries
    everything the tests need (snapped ``wavelength``, per-pixel
    ``refl_pct``, ``run_label``, ``Panel_ref``).

    Parameters
    ----------
    df : pd.DataFrame
        Frame from :func:`prepare_comparison_frame`.

    Returns
    -------
    None
    """
    print(
        "\nPairwise distribution statistics (Wasserstein-1) are not "
        "implemented yet; pending the ET00/ET03 equivalence test from "
        "APEx_SensorCalibration.")


# ==================================================================================
def aggregate_dhr_comparisons(
        path: pathlib.Path,
        out_dir: Optional[pathlib.Path],
        spec_path: pathlib.Path,
        exclude_dirs: Optional[List[str]] = None,
        include_runs: Optional[str] = None,
        include_duplicates: bool = False,
        verbose: bool = False,
    ) -> Dict[str, Any]:
    """Aggregate the per-run QC02 DHR comparisons across runs (ex DT01).

    Gathers the ``DHR_*_comparison`` / ``DHR_*_delta_stats`` parquets
    QC02 writes into each run's ``QC_data/QC02_SpectralCheck/`` folder,
    writes combined ``all_runs`` tables, and computes the advisory
    within-day panel-bias drift check (bias walk across a day's runs,
    Spearman-correlated against run order and QC01's solar elevation).
    The combined comparison frame is returned for the expected-DHR
    overlay/accuracy figures (see :func:`join_expected_dhr`).

    Parameters
    ----------
    path : pathlib.Path
        Scope folder crawled for per-run DHR artifacts.
    out_dir : pathlib.Path or None
        Scoped output folder; None saves nothing.
    spec_path : pathlib.Path
        ``spectral_limits.yml`` (drift thresholds). Missing file grades
        the drift check ``not_checked``.
    exclude_dirs : list of str, optional
        Directory names to exclude from the crawl.
    include_runs : str or None, optional
        ``--include-runs`` severity ladder level (None = clean only).
    include_duplicates : bool, optional
        Include runs flagged ``DuplicateRun``. Default False.
    verbose : bool, optional
        Print per-file diagnostics. Default False.

    Returns
    -------
    dict
        ``{"checks": [(name, status, kwargs), ...], "n_runs": int,
        "config": snapshot or None, "comp": DataFrame or None}`` for
        the contract report and the expected-DHR join. All checks are
        advisory.
    """
    # ========== Load the drift thresholds (§5, advisory) ==========
    spec, snapshot = None, None
    if spec_path.is_file():
        loaded = qr.load_thresholds(spec_path.name,
                                    thresholds_dir=spec_path.parent)
        spec = loaded["spec"]
        snapshot = {"path": loaded["path"], "sha256": loaded["sha256"]}
    else:
        warn.warn(f"Spectral limits {spec_path} missing - the within-day "
                  "drift check will report not_checked.")
    out: Dict[str, Any] = {"checks": [], "n_runs": 0, "config": snapshot,
                           "comp": None}

    # ========== Gather the per-run artifacts ==========
    comp, stats = _gather_dhr_tables(path, exclude_dirs,
                                     include_runs=include_runs,
                                     include_duplicates=include_duplicates,
                                     verbose=verbose)
    if comp is None or stats is None:
        out["checks"].append((
            "dhr_within_day_drift", "not_checked",
            {"advisory": True,
             "note": "no per-run DHR comparison artifacts under this scope "
                     "- run QC02_SpectralCheck first"}))
        return out
    _apply_dhr_run_labels(comp, stats)
    out["n_runs"] = int(stats["run_dir"].nunique())
    out["comp"] = comp
    print(f"DHR aggregation: {out['n_runs']} run(s), "
          f"{stats['panel_name'].nunique()} target(s), "
          f"{len(comp):,} comparison rows.")

    # ========== Combined tables (P7 parquet) ==========
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        comp.to_parquet(out_dir / "QA02_all_runs_comparison.parquet",
                        index=False)
        stats.to_parquet(out_dir / "QA02_all_runs_delta_stats.parquet",
                         index=False)

    # ========== Within-day drift check vs run order + solar geometry ==========
    stats["solar_elevation_deg"] = stats["run_dir"].map(_run_solar_elevation)
    drift_cfg = (spec or {}).get("within_day_drift", {})
    drift = sq.within_day_drift(stats,
                                min_runs=int(drift_cfg.get("min_runs", 3)))
    if out_dir is not None and not drift.empty:
        drift.to_parquet(out_dir / "QA02_dhr_within_day_drift.parquet",
                         index=False)
    out["checks"].append(
        _drift_check(drift, drift_cfg, spec_missing=spec is None))
    return out


# ==================================================================================
def _gather_dhr_tables(
        path: pathlib.Path,
        exclude_dirs: Optional[List[str]] = None,
        include_runs: Optional[str] = None,
        include_duplicates: bool = False,
        verbose: bool = False,
    ) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    """Load and identity-stamp every per-run DHR artifact pair under *path*.

    Only complete pairs inside a ``QC02_SpectralCheck`` folder whose run
    path parses cleanly are kept; each frame gains ``node, site, sensor,
    date, run_number, run_dir`` columns (the comparison frame also gains
    ``panel_name`` recovered from the filename). Runs flagged in
    ``RunOverview.csv`` are excluded unless opted in.

    Parameters
    ----------
    path : pathlib.Path
        Scope folder to crawl.
    exclude_dirs : list of str, optional
        Directory names to exclude.
    include_runs : str or None, optional
        ``--include-runs`` severity ladder level (None = clean only).
    include_duplicates : bool, optional
        Include runs flagged ``DuplicateRun``. Default False.
    verbose : bool, optional
        Print per-file diagnostics. Default False.

    Returns
    -------
    tuple of (pd.DataFrame, pd.DataFrame) or (None, None)
        Concatenated (comparison, delta_stats) frames, or (None, None)
        when nothing usable was found.
    """
    print(f"Scanning {path} for per-run DHR comparison artifacts ...")
    files = sorted(p for p in path.rglob("DHR_*_comparison.parquet")
                   if p.parent.name == "QC02_SpectralCheck")
    if exclude_dirs:
        exclude_set = set(exclude_dirs)
        files = [f for f in files
                 if not (set(p.name for p in f.parents) & exclude_set)]
    files = _exclude_flagged_runs(files, include_runs, include_duplicates)
    comp_parts: List[pd.DataFrame] = []
    stats_parts: List[pd.DataFrame] = []
    for fpath in tqdm(files, desc="Loading DHR artifacts"):
        spath = fpath.with_name(
            fpath.name.replace("_comparison.parquet", "_delta_stats.parquet"))
        if not spath.is_file():
            warn.warn(f"{fpath.name} has no matching delta-stats parquet; "
                      "skipping the pair (re-run QC02_SpectralCheck).")
            continue
        run_dir = fpath.parents[3]  # QC02_SpectralCheck/QC_data/T1_proc/<run>
        meta = cf.parse_APPN_dataset_path(run_dir)
        if not meta["valid"] or meta["run"] is None:
            warn.warn(f"Could not parse run identity for {run_dir} "
                      f"({meta['errors']}); skipping {fpath.name}.")
            continue
        ident = {
            "node": str(meta["node"]), "project": str(meta["project"]),
            "site": str(meta["site"]), "sensor": str(meta["sensor"]),
            "date": pd.Timestamp(meta["date"]).strftime("%Y%m%d"),
            "run_number": int(meta["run"]), "run_dir": str(run_dir),
        }
        comp = pd.read_parquet(fpath)
        region = str(comp["EM_Region"].iloc[0])
        stem = fpath.name[len("DHR_"):-len("_comparison.parquet")]
        target = stem[:-(len(region) + 1)] if stem.endswith(f"_{region}") else stem
        comp = comp.assign(panel_name=target, **ident)
        stats = pd.read_parquet(spath).assign(**ident)
        comp_parts.append(comp)
        stats_parts.append(stats)
        if verbose:
            tqdm.write(f"  {ident['date']} run_{ident['run_number']:02d}: "
                       f"{target} {region}")
    if not comp_parts:
        return None, None
    return (pd.concat(comp_parts, ignore_index=True),
            pd.concat(stats_parts, ignore_index=True))


# ==================================================================================
def _apply_dhr_run_labels(
        comp: pd.DataFrame,
        stats: pd.DataFrame,
    ) -> None:
    """Add compact ``run_label`` columns shared by both DHR frames.

    Mirrors :func:`prepare_comparison_frame`: node/project/site enter the
    label only when more than one is present across the union of both
    frames; date and run number always do.

    Parameters
    ----------
    comp, stats : pd.DataFrame
        Identity-stamped frames from :func:`_gather_dhr_tables`,
        modified in place.

    Returns
    -------
    None
    """
    ident_cols = ["node", "project", "site"]
    union = pd.concat([comp[ident_cols], stats[ident_cols]])
    for df in (comp, stats):
        parts = []
        for col in ident_cols:
            if union[col].nunique() > 1:
                parts.append(df[col].astype(str))
        parts.append(df["date"].astype(str))
        parts.append("run_" + df["run_number"].astype(int).astype(str).str.zfill(2))
        df["run_label"] = parts[0].str.cat(parts[1:], sep=" ")


# ==================================================================================
def _run_solar_elevation(run_dir: str) -> float:
    """Mean solar elevation of a run from its QC01 detail JSON.

    Parameters
    ----------
    run_dir : str
        Run folder path (as stamped by :func:`_gather_dhr_tables`).

    Returns
    -------
    float
        Mean of QC01's ``solar_elevation_deg_range``, or NaN when the
        detail JSON (or the solar block) is unavailable.
    """
    detail = (pathlib.Path(run_dir) / "T1_proc" / "QC_data"
              / "QC01_FlightCheck" / "QC01_FlightCheck_detail.json")
    if not detail.is_file():
        return float("nan")
    try:
        payload = json.loads(detail.read_text())
    except json.JSONDecodeError as err:
        warn.warn(f"Unreadable QC01 detail JSON {detail}: {err}")
        return float("nan")
    rng = (payload.get("acquisition_report", {}).get("solar", {})
           .get("solar_elevation_deg_range"))
    if not rng or any(v is None for v in rng):
        return float("nan")
    return float(np.mean(rng))


# ==================================================================================
def _drift_check(
        drift: pd.DataFrame,
        drift_cfg: Dict[str, Any],
        spec_missing: bool,
    ) -> Tuple[str, str, Dict[str, Any]]:
    """Grade the advisory within-day drift check from the drift table.

    Parameters
    ----------
    drift : pd.DataFrame
        Output of :func:`Code.functions.spectral_qc.within_day_drift`.
    drift_cfg : dict
        The ``within_day_drift`` block of ``spectral_limits.yml``.
    spec_missing : bool
        True when no limits spec was loaded (grades ``not_checked``).

    Returns
    -------
    tuple
        ``(name, status, kwargs)`` for ``qr.add_check``.
    """
    name = "dhr_within_day_drift"
    if drift.empty:
        min_runs = int(drift_cfg.get("min_runs", 3))
        return (name, "not_checked",
                {"advisory": True,
                 "note": f"no day x target x panel group spans >= "
                         f"{min_runs} runs"})
    worst = drift.loc[drift["range_pp"].idxmax()]
    limit = None if spec_missing else drift_cfg.get("range_warn_above_pp")
    status = "not_checked" if limit is None else (
        "warning" if float(worst["range_pp"]) > float(limit) else "good")
    solar_rho = worst["spearman_solar_rho"]
    note = f"run-order rho {worst['spearman_run_rho']:+.2f}"
    note += (f", solar-elevation rho {solar_rho:+.2f}"
             if np.isfinite(solar_rho)
             else ", solar geometry unavailable (run QC01_FlightCheck)")
    return (name, status, {
        "advisory": True,
        "value": (f"worst bias walk {worst['range_pp']:.1f} pp "
                  f"(panel {worst['Panel_ref']}, {worst['date']} "
                  f"{worst['panel_name']} {worst['EM_Region']}, "
                  f"{int(worst['n_runs'])} runs)"),
        "threshold": (f"within-day bias range <= {limit} pp per panel, "
                      "full region, bad bands masked"
                      if limit is not None else None),
        "note": note,
        "evidence": drift.to_dict(orient="records"),
    })


# ==================================================================================
if __name__ == "__main__":
    # ========== Parse args ==========
    parser = argparse.ArgumentParser(description="Compare extracted panel spectra across runs (multi-run spectral QC).")
    parser.add_argument("--path", type=str, default=None, help="Node or project folder to crawl for extracted spectra tables. Defaults to the git repo root. Node folders save results to <Node>/Documents/QAReports/, project folders to <Project>/Documentation/QAReports/; any other level requires --output-dir.")
    parser.add_argument("--output-dir", type=str, default=None, help="Explicit output directory for figures (overrides the node/project routing; required for other path levels unless --no-save).")
    parser.add_argument("--no-save", default=False, action="store_true", help="Display the figures interactively instead of saving them. Nothing is written to disk.")
    parser.add_argument("--type", type=str, default="parquet", choices=["parquet", "csv"], help="File type of the extracted spectra tables. Default parquet.")
    parser.add_argument("--load-dir", type=str, default=None, help="Also load spectra tables from this folder, searched recursively (e.g. a --save-dir container received from another node).")
    parser.add_argument("--save-dir", type=str, default=None, help="Build a portable spectral-accuracy container in this directory: gathered tables (tables/), per-run QC02 reports (reports/) and figures (figures/), and this script's comparison figures (comparison_figures/).")
    parser.add_argument("--start-date", type=str, default=None, help="Only include runs on or after this date (inclusive; e.g. 2026-08-01 or 20260801).")
    parser.add_argument("--end-date", type=str, default=None, help="Only include runs on or before this date (inclusive).")
    parser.add_argument("--errorbar", type=str, default="pi", choices=["pi", "sd", "none"], help="Spread band around each run line: pi (percentile interval, default), sd (+/- one standard deviation), or none (mean lines only).")
    parser.add_argument("--split-platforms", default=False, action="store_true", help="Keep sensor platforms in separate figures instead of pooling platforms that share an EM region (e.g. the CALVIS/GOBI shared Headwall VNIR). Pooled figures show the platform via line style and the run label.")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[], help="Directory names to exclude from the table search.")
    parser.add_argument("--include-runs", type=str, default=None, choices=["untriaged", "degraded", "failed"], help="Cumulative severity ladder for runs flagged in RunOverview.csv. Default: clean runs only (no flags, Deviations only, or Issues with every ticket closed ok/fixed). untriaged also includes Issues runs with open TODO/wip tickets or no Issues.yaml yet; degraded adds confirmed caution/failed tickets (and unparseable yamls); failed adds RunFailed runs.")
    parser.add_argument("--include-duplicates", default=False, action="store_true", help="Include runs flagged DuplicateRun in RunOverview.csv (reprocessings of another run's raw, e.g. BaseStation GNSS re-runs). Independent of --include-runs.")
    parser.add_argument("--spec", type=str, default="reference/thresholds/spectral_limits.yml", help="Spectral-limits YAML relative to the repo root (within-day drift thresholds; the advisory drift check reports not_checked if missing).")
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
    # Relative dir args must also resolve BEFORE the chdir, else they
    # land inside the crawl target.
    for attr in ("output_dir", "save_dir", "load_dir"):
        val = getattr(args, attr)
        if val is not None:
            setattr(args, attr, str(pathlib.Path(val).resolve()))
    os.chdir(path)

    # ========== Parse Args to main function ==========
    main(args, path)
