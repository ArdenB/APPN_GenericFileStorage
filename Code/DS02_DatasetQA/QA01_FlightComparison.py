"""Multi-run flight/acquisition comparison (QA01).

Gathers the per-run QC01_FlightCheck contract reports
(``<run>/T1_proc/QC_data/QC01_FlightCheck/QC01_FlightCheck_detail.json``
+ sibling ``flight_lines.csv``) across every run under the given path,
assembles cross-run tables, flags acquisition anomalies, and produces
comparison figures: one point/line per run so acquisition conditions,
exposure tuning, and QC flags can be compared across a whole campaign.

QA01 consumes QC01's saved artefacts **only** — it never re-opens the
gpro/graw bundles (the QA02/QA00 rule). Run QC01 first.

Where results are saved depends on the level of the path provided:

- node folder    -> ``<Node>/Documents/QAReports/``
- project folder -> ``<Project>/Documentation/QAReports/``
- site folder    -> ``<Site>/Documentation/QAReports/``
- anything else  -> ``--output-dir`` is required.

Filenames carry the crawl scope so comparisons at different scopes
never clobber; tables and figures live in the scoped subfolder next to
the contract detail JSON, with the contract summary YAML at the top.

Figures
-------
1. exposure_vs_clearsky  - VNIR/SWIR exposure (settings.txt, the applied
   ET; hdr exposure can be a stale echo of the previous attempt) vs
   run-mean clear-sky GHI.
2. photon_dose           - exposure x GHI per sensor per run, VNIR/SWIR
   panels (tuning to constant saturation should make this ~flat).
3. diurnal_coverage      - per-run acquisition window on a
   time-to-solar-noon axis, fieldbook +/-120 min window shaded.
4. sun_line_angle        - per-line sun-flight-line angle by run; >60 deg
   (sun abeam, cross-track BRDF risk) band marked.
5. agl_consistency       - per-line AGL by run with nominal mission
   altitudes (30/50/80 m) as reference lines; colour = sensor, marker
   shape = QC01 likely_airframe (o = M350, ^ = IF1200A, X = unknown).
6. panel_detections      - reflectance-panel detections per run, zeros
   highlighted.
7. height_stability      - within-line spread of per-frame KML vertex
   heights (VNIR trajectory): absolute range and range as % of AGL
   (5% guide line); airframe marked by marker shape as in 5.

Tables
------
- acquisition_comparison.csv - one row per sensor-run with key metrics.
- anomalies.csv              - explicit QC flags (exposure mismatch, no
  panels, no graw, outside solar window, sun abeam, AGL gaps).

The anomaly rules also grade the contract report: one check per rule,
``good`` when no run raised it, ``warning`` otherwise (thresholds are
still inline; they externalize to ``reference/thresholds/`` in Phase 3).

Command-line Arguments
----------------------
--path : str, optional
    Node/project/site folder to crawl for QC01 outputs. Defaults to
    the root directory of the git repository.
--output-dir : str, optional
    Where to save outputs. Required when --path is not a node, project,
    or site folder (unless --no-save).
--start-date / --end-date : str, optional
    Only include runs whose first line starts on/after / on/before this
    date (e.g. 2026-08-01 or 20260801).
--no-save : flag
    Show figures interactively instead of saving them.
--exclude-dir : str [str ...]
    Directory names to exclude from the crawl.
--include-runs : {untriaged, degraded, failed}, optional
    Cumulative severity ladder for runs flagged in ``RunOverview.csv``.
    Default: clean runs only. ``untriaged`` also includes Issues runs
    with open TODO/wip tickets; ``degraded`` adds confirmed
    caution/failed tickets; ``failed`` adds RunFailed runs.
--include-duplicates : flag
    Include runs flagged ``DuplicateRun`` (orthogonal to
    --include-runs).
"""

# ==============================================================================

__title__ = "Flight comparison"
__author__ = "Arden Burrell"
__version__ = "v2.2(26.08.2026)"
__email__ = "arden.burrell@sydney.edu.au"

# ==============================================================================
# ========== Import core packages ==========
import os
import sys
import argparse
import json
import pathlib
from typing import Any, Dict, List, Optional

# ========== Import other packages ==========
import git
from git import exc as git_exc
import pandas as pd
import warnings as warn

import matplotlib.legend as mlegend
import matplotlib.lines as mlines
import matplotlib.pyplot as plt
import seaborn as sns

# ========== Resolve git root (must happen before importing functions.*) ==========
# This is the ONLY executable top-level code permitted (see guide/01 R1/R5).
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
import Code.functions.issue_yaml as iy
import Code.functions.qc_report as qr


# ==================================================================================
def main(args: argparse.Namespace) -> None:
    """Top-level orchestration. Reads like pseudocode.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.

    Returns
    -------
    None
    """
    path = pathlib.Path(args.path) if args.path else pathlib.Path(_git_root)
    # ========== Step 1: resolve the routed output location + scope ==========
    out_dir = cf.resolve_qareports_dir(path, args.output_dir, args.no_save)
    scope = cf.scope_label(path)
    # ========== Step 2: gather QC01 outputs ==========
    runs, lines, exposure = gather_runs(
        path, args.start_date, args.end_date, exclude_dirs=args.exclude_dir,
        include_runs=args.include_runs,
        include_duplicates=args.include_duplicates,
    )
    # ========== Step 3: cross-run tables ==========
    comparison = build_comparison(runs, lines, exposure)
    anomalies = build_anomalies(comparison, lines)
    # ========== Step 4: outputs ==========
    fig_dir = None
    if out_dir is not None:
        fig_dir = out_dir / f"QA01_FlightComparison_{scope}"
        fig_dir.mkdir(parents=True, exist_ok=True)
        comparison.to_csv(fig_dir / "acquisition_comparison.csv", index=False)
        anomalies.to_csv(fig_dir / "anomalies.csv", index=False)
        print(f"Wrote comparison ({len(comparison)} sensor-runs) and "
              f"anomalies ({len(anomalies)} flags) tables.")
    # ========== Step 5: figures ==========
    make_figures(runs, lines, exposure, comparison, fig_dir)
    # ========== Step 6: contract report ==========
    if out_dir is not None:
        report = build_contract_report(path, scope, runs, comparison,
                                       anomalies)
        summary_path, _ = qr.write_report(out_dir, report)
        print(f"Done. Outputs in {fig_dir} (summary: {summary_path.name})")


# ==================================================================================
def build_contract_report(
    path: pathlib.Path,
    scope: str,
    runs: pd.DataFrame,
    comparison: pd.DataFrame,
    anomalies: pd.DataFrame,
) -> Dict[str, Any]:
    """Assemble the contract report: one check per anomaly rule.

    Each rule grades ``good`` when no sensor-run raised it and
    ``warning`` otherwise (anomalies are advisory cross-run signals,
    never hard fails).

    Parameters
    ----------
    path : pathlib.Path
        The crawl root (recorded as the scope identity).
    scope : str
        Filename scope label.
    runs : pd.DataFrame
        Per-run table from :func:`gather_runs`.
    comparison : pd.DataFrame
        Comparison table from :func:`build_comparison`.
    anomalies : pd.DataFrame
        Flag table from :func:`build_anomalies`.

    Returns
    -------
    dict
        Contract detail-report dict, ready for ``qr.write_report``.
    """
    report = qr.new_report("QA01_FlightComparison", __version__, run={
        "scope_path": str(path),
        "n_runs": int(len(runs)),
        "n_sensor_runs": int(len(comparison)),
        "first_run_utc": str(runs["start_utc"].min()),
        "last_run_utc": str(runs["start_utc"].max()),
    })
    report["scope"] = scope
    rule_notes = {
        "exposure_mismatch": "settings.txt vs hdr exposure disagree",
        "no_graw": "raw bundle absent - exposure/panels unverifiable",
        "no_panels": "no reflectance-panel detections",
        "outside_solar_window": "lines beyond fieldbook +/-120 min window",
        "sun_abeam": "line(s) near sun-abeam: cross-track BRDF risk",
        "agl_gaps": "line(s) with no DTM AGL",
    }
    for rule, note in rule_notes.items():
        hits = anomalies[anomalies["flag"] == rule]
        if hits.empty:
            qr.add_check(report, rule, "good", value="0 runs")
        else:
            affected = sorted(hits["run_id"].unique())
            qr.add_check(
                report, rule, "warning",
                value=f"{len(affected)} run(s)", note=note,
                evidence=affected)
    report["anomalies"] = anomalies.to_dict(orient="records")
    stem = f"QA01_FlightComparison_{scope}"
    report["artifacts"] = [
        f"{stem}/acquisition_comparison.csv", f"{stem}/anomalies.csv",
    ] + [f"{stem}/{name}.png" for name in (
        "exposure_vs_clearsky", "photon_dose", "diurnal_coverage",
        "sun_line_angle", "agl_consistency", "panel_detections",
        "height_stability")]
    return report


# ==================================================================================
def _unique_run_labels(metas: List[Dict[str, str]]) -> List[str]:
    """Build unique, compact run labels from parsed storage identities.

    project/site/sensor enter the label only where they differ across
    the crawl; date and run folder always do. Uniqueness is guaranteed:
    two distinct run folders must differ in at least one identity field,
    and any field that differs anywhere is included.

    Parameters
    ----------
    metas : list of dict
        One ``{project, site, sensor, date, run_folder}`` per run, in
        gather order.

    Returns
    -------
    list of str
        One label per input, same order.
    """
    varying = [c for c in ("project", "site", "sensor")
               if len({m[c] for m in metas}) > 1]
    return [" ".join([m[c] for c in varying] + [m["date"], m["run_folder"]])
            for m in metas]


# ==================================================================================
def gather_runs(
    path: pathlib.Path,
    start_date: Optional[str],
    end_date: Optional[str],
    exclude_dirs: Optional[List[str]] = None,
    include_runs: Optional[str] = None,
    include_duplicates: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Crawl ``path`` for QC01 outputs and load them.

    A run is any ``QC01_FlightCheck`` folder containing both the contract
    detail JSON (whose ``acquisition_report`` block carries the per-field
    source-tagged report) and ``flight_lines.csv``. Runs flagged in their
    date folder's ``RunOverview.csv`` are excluded unless opted in
    (see :func:`Code.functions.issue_yaml.run_exclusion`).

    ``run_id`` is a unique display label built from the parsed storage
    identity of each run folder (project/site/sensor included only where
    they differ across the crawl; date + run folder always) — gpro
    bundle stems are **not** unique across dates/projects (e.g.
    ``Menindee_A1_East.gpro`` recurs on every date) and are kept as the
    ``gpro_stem`` column only.

    Parameters
    ----------
    path : pathlib.Path
        Tree to crawl recursively.
    start_date : Optional[str]
        Keep runs starting on/after this date (parsed by pandas).
    end_date : Optional[str]
        Keep runs starting on/before this date.
    exclude_dirs : list of str, optional
        Directory names to exclude from the crawl.
    include_runs : str or None, optional
        ``--include-runs`` severity ladder level (None = clean only).
    include_duplicates : bool, optional
        Include runs flagged ``DuplicateRun``. Default False.

    Returns
    -------
    tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]
        (runs, lines, exposure): one row per run / per flight line /
        per sensor-run exposure record.

    Raises
    ------
    FileNotFoundError
        If no QC01 outputs are found under ``path``.
    """
    exclude_set = set(exclude_dirs or [])

    def _excluded(p: pathlib.Path) -> bool:
        return bool(exclude_set
                    and (set(par.name for par in p.parents) & exclude_set))

    run_rows, line_frames, exp_rows = [], [], []
    metas: List[Dict[str, str]] = []
    excluded: List[str] = []
    for det_file in sorted(path.rglob("QC01_FlightCheck_detail.json")):
        d = det_file.parent
        if _excluded(det_file) or not (d / "flight_lines.csv").is_file():
            continue
        with open(det_file, encoding="utf-8") as fh:
            detail = json.load(fh)
        rep = detail.get("acquisition_report")
        if not rep:
            warn.warn(f"{det_file}: no acquisition_report block - skipped.")
            continue
        run_dir = det_file.parents[3]  # QC01_FlightCheck/QC_data/T1_proc/<run>
        meta = cf.parse_APPN_dataset_path(run_dir)
        if not meta["valid"] or meta["run"] is None:
            warn.warn(f"{det_file}: run path does not parse as an APPN run "
                      f"({meta['errors']}) - skipped.")
            continue
        reason = iy.run_exclusion(run_dir.parent, run_dir.name,
                                  include_runs=include_runs,
                                  include_duplicates=include_duplicates)
        if reason is not None:
            excluded.append(f"{run_dir}: {reason}")
            continue
        fl = pd.read_csv(d / "flight_lines.csv",
                         parse_dates=["start_utc", "mid_utc", "end_utc"])
        # run_id is finalised after the crawl (labels need the union);
        # tag everything with the entry index for now
        run_id = len(metas)
        metas.append({
            "project": str(meta["project"]), "site": str(meta["site"]),
            "sensor": str(meta["sensor"]),
            "date": pd.Timestamp(meta["date"]).strftime("%Y%m%d"),
            "run_folder": str(meta["run_folder"]),
        })
        fl["run_id"] = run_id
        airframe = (rep.get("gnss_lever_arms") or {}).get("likely_airframe")
        fl["likely_airframe"] = airframe or "unknown"
        start = fl["start_utc"].min()
        run_rows.append({
            "run_id": run_id,
            **metas[-1],
            "gpro_stem": rep["run"]["run_id"],
            "location": rep["mission"].get("location"),
            "start_utc": start,
            "end_utc": fl["end_utc"].max(),
            "n_lines": len(fl),
            "likely_airframe": airframe,
            "graw_missing": rep["run"].get("graw_bundle") == "MISSING",
            "panels_present": rep["panels"].get("panels_present"),
            "n_panel_detections": rep["panels"].get("n_panel_detections"),
            "mean_ghi": fl["clearsky_ghi_wm2"].mean(),
        })
        line_frames.append(fl)
        for sid, rec in (rep.get("exposure", {}).get("sensors") or {}).items():
            st = rec.get("settings_txt") or {}
            hdr_exp = (rec.get("hdr_exposure_ms_range") or [None])[0]
            set_exp = st.get("exposure_ms")
            # settings.txt is the applied ET; hdr exposure can be a stale
            # echo of the previous attempt (see gryfn_exposure_hdr_vs_settings.md)
            exposure_ms = set_exp if set_exp is not None else hdr_exp
            mismatch = (hdr_exp is not None and set_exp is not None
                        and abs(hdr_exp - set_exp) > 0.01)
            exp_rows.append({
                "run_id": run_id, "sensor_id": sid,
                "type": "VNIR" if sid.startswith("nHP") else "SWIR",
                "exposure_ms": exposure_ms, "settings_exposure_ms": set_exp,
                "hdr_exposure_ms": hdr_exp, "exposure_mismatch": mismatch,
                "mean_ghi": fl["clearsky_ghi_wm2"].mean(),
            })
    if excluded:
        print(f"EXCLUDED {len(excluded)} flagged run(s) (RunOverview.csv):")
        for line in excluded:
            print(f"  {line}")
    if not run_rows:
        raise FileNotFoundError(
            f"No QC01 outputs (QC01_FlightCheck_detail.json + "
            f"flight_lines.csv) found under {path}. "
            "Run QC01_FlightCheck.py first"
            + (", or widen --include-runs / --include-duplicates "
               "(every discovered run was excluded above)."
               if excluded else "."))
    # ========== Finalise unique run_id labels from the identity union ==========
    labels = _unique_run_labels(metas)
    for rr in run_rows:
        rr["run_id"] = labels[rr["run_id"]]
    for fl in line_frames:
        fl["run_id"] = labels[int(fl["run_id"].iloc[0])]
    for er in exp_rows:
        er["run_id"] = labels[er["run_id"]]
    runs = pd.DataFrame(run_rows).sort_values("start_utc")
    if start_date:
        runs = runs[runs["start_utc"] >= pd.Timestamp(start_date, tz="UTC")]
    if end_date:
        runs = runs[runs["start_utc"] <= pd.Timestamp(end_date, tz="UTC")
                    + pd.Timedelta(days=1)]
    keep = set(runs["run_id"])
    lines = pd.concat(line_frames, ignore_index=True)
    lines = lines[lines["run_id"].isin(keep)]
    exposure = pd.DataFrame(exp_rows)
    exposure = exposure[exposure["run_id"].isin(keep)]
    print(f"Gathered {len(runs)} runs, {len(lines)} flight lines, "
          f"{len(exposure)} sensor exposure records from {path}")
    return runs, lines, exposure


# ==================================================================================
def build_comparison(
    runs: pd.DataFrame, lines: pd.DataFrame, exposure: pd.DataFrame
) -> pd.DataFrame:
    """One row per sensor-run with the key cross-run metrics.

    Parameters
    ----------
    runs : pd.DataFrame
        Per-run table from :func:`gather_runs`.
    lines : pd.DataFrame
        Per-line table from :func:`gather_runs`.
    exposure : pd.DataFrame
        Per sensor-run exposure table from :func:`gather_runs`.

    Returns
    -------
    pd.DataFrame
        Comparison table sorted by run start time.
    """
    per_run = lines.assign(
        # 0/180 deg = along-track (fine); risk peaks at 90 deg (sun abeam)
        abeamness=90.0 - (lines["sun_line_angle_deg"] - 90.0).abs()
    ).groupby("run_id").agg(
        mean_agl_m=("agl_m", "mean"),
        agl_nan_lines=("agl_m", lambda s: int(s.isna().sum())),
        mean_solar_elev=("solar_elevation_deg", "mean"),
        t2noon_first=("time_to_solar_noon_min", "min"),
        t2noon_last=("time_to_solar_noon_min", "max"),
        max_abeamness=("abeamness", "max"),
    ).reset_index()
    # base = every sensor-run seen in the lines table; exposure may be absent
    base = lines[["run_id", "sensor_id"]].drop_duplicates()
    df = (base.merge(per_run, on="run_id")
          .merge(exposure, on=["run_id", "sensor_id"], how="left")
          .merge(
              runs[["run_id", "gpro_stem", "project", "site", "sensor",
                    "date", "run_folder", "location", "start_utc",
                    "n_lines", "likely_airframe", "graw_missing",
                    "panels_present", "n_panel_detections", "mean_ghi"]]
              .rename(columns={"mean_ghi": "run_mean_ghi"}),
              on="run_id",
          ))
    df["exposure_mismatch"] = df["exposure_mismatch"].fillna(False)
    df["photon_dose"] = df["exposure_ms"] * df["run_mean_ghi"]
    return df.sort_values(["start_utc", "sensor_id"]).reset_index(drop=True)


# ==================================================================================
def build_anomalies(comparison: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
    """Explicit QC flag list, one row per (run, flag).

    Parameters
    ----------
    comparison : pd.DataFrame
        Output of :func:`build_comparison`.
    lines : pd.DataFrame
        Per-line table (for line-level counts).

    Returns
    -------
    pd.DataFrame
        Columns: run_id, sensor_id, flag, detail.
    """
    flags = []
    for _, r in comparison.iterrows():
        if r["exposure_mismatch"]:
            flags.append((r["run_id"], r["sensor_id"], "exposure_mismatch",
                          f"settings {r['settings_exposure_ms']} vs "
                          f"hdr {r['hdr_exposure_ms']} ms"))
        if r["graw_missing"]:
            flags.append((r["run_id"], r["sensor_id"], "no_graw",
                          "raw bundle absent - exposure/panels unverifiable"))
        if r["panels_present"] is False:
            flags.append((r["run_id"], r["sensor_id"], "no_panels",
                          "no reflectance-panel detections in targets.yaml"))
        if max(abs(r["t2noon_first"]), abs(r["t2noon_last"])) > 120:
            flags.append((r["run_id"], r["sensor_id"], "outside_solar_window",
                          f"lines span {r['t2noon_first']:.0f} to "
                          f"{r['t2noon_last']:.0f} min from solar noon "
                          "(fieldbook +/-120)"))
        if r["max_abeamness"] > 60:
            flags.append((r["run_id"], r["sensor_id"], "sun_abeam",
                          f"line(s) within {90 - r['max_abeamness']:.0f} deg "
                          "of sun-abeam (90): cross-track BRDF risk"))
        if r["agl_nan_lines"] > 0:
            flags.append((r["run_id"], r["sensor_id"], "agl_gaps",
                          f"{r['agl_nan_lines']} line(s) with no DTM AGL"))
    df = pd.DataFrame(flags, columns=["run_id", "sensor_id", "flag", "detail"])
    return df.drop_duplicates(subset=["run_id", "flag", "detail"])


# ==================================================================================
def make_figures(
    runs: pd.DataFrame,
    lines: pd.DataFrame,
    exposure: pd.DataFrame,
    comparison: pd.DataFrame,
    out_dir: Optional[pathlib.Path],
) -> None:
    """Produce the six comparison figures.

    Parameters
    ----------
    runs, lines, exposure, comparison : pd.DataFrame
        Tables from :func:`gather_runs` / :func:`build_comparison`.
    out_dir : Optional[pathlib.Path]
        Save directory; None shows figures interactively (--no-save).

    Returns
    -------
    None

    Notes
    -----
    Style is set here, never at module level (guide/08).
    """
    sns.set_style("whitegrid")
    order = runs["run_id"].tolist()  # chronological
    figs = {
        "exposure_vs_clearsky": _fig_exposure_vs_clearsky(exposure),
        "photon_dose": _fig_photon_dose(comparison, order),
        "diurnal_coverage": _fig_diurnal_coverage(comparison, order),
        "sun_line_angle": _fig_lines_by_run(
            lines, order, "sun_line_angle_deg",
            "Sun-flight-line angle (deg)",
            "Per-line sun-line angle by run "
            "(0/180 = along-track; 60-120 band = sun abeam, BRDF risk)",
            hspan=(60, 120),
        ),
        "agl_consistency": _fig_lines_by_run(
            lines, order, "agl_m", "AGL (m)",
            "Per-line DTM-based AGL by run (30/50/80 m nominal; "
            "marker = airframe from GNSS lever arm)",
            hlines=(30, 50, 80),
            mark_airframe=True,
        ),
        "panel_detections": _fig_panels(runs, order),
        "height_stability": _fig_height_stability(lines, order),
    }
    for name, fig in figs.items():
        if out_dir is None:
            continue
        fig.savefig(out_dir / f"{name}.png", dpi=150)
        plt.close(fig)
        print(f"Saved {name}.png")
    if out_dir is None:
        plt.show()


# ==================================================================================
def _fig_exposure_vs_clearsky(exposure: pd.DataFrame) -> plt.Figure:
    """Exposure vs clear-sky GHI, VNIR/SWIR panels.

    Parameters
    ----------
    exposure : pd.DataFrame
        Per sensor-run exposure table.

    Returns
    -------
    plt.Figure
    """
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5), sharex=True)
    for ax, kind in zip(axes, ["VNIR", "SWIR"]):
        sub = exposure[(exposure["type"] == kind)
                       & exposure["exposure_ms"].notna()]
        palette = dict(zip(sorted(sub["sensor_id"].unique()),
                           sns.color_palette(n_colors=sub["sensor_id"].nunique())))
        for sid, g in sub.groupby("sensor_id"):
            ax.scatter(g["mean_ghi"], g["exposure_ms"], s=30,
                       color=palette[sid], label=sid)
        for _, r in sub.iterrows():
            ax.annotate(str(r["run_id"])[:16], (r["mean_ghi"], r["exposure_ms"]),
                        fontsize=6.5, xytext=(4, 4), textcoords="offset points")
        ax.set_title(f"{kind} (n={len(sub)})")
        ax.set_xlabel("Run-mean clear-sky GHI (W m$^{-2}$, Ineichen)")
        ax.set_ylabel("Exposure (ms)")
        ax.legend(fontsize=7)
    fig.suptitle("Hyperspec exposure vs modelled clear-sky irradiance")
    fig.tight_layout()
    return fig


# ==================================================================================
def _fig_photon_dose(comparison: pd.DataFrame, order: list[str]) -> plt.Figure:
    """Exposure x GHI per sensor per run, VNIR/SWIR panels.

    Parameters
    ----------
    comparison : pd.DataFrame
        Comparison table with photon_dose and type columns.
    order : list[str]
        Chronological run order.

    Returns
    -------
    plt.Figure
    """
    fig, axes = plt.subplots(2, 1, figsize=(12, 9), sharex=True)
    sub = comparison[comparison["photon_dose"].notna()]
    run_order = [r for r in order if r in set(sub["run_id"])]
    for ax, kind in zip(axes, ["VNIR", "SWIR"]):
        grp = sub[sub["type"] == kind]
        if len(grp):
            sns.pointplot(data=grp, x="run_id", y="photon_dose",
                          hue="sensor_id", order=run_order,
                          linestyle="none", ax=ax)
            ax.legend(fontsize=7)
        ax.set_title(f"{kind} (n={len(grp)})")
        ax.set_ylabel("Exposure x clear-sky GHI (ms W m$^{-2}$)")
        ax.set_xlabel("")
    axes[-1].tick_params(axis="x", rotation=90, labelsize=7)
    fig.suptitle("Photon-dose proxy per run "
                 "(constant saturation tuning => flat per sensor)")
    fig.tight_layout()
    return fig


# ==================================================================================
def _fig_diurnal_coverage(comparison: pd.DataFrame, order: list[str]) -> plt.Figure:
    """Per-run acquisition window on a time-to-solar-noon axis.

    Parameters
    ----------
    comparison : pd.DataFrame
        Comparison table with t2noon_first/last per sensor-run.
    order : list[str]
        Chronological run order.

    Returns
    -------
    plt.Figure
    """
    per_run = (comparison.groupby("run_id")
               .agg(first=("t2noon_first", "min"), last=("t2noon_last", "max"))
               .reindex(order).dropna())
    fig, ax = plt.subplots(figsize=(10, 0.45 * len(per_run) + 2))
    ax.axvspan(-120, 120, color="green", alpha=0.10,
               label="fieldbook +/-120 min")
    ax.axvline(0, color="k", lw=1)
    for i, (rid, r) in enumerate(per_run.iterrows()):
        ok = max(abs(r["first"]), abs(r["last"])) <= 120
        ax.plot([r["first"], r["last"]], [i, i], lw=6,
                color="tab:blue" if ok else "tab:red",
                solid_capstyle="butt")
    ax.set_yticks(range(len(per_run)), per_run.index, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("Time to solar noon (min)")
    ax.set_title("Acquisition windows relative to solar noon "
                 "(red = outside +/-120 min)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    return fig


# ==================================================================================
def _fig_lines_by_run(
    lines: pd.DataFrame,
    order: list[str],
    col: str,
    ylabel: str,
    title: str,
    hspan: Optional[tuple[float, float]] = None,
    hlines: tuple[float, ...] = (),
    mark_airframe: bool = False,
) -> plt.Figure:
    """Generic per-line box+strip by run for a flight-line column.

    Parameters
    ----------
    lines : pd.DataFrame
        Per-line table.
    order : list[str]
        Chronological run order.
    col : str
        Column of ``lines`` to plot.
    ylabel : str
        Y-axis label.
    title : str
        Figure title.
    hspan : Optional[tuple[float, float]]
        Shade this y-range (risk band).
    hlines : tuple[float, ...]
        Dashed reference lines.
    mark_airframe : bool
        Encode likely_airframe as marker shape (colour stays sensor).

    Returns
    -------
    plt.Figure
    """
    fig, ax = plt.subplots(figsize=(12, 5.5))
    sub = lines[lines[col].notna()]
    run_order = [r for r in order if r in set(sub["run_id"])]
    if hspan is not None:
        ax.axhspan(*hspan, color="red", alpha=0.08)
    for y in hlines:
        ax.axhline(y, color="grey", ls="--", lw=0.8)
    sns.boxplot(data=sub, x="run_id", y=col, order=run_order,
                color="lightsteelblue", fliersize=0, ax=ax)
    if mark_airframe:
        _strip_by_airframe(ax, sub, run_order, col)
    else:
        sns.stripplot(data=sub, x="run_id", y=col, order=run_order,
                      hue="sensor_id", size=3.5, ax=ax)
        ax.legend(fontsize=7)
    ax.set_ylabel(ylabel)
    ax.set_xlabel("")
    ax.set_title(title)
    ax.tick_params(axis="x", rotation=90, labelsize=7)
    fig.tight_layout()
    return fig


# ==================================================================================
def _airframe_markers() -> dict:
    """Fixed marker shapes for the QC01 likely_airframe categories.

    Returns
    -------
    dict
        airframe label -> matplotlib marker.
    """
    return {
        "DJI M350": "o",
        "Inspired Flight IF1200A": "^",
        "unrecognised mount": "s",
        "unknown": "X",
    }


# ==================================================================================
def _strip_by_airframe(
    ax: plt.Axes,
    sub: pd.DataFrame,
    run_order: list[str],
    col: str,
) -> None:
    """Strip points coloured by sensor with airframe as marker shape.

    Draws one stripplot per airframe group (seaborn takes a single marker
    per call) with a shared sensor palette, then builds two legends:
    sensor colours and black airframe markers.

    Parameters
    ----------
    ax : plt.Axes
        Target axes.
    sub : pd.DataFrame
        Per-line rows to plot (needs sensor_id + likely_airframe).
    run_order : list[str]
        X-axis category order.
    col : str
        Y column.

    Returns
    -------
    None
    """
    sensors = sorted(sub["sensor_id"].unique())
    palette = dict(zip(sensors, sns.color_palette(n_colors=len(sensors))))
    seen = []
    for af, marker in _airframe_markers().items():
        grp = sub[sub["likely_airframe"] == af]
        if not len(grp):
            continue
        seen.append((af, marker))
        sns.stripplot(data=grp, x="run_id", y=col, order=run_order,
                      hue="sensor_id", palette=palette, marker=marker,
                      size=4.5, linewidth=0.3, edgecolor="k",
                      legend=False, ax=ax)
    sensor_leg = ax.legend(
        handles=[mlines.Line2D([], [], color=c, marker="o", ls="", label=s)
                 for s, c in palette.items()],
        fontsize=7, title="sensor", title_fontsize=7, loc="upper left")
    ax.add_artist(sensor_leg)
    ax.legend(
        handles=[mlines.Line2D([], [], color="k", marker=m, ls="", label=af)
                 for af, m in seen],
        fontsize=7, title="airframe", title_fontsize=7, loc="upper right")


# ==================================================================================
def _fig_panels(runs: pd.DataFrame, order: list[str]) -> plt.Figure:
    """Panel detections per run, zeros/unknowns highlighted.

    Parameters
    ----------
    runs : pd.DataFrame
        Per-run table.
    order : list[str]
        Chronological run order.

    Returns
    -------
    plt.Figure
    """
    df = runs.set_index("run_id").reindex(order)
    n = df["n_panel_detections"].astype(float)
    colors = ["tab:red" if (pd.isna(v) or v == 0) else "tab:green" for v in n]
    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.bar(range(len(df)), n.fillna(0), color=colors)
    for i, v in enumerate(n):
        if pd.isna(v):
            ax.annotate("?", (i, 0.1), ha="center", fontsize=12,
                        color="tab:red")
    ax.set_xticks(range(len(df)), df.index, rotation=90, fontsize=7)
    ax.set_ylabel("Panel detections (targets.yaml)")
    ax.set_title("Reflectance-panel detections per run "
                 "(red = none, ? = no graw)")
    fig.tight_layout()
    return fig


# ==================================================================================
def _fig_height_stability(lines: pd.DataFrame, order: list[str]) -> plt.Figure:
    """Within-line flight-height spread, absolute and as % of AGL.

    VNIR lines only (the KML vertices follow the VNIR trajectory; SWIR
    duplicates it). Heights are ellipsoidal, so the spread is aircraft
    drift only, not terrain.

    Parameters
    ----------
    lines : pd.DataFrame
        Per-line table with flight_height_range_m and agl_m.
    order : list[str]
        Chronological run order.

    Returns
    -------
    plt.Figure
    """
    sub = lines[lines["sensor_id"].str.startswith("nHP")
                & lines["flight_height_range_m"].notna()].copy()
    sub["range_pct_agl"] = 100.0 * sub["flight_height_range_m"] / sub["agl_m"]
    run_order = [r for r in order if r in set(sub["run_id"])]
    fig, axes = plt.subplots(2, 1, figsize=(12, 9), sharex=True)
    for ax, col, ylabel in zip(
            axes,
            ["flight_height_range_m", "range_pct_agl"],
            ["Within-line height range (m)", "Height range (% of AGL)"]):
        pane = sub[sub[col].notna()]
        sns.boxplot(data=pane, x="run_id", y=col, order=run_order,
                    color="lightgrey", fliersize=0, ax=ax)
        _strip_by_airframe(ax, pane, run_order, col)
        ax.set_ylabel(ylabel)
        ax.set_xlabel("")
    # bottom panel: drop both legends (sensor one was added via add_artist)
    for leg in [a for a in axes[1].artists
                if isinstance(a, mlegend.Legend)] + [axes[1].get_legend()]:
        if leg is not None:
            leg.remove()
    axes[1].axhline(5, color="tab:red", ls="--", lw=0.9)
    axes[1].annotate("5% of AGL", (0.01, 5.1), xycoords=("axes fraction", "data"),
                     color="tab:red", fontsize=8, va="bottom")
    axes[1].tick_params(axis="x", rotation=90, labelsize=7)
    fig.suptitle(
        f"Flight-height stability per flight line (VNIR trajectory, "
        f"{sub['run_id'].nunique()} runs / {len(sub)} lines) - spread of "
        "per-frame KML vertex heights within each line")
    fig.tight_layout()
    return fig


# ==================================================================================
if __name__ == "__main__":
    # ========== chdir to git root (resolved at module top) ==========
    os.chdir(_git_root)

    # ========== Parse args ==========
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--path", type=str, default=None,
        help="Node, project, or site folder to crawl for QC01 outputs. "
             "Defaults to the git repo root. Node folders save results to "
             "<Node>/Documents/QAReports/, project/site folders to "
             "<level>/Documentation/QAReports/; any other level requires "
             "--output-dir.",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Explicit output directory (overrides the level routing; "
             "required for other path levels unless --no-save).",
    )
    parser.add_argument("--start-date", type=str, default=None,
                        help="Only runs starting on/after this date.")
    parser.add_argument("--end-date", type=str, default=None,
                        help="Only runs starting on/before this date.")
    parser.add_argument("--no-save", action="store_true",
                        help="Show figures interactively instead of saving.")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[],
                        help="Directory names to exclude from the crawl.")
    parser.add_argument("--include-runs", type=str, default=None,
                        choices=["untriaged", "degraded", "failed"],
                        help="Cumulative severity ladder for runs flagged in "
                             "RunOverview.csv. Default: clean runs only. "
                             "untriaged also includes Issues runs with open "
                             "TODO/wip tickets or no Issues.yaml yet; degraded "
                             "adds confirmed caution/failed tickets; failed "
                             "adds RunFailed runs.")
    parser.add_argument("--include-duplicates", action="store_true",
                        help="Include runs flagged DuplicateRun in "
                             "RunOverview.csv. Independent of --include-runs.")
    args = parser.parse_args()

    main(args)
