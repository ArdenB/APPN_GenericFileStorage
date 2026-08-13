"""Multi-run GCP accuracy comparison (QA03).

Gathers the per-run GCP distance tables and accuracy reports produced by
``QA01_PointDistanceComparison.py``
(``<run>/T1_proc/QC_data/QC_GCP[_{Product}]_distances[_{extra}].{csv|parquet}``
+ ``..._report.json``) across every run under the given path, optionally
appends artefacts received from other nodes (``--load-dir``), and produces
cross-run comparison outputs:

- a per run x product summary table (parquet + csv) with counts, 2D/3D
  RMSE, mean/median distances and the bias decomposition (magnitude,
  bearing, bias_fraction) — read from the QA01 report JSON where present
  and current, recomputed from the distance table otherwise;
- comparison figures: accuracy metrics per run over time, per-run 2D
  bias vectors, and per-GCP-id displacement trends across runs;
- a markdown overview report (``QC_GCP_run_comparison.md``) embedding
  the figures with relative paths so it renders in the VS Code / GitHub
  preview.

QA03 consumes QA01's saved artefacts **only** — it never re-opens the
geojson point layers or rasters (mirroring QA02's "never opens .bin"
rule). Run QA01 first to (re)generate per-run artefacts.

Where results are saved depends on the level of the path provided:

- node folder    -> ``<Node>/Documents/QCReports/``
- project folder -> ``<Project>/Documentation/QCReports/``
- anything else  -> ``--output-dir`` is required.

``--no-save`` displays the figures interactively instead of saving them.

Command-line Arguments
----------------------
--path : str, optional
    Node/project folder to crawl for QA01 distance tables. Defaults to
    the root directory of the git repository.
--output-dir : str, optional
    Where to save outputs. Required when --path is not a node or
    project folder (unless --no-save).
--no-save : flag
    Show figures interactively instead of saving them.
--load-dir : str, optional
    Also load distance tables from this folder (searched recursively,
    e.g. a container produced by --save-dir on another node).
--save-dir : str, optional
    Build a portable GCP-accuracy container in this directory: every
    gathered distance table (``tables/``), the per-run QA01 reports
    (``reports/``) and displacement figures (``figures/``), plus the
    comparison figures produced by this script
    (``comparison_figures/``).
--start-date : str, optional
    Only include runs on or after this date (e.g. 2026-08-01 or 20260801).
--end-date : str, optional
    Only include runs on or before this date.
--exclude-dir : str [str ...]
    Directory names to exclude from the search.
--force : flag
    Regenerate outputs even when they are newer than every input.
--verbose : flag
    Print extra diagnostic information.
"""

# ==============================================================================

__title__ = "GCP run comparison"
__author__ = "Arden Burrell"
__version__ = "v1.0(13.08.2026)"
__email__ = "arden.burrell@sydney.edu.au"

# ==============================================================================
# ========== Import core packages ==========
import os
import re
import sys
import json
import shutil
import argparse
import pathlib
from typing import Dict, List, Any, Optional

# ========== Import other packages ==========
import git
from git import exc as git_exc
import numpy as np
import pandas as pd
from tqdm import tqdm
import warnings as warn
import matplotlib.pyplot as plt
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
import Code.functions.gcp_qc as gq


# ==================================================================================
def main(
        args: argparse.Namespace,
        path: pathlib.Path,
    ) -> pd.DataFrame:
    """Run the multi-run GCP accuracy comparison pipeline.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments.
    path : pathlib.Path
        Node/project folder to crawl for QA01 distance tables.

    Returns
    -------
    pd.DataFrame
        End-of-run summary: one row per gathered artefact with columns
        ``project, sensor, date, run, product, n, rmse_2d_m, status,
        reason``.
    """
    # ========== Resolve where the outputs go ==========
    out_dir = cf.resolve_qcreports_dir(path, args.output_dir, args.no_save)

    # ========== Gather the per-run distance artefacts ==========
    entries = gather_distance_artefacts(
        path, exclude_dirs=args.exclude_dir, verbose=args.verbose)
    if args.load_dir is not None:
        entries.extend(load_external_artefacts(
            pathlib.Path(args.load_dir), verbose=args.verbose))
    entries = filter_entries_by_date(entries, args.start_date, args.end_date)
    usable = [e for e in entries if e.get("skip_reason") is None]
    if len(usable) == 0:
        raise ValueError(
            f"No usable QC_GCP distance tables found under {path}"
            + (f" or {args.load_dir}" if args.load_dir else "")
            + (" within the requested date window"
               if (args.start_date or args.end_date) else "")
            + ". Run QA01_PointDistanceComparison.py first to create them.")

    # ========== Save copies to --save-dir if provided ==========
    save_dir = pathlib.Path(args.save_dir) if args.save_dir is not None else None
    if save_dir is not None:
        save_artefact_copies(usable, save_dir)

    # ========== Skip work when the outputs are already up to date ==========
    if (not args.force and out_dir is not None and save_dir is None
            and _comparison_up_to_date(usable, out_dir)):
        print(f"Comparison outputs in {out_dir} are up to date "
              "(use --force to regenerate).")
        result = end_of_run_summary(entries)
        _print_end_of_run(result)
        return result

    # ========== Per run x product summary stats ==========
    summary = build_run_summary(usable, verbose=args.verbose)
    print(f"Prepared cross-run summary: {len(summary)} run-layer(s), "
          f"{summary['sensor'].nunique()} sensor(s).")
    if out_dir is not None:
        save_summary_tables(summary, out_dir)

    # ========== Comparison figures ==========
    distances = combine_distance_tables(usable)
    fig_paths = plot_comparisons(
        summary, distances, plot_dir=out_dir, show=args.no_save,
        copy_dir=(save_dir / "comparison_figures") if save_dir else None,
        verbose=args.verbose)

    # ========== Markdown overview report ==========
    if out_dir is not None:
        write_markdown_report(summary, entries, fig_paths, out_dir)

    # ========== End-of-run summary table ==========
    result = end_of_run_summary(entries)
    _print_end_of_run(result)
    if out_dir is not None:
        print(f"\nAll comparison outputs saved to: {out_dir}")
    else:
        print("\n*** NOTHING WAS SAVED (--no-save): figures were displayed only. ***")
    return result


# ==================================================================================
def _comparison_up_to_date(
        entries: List[Dict[str, Any]],
        out_dir: pathlib.Path,
    ) -> bool:
    """Check the stable-name outputs against every gathered input.

    Inputs are the distance tables plus any sibling report JSONs;
    outputs are the summary tables and the markdown report (figures
    share the report's regeneration cycle, so they are not checked
    individually).

    Parameters
    ----------
    entries : list of dict
        Usable artefact entries.
    out_dir : pathlib.Path
        Routed QCReports directory.

    Returns
    -------
    bool
        True when nothing needs regenerating.
    """
    outputs = [out_dir / "QC_GCP_run_comparison.parquet",
               out_dir / "QC_GCP_run_comparison.csv",
               out_dir / "QC_GCP_run_comparison.md"]
    inputs = [e["table_path"] for e in entries]
    inputs += [e["report_path"] for e in entries
               if e.get("report_path") is not None]
    return cf.outputs_up_to_date(outputs, inputs)


# ==================================================================================
def gather_distance_artefacts(
        path: pathlib.Path,
        exclude_dirs: Optional[List[str]] = None,
        verbose: bool = False,
    ) -> List[Dict[str, Any]]:
    """Find every QA01 distance table (+ sibling report) under *path*.

    Searches for ``QC_GCP*_distances*.{parquet,csv}`` files inside
    ``T1_proc/QC_data`` folders (the QA01 output convention). When both
    a parquet and a csv exist for the same stem, the parquet wins.

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively.
    exclude_dirs : list of str, optional
        Directory names to exclude from the search.
    verbose : bool, optional
        Print per-file diagnostics. Default False.

    Returns
    -------
    list of dict
        One entry per distance table (see :func:`_load_entry` for the
        keys). Entries that fail validation carry a ``skip_reason``.
    """
    print(f"Scanning {path} for QC_GCP distance tables. {pd.Timestamp.now()}")
    exclude_set = set(exclude_dirs or [])

    def _excluded(p: pathlib.Path) -> bool:
        return bool(exclude_set
                    and (set(par.name for par in p.parents) & exclude_set))

    candidates: Dict[pathlib.Path, pathlib.Path] = {}
    for ext in ("parquet", "csv"):
        for f in path.rglob(f"QC_GCP*_distances*.{ext}"):
            if _excluded(f) or f.parent.name != "QC_data":
                continue
            key = f.with_suffix("")
            # +++++ parquet listed first, so csv never overwrites it +++++
            candidates.setdefault(key, f)
    files = sorted(candidates.values())
    print(f"Found {len(files)} distance table(s).")

    entries = []
    for fpath in tqdm(files, desc="Loading distance tables"):
        entries.append(_load_entry(fpath, run_dir=fpath.parents[2],
                                   verbose=verbose))
    return entries


# ==================================================================================
def _load_entry(
        fpath: pathlib.Path,
        run_dir: Optional[pathlib.Path],
        verbose: bool = False,
    ) -> Dict[str, Any]:
    """Load one distance table plus its sibling report JSON.

    Parameters
    ----------
    fpath : pathlib.Path
        Distance table path (``QC_GCP*_distances*.{parquet,csv}``).
    run_dir : pathlib.Path or None
        The ``<run>`` directory (``None`` for external artefacts whose
        metadata is parsed from the copy filename instead).
    verbose : bool, optional
        Print the reason when a file is problematic. Default False.

    Returns
    -------
    dict
        Keys: ``table_path``, ``report_path`` (or None), ``df``
        (or None), ``report`` (dict or None), ``product``, ``extra``,
        and the run metadata (``node, project, site, sensor, date,
        run``). Entries that cannot be used carry ``skip_reason``.
    """
    stem_info = parse_distance_stem(fpath.stem)
    entry: Dict[str, Any] = {
        "table_path": fpath,
        "report_path": None,
        "df": None,
        "report": None,
        "skip_reason": None,
        **stem_info,
        "node": None, "project": None, "site": None,
        "sensor": None, "date": None, "run": None,
    }
    if stem_info.get("bad_stem"):
        entry["skip_reason"] = f"unrecognised filename stem '{fpath.stem}'"
        return entry

    # +++++ Run metadata from the APPN path +++++
    if run_dir is not None:
        parsed = cf.parse_APPN_dataset_path(run_dir)
        for key in ("node", "project", "site", "sensor", "run"):
            entry[key] = parsed.get(key)
        entry["date"] = parsed.get("date")

    # +++++ Distance table +++++
    required = {"id", "delta_easting_m", "delta_northing_m",
                "distance_2d_m", "distance_3d_m", "delta_height_m"}
    try:
        if fpath.suffix == ".csv":
            df = pd.read_csv(fpath)
        else:
            df = pd.read_parquet(fpath)
    except Exception as er:
        entry["skip_reason"] = f"could not read table: {er}"
        return entry
    missing = required - set(df.columns)
    if missing:
        entry["skip_reason"] = (
            f"missing columns {sorted(missing)} "
            "(old schema? re-run QA01_PointDistanceComparison.py)")
        if verbose:
            tqdm.write(f"Skipping {fpath.name}: {entry['skip_reason']}")
        return entry
    if df.empty:
        entry["skip_reason"] = "no rows"
        return entry
    df["id"] = df["id"].astype(str)
    entry["df"] = df

    # +++++ Sibling report JSON (optional; stats recomputed if absent) +++++
    report_path = fpath.with_name(f"{fpath.stem}_report.json")
    if report_path.is_file():
        entry["report_path"] = report_path
        try:
            with report_path.open("r", encoding="utf-8") as fh:
                entry["report"] = json.load(fh)
        except (OSError, json.JSONDecodeError) as er:
            warn.warn(f"Could not read {report_path}: {er}. "
                      "Stats will be recomputed from the distance table.")
    elif verbose:
        tqdm.write(f"No report JSON for {fpath.name}; recomputing stats.")
    return entry


# ==================================================================================
def parse_distance_stem(stem: str) -> Dict[str, Any]:
    """Split a QA01 distance-table stem into product / extra parts.

    ``QC_GCP_distances`` -> base layer; ``QC_GCP_VNIR_distances`` ->
    product ``VNIR``; trailing ``_{extra}`` after ``distances`` is the
    extra-info suffix (``QC_GCP_distances_20260805``). Copies made by
    ``--save-dir`` carry a metadata prefix which is tolerated.

    Parameters
    ----------
    stem : str
        Filename stem (no suffix).

    Returns
    -------
    dict
        ``product`` (str or None), ``extra`` (str or None),
        ``bad_stem`` (bool).
    """
    m = re.search(r"QC_GCP_(?:(.+)_)?distances(?:_(.+))?$", stem)
    if m is None or (m.group(2) or "") == "report":
        return {"product": None, "extra": None, "bad_stem": True}
    return {"product": m.group(1), "extra": m.group(2), "bad_stem": False}


# ==================================================================================
def load_external_artefacts(
        load_dir: pathlib.Path,
        verbose: bool = False,
    ) -> List[Dict[str, Any]]:
    """Load distance tables received from other nodes/collaborators.

    The directory is searched recursively, so it can point either at a
    flat folder of tables or at the root of a container produced by
    ``--save-dir`` (whose tables live under ``tables/``). Run metadata
    comes from the container's ``manifest.csv`` where present, falling
    back to the copy-filename prefix
    (``{node}_{project}_{site}_{sensor}_{date}_{run}_QC_GCP...``).

    Parameters
    ----------
    load_dir : pathlib.Path
        Directory containing the external artefacts.
    verbose : bool, optional
        Print per-file diagnostics. Default False.

    Returns
    -------
    list of dict
        Entries in the :func:`_load_entry` shape.

    Raises
    ------
    NotADirectoryError
        If *load_dir* does not exist or is not a directory.
    """
    if not load_dir.is_dir():
        raise NotADirectoryError(
            f"The --load-dir path does not exist or is not a directory: {load_dir}")
    files = sorted(
        f for ext in ("parquet", "csv")
        for f in load_dir.rglob(f"*QC_GCP*distances*.{ext}"))
    if len(files) == 0:
        warn.warn(f"No QC_GCP distance tables found in --load-dir {load_dir}.")
        return []
    manifest = _load_manifests(load_dir)
    entries: List[Dict[str, Any]] = []
    for fpath in tqdm(files, desc="Loading external tables"):
        entry = _load_entry(fpath, run_dir=None, verbose=verbose)
        meta = manifest.get(fpath.name)
        if meta is None:
            meta = _metadata_from_copy_name(fpath.stem)
            if not meta and verbose:
                tqdm.write(
                    f"  no manifest entry or parseable prefix for "
                    f"{fpath.name}; run metadata left empty.")
        entry.update(meta)
        entries.append(entry)
    n_ok = sum(1 for e in entries if e.get("skip_reason") is None)
    print(f"Loaded {n_ok} external table(s) from {load_dir}"
          + (f" ({len(entries) - n_ok} skipped)" if len(entries) != n_ok else ""))
    return entries


# ==================================================================================
def _load_manifests(load_dir: pathlib.Path) -> Dict[str, Dict[str, Any]]:
    """Read every container ``manifest.csv`` under *load_dir*.

    Parameters
    ----------
    load_dir : pathlib.Path
        External container root.

    Returns
    -------
    dict of str to dict
        ``{table filename: metadata row}`` with ``date`` parsed to a
        Timestamp and ``run`` to int where possible.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for mpath in sorted(load_dir.rglob("manifest.csv")):
        try:
            mdf = pd.read_csv(mpath, dtype={"date": str})
        except (OSError, pd.errors.ParserError) as er:
            warn.warn(f"Could not read {mpath}: {er}. Ignoring it.")
            continue
        if "filename" not in mdf.columns:
            continue
        for row in mdf.to_dict("records"):
            meta = {k: row.get(k) for k in
                    ("node", "project", "site", "sensor", "run")}
            meta["date"] = pd.to_datetime(row.get("date"), format="%Y%m%d",
                                          errors="coerce")
            out[str(row["filename"])] = meta
    return out


# ==================================================================================
def _metadata_from_copy_name(stem: str) -> Dict[str, Any]:
    """Recover run metadata from a --save-dir copy filename (fallback).

    Copies are named
    ``{node}_{project}_{site}_{sensor}_{date}_{run}_QC_GCP...`` (see
    :func:`_copy_stem`). The pattern is anchored on the 8-digit date
    and assumes project = ``YYYY_token`` and single-token site/sensor,
    so node names containing underscores still parse. Only the
    metadata keys that parse cleanly are returned; the container
    ``manifest.csv`` is the authoritative source.

    Parameters
    ----------
    stem : str
        Copy filename stem.

    Returns
    -------
    dict
        Subset of ``node, project, site, sensor, date, run``.
    """
    m = re.match(
        r"(?P<node>.+)_(?P<project>\d{4}_[^_]+)_(?P<site>[^_]+)_"
        r"(?P<sensor>[^_]+)_(?P<date>\d{8})_run[_ ]?(?P<run>\d+)_QC_GCP",
        stem)
    if m is None:
        return {}
    out: Dict[str, Any] = dict(m.groupdict())
    out["date"] = pd.to_datetime(out["date"], format="%Y%m%d")
    out["run"] = int(out["run"])
    return out


# ==================================================================================
def filter_entries_by_date(
        entries: List[Dict[str, Any]],
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> List[Dict[str, Any]]:
    """Keep only the entries whose run date falls inside the window.

    Entries without a parseable date are kept with a warning.

    Parameters
    ----------
    entries : list of dict
        Artefact entries.
    start_date, end_date : str or None
        Inclusive bounds in any ``pd.to_datetime``-parseable form.
        None = unbounded.

    Returns
    -------
    list of dict
        The entries within the window.

    Raises
    ------
    ValueError
        If a bound cannot be parsed or start_date > end_date.
    """
    if start_date is None and end_date is None:
        return entries
    bounds = {}
    for name, val in [("start-date", start_date), ("end-date", end_date)]:
        if val is not None:
            bounds[name] = pd.to_datetime(val, errors="coerce")
            if pd.isna(bounds[name]):
                raise ValueError(f"Could not parse --{name} '{val}'.")
    start = bounds.get("start-date")
    end = bounds.get("end-date")
    if start is not None and end is not None and start > end:
        raise ValueError(
            f"--start-date {start.date()} is after --end-date {end.date()}.")

    kept: List[Dict[str, Any]] = []
    for e in entries:
        run_date = pd.to_datetime(e.get("date"), errors="coerce")
        if pd.isna(run_date):
            warn.warn(
                f"Could not parse run date for {e['table_path'].name}; "
                "keeping the entry.")
            kept.append(e)
            continue
        if ((start is None or run_date >= start)
                and (end is None or run_date <= end)):
            kept.append(e)
    print(f"Date filter [{start_date or '...'} to {end_date or '...'}]: "
          f"kept {len(kept)} of {len(entries)} entrie(s).")
    return kept


# ==================================================================================
def save_artefact_copies(
        entries: List[Dict[str, Any]],
        save_dir: pathlib.Path,
    ) -> None:
    """Build a portable GCP-accuracy container in *save_dir*.

    Copies every gathered distance table into ``tables/``, the sibling
    QA01 report JSONs into ``reports/`` and the per-run displacement
    figures (``QC_plots/QC_GCP*_displacements.png``) into ``figures/``.
    Filenames get a run-metadata prefix so files from different nodes,
    projects, sensors, and dates stay uniquely identifiable, and a
    ``manifest.csv`` mapping each table copy back to its run metadata
    is written into the container root (``--load-dir`` reads it, so
    metadata survives underscores in node/project names).

    Parameters
    ----------
    entries : list of dict
        Usable artefact entries.
    save_dir : pathlib.Path
        Container root. Created (with parents) if missing.

    Returns
    -------
    None
    """
    tables_dir = save_dir / "tables"
    reports_dir = save_dir / "reports"
    figures_dir = save_dir / "figures"
    tables_dir.mkdir(parents=True, exist_ok=True)
    counts = {"tables": 0, "reports": 0, "figures": 0}
    manifest_rows: List[Dict[str, Any]] = []
    done_runs: set = set()
    for e in entries:
        stem = _copy_stem(e)
        shutil.copy2(e["table_path"],
                     tables_dir / f"{stem}{e['table_path'].suffix}")
        counts["tables"] += 1
        date = pd.to_datetime(e.get("date"), errors="coerce")
        manifest_rows.append({
            "filename": f"{stem}{e['table_path'].suffix}",
            "node": e.get("node"), "project": e.get("project"),
            "site": e.get("site"), "sensor": e.get("sensor"),
            "date": date.strftime("%Y%m%d") if pd.notna(date) else None,
            "run": e.get("run"),
        })
        if e.get("report_path") is not None:
            reports_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(e["report_path"],
                         reports_dir / f"{stem}_report.json")
            counts["reports"] += 1
        # +++++ Per-run displacement figures (once per QC_data dir) +++++
        qc_dir = e["table_path"].parent
        if qc_dir in done_runs:
            continue
        done_runs.add(qc_dir)
        run_prefix = _copy_stem(e, run_level=True)
        for fig in sorted((qc_dir / "QC_plots").glob(
                "QC_GCP*_displacements.png")):
            figures_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fig, figures_dir / f"{run_prefix}_{fig.name}")
            counts["figures"] += 1
    pd.DataFrame(manifest_rows).to_csv(
        (save_dir / "manifest.csv").as_posix(), index=False)
    print(f"Container built at {save_dir}: {counts['tables']} table(s), "
          f"{counts['reports']} report(s), {counts['figures']} run figure(s).")


# ==================================================================================
def _copy_stem(entry: Dict[str, Any], run_level: bool = False) -> str:
    """Build the unique filename stem for one artefact copy.

    Parameters
    ----------
    entry : dict
        Artefact entry (carries the run metadata).
    run_level : bool, optional
        When True return only the run-metadata prefix (no table stem),
        used for the per-run figure copies. Default False.

    Returns
    -------
    str
        Underscore-joined metadata stem.
    """
    parts = []
    for col in ["node", "project", "site", "sensor", "date", "run"]:
        val = entry.get(col)
        if val is None or (isinstance(val, float) and pd.isna(val)):
            continue
        if hasattr(val, "strftime"):
            val = val.strftime("%Y%m%d")
        if col == "run":
            val = f"run_{int(val):02d}" if str(val).isdigit() else str(val)
        parts.append(str(val))
    if not run_level:
        parts.append(entry["table_path"].stem)
    return "_".join(parts)


# ==================================================================================
def build_run_summary(
        entries: List[Dict[str, Any]],
        verbose: bool = False,
    ) -> pd.DataFrame:
    """Build the per run x product cross-run summary table.

    Statistics come from the QA01 report JSON where present; when the
    report is missing, unreadable, or older than the distance table,
    they are recomputed from the table via the shared
    :mod:`Code.functions.gcp_qc` helpers (identical maths).

    Parameters
    ----------
    entries : list of dict
        Usable artefact entries.
    verbose : bool, optional
        Print per-entry diagnostics. Default False.

    Returns
    -------
    pd.DataFrame
        One row per run x product layer with the run metadata,
        ``run_label``, ``product_label`` and the accuracy metrics
        (counts, 2D/3D distance stats, bias decomposition, unmatched
        IDs, pass/fail status).
    """
    rows = []
    for e in entries:
        stats = _entry_stats(e, verbose=verbose)
        rows.append({
            "node": e.get("node"), "project": e.get("project"),
            "site": e.get("site"), "sensor": e.get("sensor"),
            "date": pd.to_datetime(e.get("date"), errors="coerce"),
            "run": e.get("run"),
            "product": e.get("product"), "extra": e.get("extra"),
            **stats,
        })
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["product_label"] = _product_labels(df)
    df["run_label"] = _run_labels(df)
    return df.sort_values(
        ["sensor", "product_label", "date", "run"]).reset_index(drop=True)


# ==================================================================================
def _entry_stats(
        entry: Dict[str, Any],
        verbose: bool = False,
    ) -> Dict[str, Any]:
    """Extract (or recompute) the accuracy metrics for one entry.

    Parameters
    ----------
    entry : dict
        Usable artefact entry.
    verbose : bool, optional
        Report when stats are recomputed. Default False.

    Returns
    -------
    dict
        Flat metric columns: ``n_matched, n_only_groundtruth,
        n_only_qc, mean_2d_m, median_2d_m, max_2d_m, rmse_2d_m,
        mean_3d_m, rmse_3d_m, mean_dz_m, bias_magnitude_m,
        bias_bearing_deg, bias_fraction, bias_class, status,
        stats_source``.
    """
    report = entry.get("report")
    # A usable report must carry the per-pair stats payload (foreign
    # schema variants, e.g. run-level aggregates, force a recompute)
    # and be at least as new as the distance table.
    has_stats = (report is not None
                 and isinstance(report.get("statistics_metres"), dict)
                 and "distance_2d" in report["statistics_metres"])
    fresh = (has_stats
             and entry.get("report_path") is not None
             and entry["report_path"].stat().st_mtime
             >= entry["table_path"].stat().st_mtime)
    if fresh:
        assert report is not None
        stats = report.get("statistics_metres", {})
        bias = report.get("bias", {})
        counts = report.get("counts", {})
        status = report.get("status", {}).get("result")
        source = "report"
    else:
        df = entry["df"]
        stats = {
            "distance_2d": gq.distance_stats(df["distance_2d_m"]),
            "distance_3d": gq.distance_stats(df["distance_3d_m"]),
            "delta_height": gq.distance_stats(df["delta_height_m"]),
        }
        bias = gq.bias_analysis(df)
        counts = {"matched": int(len(df)),
                  "only_in_a": None, "only_in_b": None}
        status = None
        source = "recomputed"
        if verbose:
            tqdm.write(
                f"  {entry['table_path'].name}: report missing/stale, "
                "stats recomputed from the distance table.")

    d2d = stats.get("distance_2d", {})
    d3d = stats.get("distance_3d", {})
    dz = stats.get("delta_height", {})
    planar = bias.get("planar_2d", {})
    return {
        "n_matched": counts.get("matched"),
        "n_only_groundtruth": counts.get("only_in_a"),
        "n_only_qc": counts.get("only_in_b"),
        "mean_2d_m": d2d.get("mean"),
        "median_2d_m": d2d.get("median"),
        "max_2d_m": d2d.get("max"),
        "rmse_2d_m": d2d.get("rmse"),
        "mean_3d_m": d3d.get("mean"),
        "rmse_3d_m": d3d.get("rmse"),
        "mean_dz_m": dz.get("mean"),
        "bias_magnitude_m": planar.get("bias_magnitude_m"),
        "bias_bearing_deg": planar.get("bias_bearing_deg"),
        "bias_fraction": planar.get("bias_fraction"),
        "bias_class": planar.get("classification"),
        "status": status,
        "stats_source": source,
    }


# ==================================================================================
def _product_labels(df: pd.DataFrame) -> pd.Series:
    """Comparison-group label for each row: product layer + extra suffix.

    The base single-layer runs (``QC_GCP_points``) are labelled
    ``all products``; per-product layers keep the product identifier.
    An ``_extra`` filename suffix stays part of the label so duplicate
    layers in one run plot as distinct lines (QA02
    ``_split_duplicate_targets`` analogue).

    Parameters
    ----------
    df : pd.DataFrame
        Summary frame with ``product`` and ``extra`` columns.

    Returns
    -------
    pd.Series
        Product labels.
    """
    base = df["product"].fillna("all products").astype(str)
    extra = df["extra"]
    return base.where(extra.isna(), base + " (" + extra.astype(str) + ")")


# ==================================================================================
def _run_labels(df: pd.DataFrame) -> pd.Series:
    """Compact run labels: only include the metadata that differs.

    Parameters
    ----------
    df : pd.DataFrame
        Summary frame with node/site/date/run columns.

    Returns
    -------
    pd.Series
        Run labels (``[node ][site ]YYYYMMDD run_NN``).
    """
    dates = pd.to_datetime(df["date"], errors="coerce")
    date_str = dates.dt.strftime("%Y%m%d").fillna("nodate")

    def _fmt_run(r: Any) -> str:
        if r is None or (isinstance(r, float) and pd.isna(r)):
            return "run_?"
        try:
            return f"run_{int(r):02d}"
        except (ValueError, TypeError):
            return str(r)

    run_str = df["run"].map(_fmt_run)
    label_cols = []
    if df["node"].nunique(dropna=True) > 1:
        label_cols.append(df["node"].astype(str))
    if df["site"].nunique(dropna=True) > 1:
        label_cols.append(df["site"].astype(str))
    label_cols.extend([date_str, run_str])
    return label_cols[0].str.cat(label_cols[1:], sep=" ")


# ==================================================================================
def combine_distance_tables(entries: List[Dict[str, Any]]) -> pd.DataFrame:
    """Concatenate the per-run distance tables into one long frame.

    Parameters
    ----------
    entries : list of dict
        Usable artefact entries.

    Returns
    -------
    pd.DataFrame
        All matched-point rows with the run metadata, ``product_label``
        and ``run_label`` columns attached (labels consistent with
        :func:`build_run_summary`).
    """
    frames = []
    for e in entries:
        df = e["df"].copy()
        for key in ("node", "project", "site", "sensor", "run", "product",
                    "extra"):
            df[key] = e.get(key)
        df["date"] = pd.to_datetime(e.get("date"), errors="coerce")
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    out["product_label"] = _product_labels(out)
    out["run_label"] = _run_labels(out)
    return out


# ==================================================================================
def save_summary_tables(summary: pd.DataFrame, out_dir: pathlib.Path) -> None:
    """Write the cross-run summary table as parquet + csv.

    Parameters
    ----------
    summary : pd.DataFrame
        Frame from :func:`build_run_summary`.
    out_dir : pathlib.Path
        Routed QCReports directory.

    Returns
    -------
    None
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("parquet", "csv"):
        outpath = out_dir / f"QC_GCP_run_comparison.{ext}"
        if ext == "parquet":
            summary.to_parquet(outpath.as_posix(), index=False)
        else:
            summary.to_csv(outpath.as_posix(), index=False)
        print(f"Wrote {outpath}")


# ==================================================================================
def plot_comparisons(
        summary: pd.DataFrame,
        distances: pd.DataFrame,
        plot_dir: Optional[pathlib.Path],
        show: bool = False,
        copy_dir: Optional[pathlib.Path] = None,
        verbose: bool = False,
    ) -> Dict[str, pathlib.Path]:
    """Draw the cross-run comparison figures.

    Per sensor: (1) accuracy metrics per run over date, one line per
    product layer; (2) per-run 2D bias vectors on a polar axis; (3)
    per-GCP-id displacement trend across runs, faceted by product
    layer. A single run palette (:func:`cf.resolve_run_palette` tiers)
    is shared so a run keeps its colour everywhere.

    Parameters
    ----------
    summary : pd.DataFrame
        Frame from :func:`build_run_summary`.
    distances : pd.DataFrame
        Frame from :func:`combine_distance_tables`.
    plot_dir : pathlib.Path or None
        Directory to save figures; None saves nothing.
    show : bool, optional
        Display each figure interactively. Default False.
    copy_dir : pathlib.Path, optional
        Also save each figure into this directory (the --save-dir
        container's ``comparison_figures/``). Default None.
    verbose : bool, optional
        Print per-figure diagnostics. Default False.

    Returns
    -------
    dict of str to pathlib.Path
        ``{figure key: saved path}`` for the markdown report (empty in
        ``--no-save`` mode).
    """
    sns.set_style("whitegrid")
    plt.rcParams.update({
        "font.size": 11,
        "axes.titleweight": "bold",
        "axes.labelweight": "bold",
        "figure.titleweight": "bold",
    })
    palette = cf.resolve_run_palette(list(summary["run_label"].unique()))
    fig_paths: Dict[str, pathlib.Path] = {}
    for sensor, sub in summary.groupby("sensor", dropna=False):
        s_label = str(sensor)
        s_dist = distances[distances["sensor"] == sensor]
        makers = [
            ("metrics", _plot_metric_trends, sub),
            ("bias_vectors", _plot_bias_vectors, sub),
            ("per_gcp", _plot_per_gcp_trends, s_dist),
        ]
        for key, maker, data in makers:
            if data.empty:
                continue
            fig = maker(data, s_label, palette)
            saved = _save_figure(
                fig, f"QC_GCP_{cf.safe_filename_component(s_label)}_{key}",
                plot_dir, copy_dir, show, verbose)
            if saved is not None:
                fig_paths[f"{s_label}|{key}"] = saved
    return fig_paths


# ==================================================================================
def _save_figure(
        fig: plt.Figure,
        stem: str,
        plot_dir: Optional[pathlib.Path],
        copy_dir: Optional[pathlib.Path],
        show: bool,
        verbose: bool,
    ) -> Optional[pathlib.Path]:
    """Save (and optionally show) one figure; return the primary path.

    Parameters
    ----------
    fig : matplotlib.figure.Figure
        Figure to save.
    stem : str
        Preview-safe filename stem (no suffix).
    plot_dir, copy_dir : pathlib.Path or None
        Primary and container destinations.
    show : bool
        Display the figure interactively.
    verbose : bool
        Print the output path.

    Returns
    -------
    pathlib.Path or None
        The path saved under *plot_dir* (None in --no-save mode).
    """
    saved = None
    for dest in (plot_dir, copy_dir):
        if dest is None:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        outpath = dest / f"{stem}.png"
        fig.savefig(outpath.as_posix(), dpi=150, bbox_inches="tight")
        if verbose:
            print(f"  saved {outpath}")
        if dest is plot_dir:
            saved = outpath
    if show:
        plt.show()
    plt.close(fig)
    return saved


# ==================================================================================
def _plot_metric_trends(
        sub: pd.DataFrame,
        sensor: str,
        palette: Dict[str, Any],
    ) -> plt.Figure:
    """Accuracy metrics per run, one line per product layer.

    Two-row facet grid: RMSE (2D + 3D) on the top row, median 2D
    distance and bias magnitude on the bottom row. The x-axis is the
    ordered run label (date + run number), so same-day multi-run
    flights stay readable.

    Parameters
    ----------
    sub : pd.DataFrame
        Summary rows for one sensor.
    sensor : str
        Sensor label for the title.
    palette : dict
        Shared ``{run_label: colour}`` map (kept for signature
        uniformity; lines are coloured per product layer).

    Returns
    -------
    matplotlib.figure.Figure
    """
    metrics = [("rmse_2d_m", "RMSE 2D (m)"), ("rmse_3d_m", "RMSE 3D (m)"),
               ("median_2d_m", "Median 2D (m)"),
               ("bias_magnitude_m", "Bias magnitude (m)")]
    run_order = sorted(sub["run_label"].unique(), key=cf.run_sort_key)
    run_idx = {label: i for i, label in enumerate(run_order)}
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharex=True)
    product_palette = dict(zip(
        sorted(sub["product_label"].unique()),
        sns.color_palette("colorblind",
                          sub["product_label"].nunique())))
    for ax, (col, label) in zip(axes.flat, metrics):
        if sub[col].notna().sum() == 0:
            ax.text(0.5, 0.5, f"no {label} data", ha="center", va="center",
                    transform=ax.transAxes, color="0.5")
            ax.set_ylabel(label)
            continue
        for product, grp in sub.groupby("product_label"):
            grp = grp.assign(_x=grp["run_label"].map(run_idx)).sort_values("_x")
            ax.plot(grp["_x"], grp[col], marker="o", ms=5, lw=1.2,
                    color=product_palette[product], label=str(product))
        ax.set_ylabel(label)
        ax.grid(True, which="major", linestyle="--", linewidth=0.5, color="0.6")
    for ax in axes[-1]:
        # +++++ Thin the tick labels on many-run days +++++
        step = max(1, int(np.ceil(len(run_order) / 25)))
        ax.set_xticks(range(0, len(run_order), step))
        ax.set_xticklabels(run_order[::step], rotation=45, ha="right",
                           fontsize=8)
        ax.set_xlabel("Run")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    if not handles:
        for ax in axes.flat:
            handles, labels = ax.get_legend_handles_labels()
            if handles:
                break
    fig.legend(handles, labels, title="Product layer",
               loc="upper left", bbox_to_anchor=(1.005, 0.95), frameon=False)
    fig.suptitle(f"Sensor: {sensor} - GCP accuracy per run", y=0.99)
    fig.tight_layout()
    return fig


# ==================================================================================
def _plot_bias_vectors(
        sub: pd.DataFrame,
        sensor: str,
        palette: Dict[str, Any],
    ) -> plt.Figure:
    """Per-run 2D bias vectors on a polar axis (drift check).

    Each arrow points along the run's mean offset bearing with length
    equal to the bias magnitude; a tight cluster = a stable systematic
    offset, scattered directions = random per-run behaviour. One panel
    per product layer.

    Parameters
    ----------
    sub : pd.DataFrame
        Summary rows for one sensor.
    sensor : str
        Sensor label for the title.
    palette : dict
        Shared ``{run_label: colour}`` map.

    Returns
    -------
    matplotlib.figure.Figure
    """
    products = sorted(sub["product_label"].unique())
    max_mag = float(pd.to_numeric(sub["bias_magnitude_m"],
                                  errors="coerce").max())
    rmax = max_mag * 1.25 if np.isfinite(max_mag) and max_mag > 0 else 0.05
    ncol = min(len(products), 2)
    nrow = int(np.ceil(len(products) / ncol))
    fig, axes = plt.subplots(
        nrow, ncol, figsize=(6.5 * ncol, 6 * nrow),
        subplot_kw={"projection": "polar"}, squeeze=False)
    for ax, product in zip(axes.flat, products):
        grp = sub[sub["product_label"] == product]
        for _, row in grp.iterrows():
            mag = row["bias_magnitude_m"]
            bearing = row["bias_bearing_deg"]
            if pd.isna(mag) or pd.isna(bearing):
                continue
            theta = np.deg2rad(bearing)
            colour = palette.get(row["run_label"], "0.3")
            ax.annotate(
                "", xy=(theta, mag), xytext=(0, 0),
                arrowprops=dict(arrowstyle="->", color=colour, lw=1.8))
            ax.plot([], [], color=colour, lw=1.8, label=row["run_label"])
        # +++++ Compass convention: 0 deg = grid north, clockwise +++++
        ax.set_theta_zero_location("N")
        ax.set_theta_direction(-1)
        # annotate() does not autoscale, so set the radial limit by hand
        ax.set_rmax(rmax)
        ax.set_title(f"{product}", pad=18)
        ax.legend(loc="upper left", bbox_to_anchor=(1.08, 1.0),
                  fontsize=8, frameon=False)
    for ax in axes.flat[len(products):]:
        ax.set_visible(False)
    fig.suptitle(
        f"Sensor: {sensor} - per-run 2D bias vectors "
        "(bearing clockwise from grid north, radius = metres)", y=1.0)
    fig.tight_layout()
    return fig


# ==================================================================================
def _plot_per_gcp_trends(
        dist: pd.DataFrame,
        sensor: str,
        palette: Dict[str, Any],
    ) -> plt.Figure:
    """Per-GCP-id 2D displacement across runs, faceted by product layer.

    A single moved marker shows as one line jumping while the others
    stay flat; a whole-flight georeferencing shift moves every line
    together.

    Parameters
    ----------
    dist : pd.DataFrame
        Combined distance rows for one sensor.
    sensor : str
        Sensor label for the title.
    palette : dict
        Shared ``{run_label: colour}`` map (unused here; ids get their
        own palette).

    Returns
    -------
    matplotlib.figure.Figure
    """
    run_order = sorted(dist["run_label"].unique(), key=cf.run_sort_key)
    dist = dist.assign(
        _run_idx=dist["run_label"].map({l: i for i, l in enumerate(run_order)}))
    g = sns.relplot(
        data=dist, x="_run_idx", y="distance_2d_m",
        hue="id", style="id",
        col="product_label", col_wrap=min(dist["product_label"].nunique(), 2),
        kind="line", marker="o", ms=6,
        height=5, aspect=max(1.2, len(run_order) / 16),
        facet_kws={"sharey": True},
        palette=cf.resolve_run_palette(list(dist["id"].unique())),
    )
    g.set_xlabels("Run")
    g.set_ylabels("2D distance (m)")
    # +++++ Thin the tick labels on many-run days +++++
    step = max(1, int(np.ceil(len(run_order) / 25)))
    for ax in g.axes.flat:
        ax.set_xticks(range(0, len(run_order), step))
        ax.set_xticklabels(run_order[::step], rotation=45, ha="right",
                           fontsize=8)
        ax.grid(True, which="major", linestyle="--", linewidth=0.5, color="0.6")
    if g.legend is not None:
        g.legend.set_title("GCP id")
        g.legend.set_frame_on(False)
    g.figure.suptitle(
        f"Sensor: {sensor} - per-GCP 2D displacement across runs", y=1.02)
    return g.figure


# ==================================================================================
def write_markdown_report(
        summary: pd.DataFrame,
        entries: List[Dict[str, Any]],
        fig_paths: Dict[str, pathlib.Path],
        out_dir: pathlib.Path,
    ) -> None:
    """Write the markdown overview report into the routed QCReports dir.

    Contains the per run x product headline table, worst-run callouts,
    unmatched-ID counts, the skipped-artefact list and relative-path
    embeds of every comparison figure (no ``%`` in any figure name, so
    the VS Code preview renders them).

    Parameters
    ----------
    summary : pd.DataFrame
        Frame from :func:`build_run_summary`.
    entries : list of dict
        All artefact entries (usable + skipped).
    fig_paths : dict
        ``{('sensor|key'): path}`` map from :func:`plot_comparisons`.
    out_dir : pathlib.Path
        Routed QCReports directory.

    Returns
    -------
    None
    """
    lines: List[str] = []
    lines.append("# GCP run comparison (QA03)")
    lines.append("")
    lines.append(f"Generated by `{__title__}` {__version__} on "
                 f"{pd.Timestamp.now():%Y-%m-%d %H:%M}. Inputs are the "
                 "per-run artefacts written by "
                 "`QA01_PointDistanceComparison.py`.")
    lines.append("")

    # ========== Headline table per sensor ==========
    display_cols = [
        ("run_label", "Run"), ("product_label", "Product"),
        ("n_matched", "n"), ("rmse_2d_m", "RMSE 2D (m)"),
        ("median_2d_m", "Median 2D (m)"), ("max_2d_m", "Max 2D (m)"),
        ("rmse_3d_m", "RMSE 3D (m)"),
        ("bias_magnitude_m", "Bias (m)"),
        ("bias_bearing_deg", "Bearing (deg)"),
        ("bias_fraction", "Bias fraction"), ("bias_class", "Bias class"),
        ("status", "QA01 status"), ("stats_source", "Stats source"),
    ]
    for sensor, sub in summary.groupby("sensor", dropna=False):
        lines.append(f"## Sensor: {sensor}")
        lines.append("")
        table = sub[[c for c, _ in display_cols]].rename(
            columns=dict(display_cols))
        lines.append(cf.markdown_table(table))
        lines.append("")

        # +++++ Worst-run callouts +++++
        with_rmse = sub.dropna(subset=["rmse_2d_m"])
        if not with_rmse.empty:
            worst = with_rmse.loc[with_rmse["rmse_2d_m"].idxmax()]
            lines.append(
                f"- Worst 2D RMSE: **{worst['rmse_2d_m']:.3f} m** "
                f"({worst['run_label']}, {worst['product_label']}).")
        failing = sub[sub["status"] == "fail"]
        if not failing.empty:
            fails = ", ".join(
                f"{r.run_label} ({r.product_label})"
                for r in failing.itertuples())
            lines.append(f"- QA01 FAILED run-layer(s): {fails}.")
        unmatched = sub[["n_only_groundtruth", "n_only_qc"]].sum(
            numeric_only=True)
        lines.append(
            f"- Unmatched IDs across runs: "
            f"{int(unmatched.get('n_only_groundtruth') or 0)} groundtruth-only, "
            f"{int(unmatched.get('n_only_qc') or 0)} QC-only.")
        lines.append("")

        # +++++ Figures (relative-path embeds) +++++
        for key, title in [("metrics", "Accuracy metrics per run"),
                           ("bias_vectors", "Per-run 2D bias vectors"),
                           ("per_gcp", "Per-GCP displacement trends")]:
            fig = fig_paths.get(f"{sensor}|{key}")
            if fig is None:
                continue
            rel = os.path.relpath(fig, out_dir).replace(os.sep, "/")
            lines.append(f"### {title}")
            lines.append("")
            lines.append(f"![{title}]({rel})")
            lines.append("")

    # ========== Skipped artefacts ==========
    skipped = [e for e in entries if e.get("skip_reason") is not None]
    if skipped:
        lines.append("## Skipped artefacts")
        lines.append("")
        for e in skipped:
            lines.append(f"- `{e['table_path'].name}`: {e['skip_reason']}")
        lines.append("")

    report_path = out_dir / "QC_GCP_run_comparison.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {report_path}")


# ==================================================================================
def end_of_run_summary(entries: List[Dict[str, Any]]) -> pd.DataFrame:
    """Assemble the REPORTED/SKIPPED end-of-run summary DataFrame.

    Parameters
    ----------
    entries : list of dict
        All artefact entries.

    Returns
    -------
    pd.DataFrame
        One row per artefact: ``project, sensor, date, run, product,
        n, rmse_2d_m, status, reason``.
    """
    rows = []
    for e in entries:
        date = pd.to_datetime(e.get("date"), errors="coerce")
        row = {
            "project": e.get("project"), "sensor": e.get("sensor"),
            "date": date.strftime("%Y-%m-%d") if pd.notna(date) else None,
            "run": e.get("run"), "product": e.get("product"),
            "n": None, "rmse_2d_m": None,
            "status": ("skipped" if e.get("skip_reason") is not None
                       else "reported"),
            "reason": e.get("skip_reason"),
        }
        if e.get("df") is not None and e.get("skip_reason") is None:
            d2d = e["df"]["distance_2d_m"].dropna()
            row["n"] = int(len(e["df"]))
            row["rmse_2d_m"] = (float(np.sqrt(np.mean(d2d.to_numpy() ** 2)))
                                if len(d2d) else None)
        rows.append(row)
    columns = ["project", "sensor", "date", "run", "product",
               "n", "rmse_2d_m", "status", "reason"]
    return pd.DataFrame(rows, columns=columns)


# ==================================================================================
def _print_end_of_run(df: pd.DataFrame) -> None:
    """Print the end-of-run summary split into reported/skipped tables.

    Parameters
    ----------
    df : pd.DataFrame
        Frame from :func:`end_of_run_summary`.

    Returns
    -------
    None
    """
    if df.empty:
        print("\nNo artefacts to summarise.")
        return
    disp = df.copy()
    disp["rmse_2d_m"] = disp["rmse_2d_m"].map(
        lambda v: "" if v is None or pd.isna(v) else f"{v:.4f}")
    disp["product"] = disp["product"].fillna("")
    disp["reason"] = disp["reason"].fillna("")
    reported = disp[disp["status"] == "reported"]
    skipped = disp[disp["status"] == "skipped"]
    if not skipped.empty:
        print(f"\nSKIPPED ({len(skipped)}):")
        print(skipped.drop(columns=["n", "rmse_2d_m"]).to_string(index=False))
    if not reported.empty:
        print(f"\nREPORTED ({len(reported)}):")
        print(reported.drop(columns=["reason"]).to_string(index=False))


# ==================================================================================
if __name__ == "__main__":
    # ========== Parse args ==========
    parser = argparse.ArgumentParser(description="Compare QA01 GCP distance results across runs (multi-run GCP accuracy QC).")
    parser.add_argument("--path", type=str, default=None, help="Node or project folder to crawl for QA01 distance tables. Defaults to the git repo root. Node folders save results to <Node>/Documents/QCReports/, project folders to <Project>/Documentation/QCReports/; any other level requires --output-dir.")
    parser.add_argument("--output-dir", type=str, default=None, help="Explicit output directory (overrides the node/project routing; required for other path levels unless --no-save).")
    parser.add_argument("--no-save", default=False, action="store_true", help="Display the figures interactively instead of saving them. Nothing is written to disk.")
    parser.add_argument("--load-dir", type=str, default=None, help="Also load distance tables from this folder, searched recursively (e.g. a --save-dir container received from another node).")
    parser.add_argument("--save-dir", type=str, default=None, help="Build a portable GCP-accuracy container in this directory: gathered tables (tables/), per-run QA01 reports (reports/) and displacement figures (figures/), and this script's comparison figures (comparison_figures/).")
    parser.add_argument("--start-date", type=str, default=None, help="Only include runs on or after this date (inclusive; e.g. 2026-08-01 or 20260801).")
    parser.add_argument("--end-date", type=str, default=None, help="Only include runs on or before this date (inclusive).")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[], help="Directory names to exclude from the table search.")
    parser.add_argument("-f", "--force", default=False, action="store_true", help="Regenerate the comparison outputs even when they are newer than every gathered input.")
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
