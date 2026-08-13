"""Multi-run spectral QC comparison (QA02).

Gathers the per-run panel spectra tables produced by
``QA00_SpectralValidation.py`` (``QC_Spectral_Tables/QC_*_spectra_*``)
across every run under the given path, optionally appends tables received
from other nodes (``--load-dir``), aligns wavelengths onto a shared
reference grid, and produces cross-run comparison figures (per-panel
reflectance and residual spectra, one line per run).

Where results are saved depends on the level of the path provided:

- node folder    -> ``<Node>/Documents/QCReports/``
- project folder -> ``<Project>/Documentation/QCReports/``
- anything else  -> ``--output-dir`` is required.

``--no-save`` displays the figures interactively instead of saving them.

Cross-run statistics (pairwise Wasserstein-1 distances, seasonal drift)
are planned but not yet implemented; they depend on the equivalence test
under development in APEx_SensorCalibration (ET00/ET03).

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
    every gathered spectra table (``tables/``), the per-run QA00 QC
    reports (``reports/``) and figures (``figures/``), plus the
    comparison figures produced by this script
    (``comparison_figures/``).
--start-date : str, optional
    Only include runs on or after this date (e.g. 2026-08-01 or 20260801).
--end-date : str, optional
    Only include runs on or before this date.
--errorbar : {pi, sd, none}
    Spread band around each run line. Default pi.
"""

# ==============================================================================

__title__ = "Spectral run comparison"
__author__ = "Arden Burrell"
__version__ = "v1.3(13.08.2026)"
__email__ = "arden.burrell@sydney.edu.au"

# ==============================================================================
# ========== Import core packages ==========
import os
import sys
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
import Code.functions.spectral_qc as sq


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
    out_dir = cf.resolve_qcreports_dir(path, args.output_dir, args.no_save)

    # ========== Gather the extracted spectra tables ==========
    tables = gather_spectra_tables(
        path, file_type=args.type, exclude_dirs=args.exclude_dir,
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
            + ". Run QA00_SpectralValidation.py first to extract them.")

    # ========== Save copies to --save-dir if provided ==========
    save_dir = pathlib.Path(args.save_dir) if args.save_dir is not None else None
    if save_dir is not None:
        save_spectra_copies(tables, save_dir, args.type)

    # ========== Combine, align wavelengths, and label the runs ==========
    df = prepare_comparison_frame(tables, verbose=args.verbose)
    print(f"Prepared {len(df):,} rows across {df['run_label'].nunique()} run(s), "
          f"{df['sensor'].nunique()} sensor(s).")

    # ========== Cross-run comparison figures ==========
    plot_comparison_spectra(
        df, plot_dir=out_dir, show=args.no_save,
        errorbar=args.errorbar,
        bad_wavelengths=sq.default_bad_wavelengths(),
        copy_dir=(save_dir / "comparison_figures") if save_dir else None,
        verbose=args.verbose)

    # ========== Cross-run statistics (future) ==========
    cross_run_stats(df)

    if out_dir is not None:
        print(f"\nAll comparison figures saved to: {out_dir}")
    else:
        print("\n*** NOTHING WAS SAVED (--no-save): figures were displayed only. ***")


# ==================================================================================
def gather_spectra_tables(
        path: pathlib.Path,
        file_type: str = "parquet",
        exclude_dirs: Optional[List[str]] = None,
        verbose: bool = False,
    ) -> List[pd.DataFrame]:
    """Find and load every extracted spectra table under *path*.

    Searches for ``QC_*_spectra_*.<type>`` files inside
    ``QC_Spectral_Tables`` folders (the QA00 output convention) and
    loads the ones that pass schema validation.

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively.
    file_type : str, optional
        ``"parquet"`` or ``"csv"``. Default ``"parquet"``.
    exclude_dirs : list of str, optional
        Directory names to exclude from the search.
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
    print(f"Found {len(files)} spectra table(s).")

    tables: List[pd.DataFrame] = []
    for fpath in tqdm(files, desc="Loading spectra tables"):
        df = _load_and_validate_table(fpath, file_type, verbose=verbose)
        if df is not None:
            tables.append(df)
    return tables


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
                "(old schema? re-run QA00_SpectralValidation.py with --force).")
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
    table's source run can be located on disk, the per-run QA00 QC
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
        report = qc_dir / "QC_spectra_report.json"
        if report.is_file():
            reports_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(report, reports_dir / f"{run_stem}_{report.name}")
            counts["reports"] += 1
        for fig in sorted((qc_dir / "QC_plots").glob("*.png")):
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
        verbose: bool = False,
    ) -> pd.DataFrame:
    """Combine tables, snap wavelengths, and build run labels/residuals.

    Wavelengths are snapped onto a shared per-sensor/EM-region reference
    grid (see :func:`Code.functions.spectral_qc.snap_wavelengths`) so
    runs from different sensor units group onto the same axis. Run
    labels include only the metadata that differs across the frame
    (node/site are added only when more than one is present).

    Parameters
    ----------
    tables : list of pd.DataFrame
        Validated spectra tables.
    verbose : bool, optional
        Print snap diagnostics. Default False.

    Returns
    -------
    pd.DataFrame
        Long frame with ``refl_pct``, ``residual_pct`` (deviation from
        the cross-run mean spectrum at each snapped wavelength), snapped
        ``wavelength`` (+ ``raw_wavelength``), ``target_group`` (physical
        panel set where identified, filename otherwise) and ``run_label``
        columns.
    """
    # +++++ Normalise reflectance to percent per table (dtype-dependent) +++++
    frames = [t.assign(refl_pct=sq.reflectance_pct(t["value"])) for t in tables]
    df = pd.concat(frames, ignore_index=True)

    # ========== Snap wavelengths onto the shared reference grid ==========
    df = sq.snap_wavelengths(df, unit_col="node", verbose=verbose)

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
            "Panel_ref signature or pre-v2.2 table; re-run QA00 to migrate). "
            "They are grouped by filename instead.")

    # ========== Compact run labels: only include what differs ==========
    date_str = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y%m%d")
    date_str = date_str.fillna(df["date"].astype(str))
    label_cols = []
    if df["node"].nunique() > 1:
        label_cols.append(df["node"].astype(str))
    if df["site"].nunique() > 1:
        label_cols.append(df["site"].astype(str))
    label_cols.append(date_str)
    label_cols.append(df["run"].astype(str))
    if "gpro_nu" in df.columns and df["gpro_nu"].nunique() > 1:
        label_cols.append("g" + df["gpro_nu"].astype(str))
    df["run_label"] = label_cols[0].str.cat(label_cols[1:], sep=" ")

    # ========== Split duplicate same-set targets flown in one run ==========
    df = _split_duplicate_targets(df)

    # ========== Residual: deviation from the cross-run mean per wavelength ==========
    # Reference = mean of the per-run means at each snapped wavelength, so
    # every run carries equal weight regardless of pixel count.
    keys = ["sensor", "target_group", "EM_Region", "Panel_ref", "wavelength"]
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

    For every (sensor, target, EM region) two figures are produced —
    reflectance and residual vs the cross-run mean spectrum at each
    wavelength — faceted by panel with one line per run. Residual figures use a symlog y-axis so
    small systematic offsets stay readable next to artefact spikes.
    Known-bad wavelength ranges are masked to NaN (line gaps). A single
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

    # +++++ Mask known-bad wavelengths (NaN -> line gaps) +++++
    data = df.copy()
    if bad_wavelengths:
        for sensor, regions in bad_wavelengths.items():
            for region, ranges in regions.items():
                rows = (data["sensor"] == sensor) & (data["EM_Region"] == region)
                cut = rows & sq.bad_wavelength_mask(data["wavelength"], ranges)
                data.loc[cut, ["refl_pct", "residual_pct"]] = np.nan

    for (sensor, group, region), sub in data.groupby(
            ["sensor", "target_group", "EM_Region"]):
        for var, label in [("refl_pct", "Reflectance (%)"),
                           ("residual_pct", "Residual (% refl)")]:
            _make_comparison_figure(
                sub, str(sensor), str(group), str(region), var, label,
                palette=palette, plot_dir=plot_dir, show=show,
                errorbar=errorbar, copy_dir=copy_dir, verbose=verbose)


# ==================================================================================
def _make_comparison_figure(
        sub: pd.DataFrame,
        sensor: str,
        target: str,
        region: str,
        var: str,
        var_label: str,
        palette: Dict[str, Any],
        plot_dir: Optional[pathlib.Path],
        show: bool,
        errorbar: str,
        copy_dir: Optional[pathlib.Path] = None,
        verbose: bool = False,
    ) -> None:
    """Draw and save one faceted cross-run figure.

    Parameters
    ----------
    sub : pd.DataFrame
        Rows for one (sensor, target, EM region).
    sensor, target, region : str
        Labels for the title/filename.
    var : str
        Column plotted on the y-axis (residual columns get symlog).
    var_label : str
        Axis label for *var*.
    palette : dict
        Shared ``{run_label: colour}`` map.
    plot_dir : pathlib.Path or None
        Save directory; None = don't save.
    show : bool
        Display the figure interactively.
    errorbar : str
        ``"pi"``, ``"sd"`` or ``"none"``.
    copy_dir : pathlib.Path, optional
        Extra directory to also save the figure into (--save-dir
        container). Default None.
    verbose : bool, optional
        Print the output path. Default False.

    Returns
    -------
    None
    """
    is_residual = "residual" in var
    present = set(sub["run_label"].dropna().astype(str).unique())
    hue_order = [h for h in palette if h in present]
    print(f"Plotting {var_label} for sensor: {sensor}, target: {target}, region: {region}")
    g = sns.relplot(
        data=sub,
        x="wavelength", y=var,
        col="Panel_ref", col_wrap=2,
        hue="run_label", hue_order=hue_order, palette=palette,
        kind="line",
        errorbar=None if errorbar == "none" else errorbar,
    )
    g.set_xlabels("Wavelength (nm)")
    g.set_ylabels(var_label)
    if g.legend is not None:
        g.legend.set_frame_on(False)
        g.legend.set_title("Run")
        plt.setp(g.legend.get_texts(), fontfamily="monospace")
    g.figure.suptitle(
        f"Sensor: {sensor}, Target: {target}, EM range: {region}",
        y=0.98, fontweight="bold")
    g.figure.subplots_adjust(top=0.92)

    if is_residual:
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

    if plot_dir is not None or copy_dir is not None:
        parts = (cf.safe_filename_component(v)
                 for v in (sensor, target, region, var))
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
        "\nCross-run statistics (pairwise W1, drift) are not implemented yet; "
        "pending the ET00/ET03 equivalence test from APEx_SensorCalibration.")


# ==================================================================================
if __name__ == "__main__":
    # ========== Parse args ==========
    parser = argparse.ArgumentParser(description="Compare extracted panel spectra across runs (multi-run spectral QC).")
    parser.add_argument("--path", type=str, default=None, help="Node or project folder to crawl for extracted spectra tables. Defaults to the git repo root. Node folders save results to <Node>/Documents/QCReports/, project folders to <Project>/Documentation/QCReports/; any other level requires --output-dir.")
    parser.add_argument("--output-dir", type=str, default=None, help="Explicit output directory for figures (overrides the node/project routing; required for other path levels unless --no-save).")
    parser.add_argument("--no-save", default=False, action="store_true", help="Display the figures interactively instead of saving them. Nothing is written to disk.")
    parser.add_argument("--type", type=str, default="parquet", choices=["parquet", "csv"], help="File type of the extracted spectra tables. Default parquet.")
    parser.add_argument("--load-dir", type=str, default=None, help="Also load spectra tables from this folder, searched recursively (e.g. a --save-dir container received from another node).")
    parser.add_argument("--save-dir", type=str, default=None, help="Build a portable spectral-accuracy container in this directory: gathered tables (tables/), per-run QA00 reports (reports/) and figures (figures/), and this script's comparison figures (comparison_figures/).")
    parser.add_argument("--start-date", type=str, default=None, help="Only include runs on or after this date (inclusive; e.g. 2026-08-01 or 20260801).")
    parser.add_argument("--end-date", type=str, default=None, help="Only include runs on or before this date (inclusive).")
    parser.add_argument("--errorbar", type=str, default="pi", choices=["pi", "sd", "none"], help="Spread band around each run line: pi (percentile interval, default), sd (+/- one standard deviation), or none (mean lines only).")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[], help="Directory names to exclude from the table search.")
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
    os.chdir(path)

    # ========== Parse Args to main function ==========
    main(args, path)
