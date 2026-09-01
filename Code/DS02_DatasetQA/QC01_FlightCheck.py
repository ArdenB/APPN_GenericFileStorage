"""Flight check (QC01) — per-run acquisition parameters + FlightCal spec check.

Crawls the APPN dataset tree for processed runs (``<run>/T1_proc/*.gpro``),
scrapes each run's `.gpro` bundle for per-flight-line acquisition times,
line geometry, and DTM-based flight altitude (AGL), computes solar geometry
(altitude/azimuth, time to solar noon, sun-flight-line angle) and clear-sky
irradiance via pvlib, re-runs the GRYFN Flight Calculator equations on the
as-flown values, and grades bundle integrity — then writes the dual-file
QC report contract (``QC_PIPELINE_PLAN.md`` §2) into each run's
``T1_proc/QC_data/``.

Design rules (ported from APEx_Analysis DT00, decision 2026-08-18):

- The `.gpro` is the ONLY source for anything it should contain (post-PPK
  trajectory, DTM AGL, per-line times, line geometry). There is NO
  fallback to `.graw` nav data - a missing gpro field is reported missing.
- The `.graw` is consulted only for graw-exclusive fields (VNIR/SWIR
  exposure/gain, which exist only in the raw capture `settings.txt` /
  `raw_N.hdr` headers; GNSS/INS lever arms, which live in the SBG
  session JSON or the Applanix POSPac logs). Panel picks come from the
  gpro's `pipelines/*.yml` `hsi_preprocessing` blocks — the operator's
  actual ELM inputs — so they are checkable even without a graw.
- Runs post-QC on processed data: a run without a `.gpro` is skipped
  (not processed yet), never graded.
- Every scraped field is tagged `source: gpro|graw` in the report.

Outputs (per run, §4 layout):

- ``QC_data/QC01_FlightCheck_summary.yaml`` — contract summary.
- ``QC_data/QC01_FlightCheck/QC01_FlightCheck_detail.json`` — contract
  detail (checks + the full acquisition report + config snapshot).
- ``QC_data/QC01_FlightCheck/flight_lines.csv`` - one row per flight line.
- ``QC_data/QC01_FlightCheck/exposure_segments.csv`` - per raw capture
  segment VNIR/SWIR exposure.
- ``QC_data/QC01_FlightCheck/run_summary.csv`` - one row per run.

Within-spec check: as-flown values (AGL, ground speed, line spacing,
settings.txt optics) are re-run through the GRYFN Flight Calculator's
equations and classified against two threshold sets (the calculator's
breakpoints and the stricter fieldbook targets) from
``reference/thresholds/flightcal_spec.yml``; regression fixture in
``Code/DS02_DatasetQA/tests/test_flightcal_spec.py``. Rogue
take-off/landing capture lines (AGL far below the sensor median) are
flagged, excluded from line-spacing estimation and from the spec
verdicts, and reported separately.

Bundle-integrity checks (first-class, plan §7 Phase 2): graw presence,
dark-reference presence, reflectance-panel presence, and the per-region
reflectance orthomosaic products (a hyperspec sensor that flew but has no
``*_{VNIR|SWIR}_Orthomosaic.bin``, or a radiance-tagged header, is the
ELM-failed tell).

Note on heights: flight-line KML heights and the LiDAR DTM share the
GNSS trajectory's vertical frame, so their difference (AGL) is internally
consistent even though neither is orthometric.

Command-line Arguments
----------------------
--path : str, optional
    Folder to crawl for processed runs (any APPN tree level). Defaults
    to the git repo root.
--spec : str
    FlightCal spec/thresholds YAML relative to the repo root
    (default: reference/thresholds/flightcal_spec.yml; spec check
    skipped if missing).
--rogue-agl-frac : float
    Lines with AGL below this fraction of the per-sensor median AGL are
    flagged as rogue take-off/landing captures (default 0.5; 0 disables).
--rogue-len-frac : float
    Lines shorter than this fraction of the per-sensor median line length
    are flagged as capture stubs (default 0.2; 0 disables).
--exclude-dir : str [str ...]
    Directory names to exclude from the crawl.
--force : flag
    Regenerate reports even when they are newer than every input.
--verbose : flag
    Print extra diagnostic information.
"""

# ==============================================================================

__title__ = "Flight check"
__author__ = "Arden Burrell"
__version__ = "v2.1(01.09.2026)"
__email__ = "arden.burrell@sydney.edu.au"

# ==============================================================================
# ========== Import core packages ==========
import os
import sys
import argparse
import json
import pathlib
import re
from typing import Any, Dict, List, Optional, Tuple

# ========== Import other packages ==========
import git
from git import exc as git_exc
import numpy as np
import pandas as pd
import pvlib
import pyproj
import rasterio
import rasterio.warp
import yaml
from tqdm import tqdm
import warnings as warn

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
import Code.functions.qc_report as qr


# ==================================================================================
def main(args: argparse.Namespace) -> pd.DataFrame:
    """Top-level orchestration. Reads like pseudocode.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.

    Returns
    -------
    pd.DataFrame
        End-of-run summary: one row per discovered run with columns
        ``project, sensor, date, run, status, reason``.
    """
    path = pathlib.Path(args.path) if args.path else pathlib.Path(_git_root)
    # ========== Step 1: load the FlightCal spec (with provenance) ==========
    spec, spec_snapshot = load_flightcal_spec(pathlib.Path(args.spec))
    # ========== Step 2: discover processed runs ==========
    run_dirs = find_run_dirs(path, exclude_dirs=args.exclude_dir)
    # ========== Step 3: per-run processing ==========
    rows: List[Dict[str, Any]] = []
    for run_dir in tqdm(run_dirs, desc="QC01 flight check"):
        rows.append(process_run(run_dir, spec, spec_snapshot, args))
    # ========== Step 4: end-of-run summary ==========
    return _print_run_summary(rows)


# ==================================================================================
def find_run_dirs(
    path: pathlib.Path,
    exclude_dirs: Optional[List[str]] = None,
) -> List[pathlib.Path]:
    """Discover run directories holding a processed bundle under *path*.

    Searches for ``T1_proc/*.gpro`` bundles and returns the unique
    ``<run>`` directories, validated through the APPN path parser.

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively (any APPN tree level).
    exclude_dirs : list of str, optional
        Directory names to exclude from the search.

    Returns
    -------
    list of pathlib.Path
        Sorted unique run directories.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    """
    print(f"Scanning {path} for processed runs. {pd.Timestamp.now()}")
    if not path.is_dir():
        raise FileNotFoundError(f"Search path does not exist: {path}")
    exclude_set = set(exclude_dirs or [])

    def _excluded(p: pathlib.Path) -> bool:
        return bool(exclude_set
                    and (set(par.name for par in p.parents) & exclude_set))

    run_dirs = sorted({
        g.parent.parent for g in path.rglob("*.gpro")
        if g.parent.name == "T1_proc" and g.is_dir() and not _excluded(g)
    })
    print(f"Found {len(run_dirs)} run(s) with a .gpro bundle.")
    if not run_dirs:
        parsed = cf.parse_APPN_dataset_path(path)
        if not parsed["valid"]:
            warn.warn(
                f"{path} is not a valid APPN dataset path: "
                + " ".join(parsed["errors"]))
    return run_dirs


# ==================================================================================
def process_run(
    run_dir: pathlib.Path,
    spec: Optional[dict],
    spec_snapshot: Optional[Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    """Run the full flight check on one run directory.

    Parameters
    ----------
    run_dir : pathlib.Path
        Run directory containing ``T1_proc/*.gpro``.
    spec : dict or None
        Parsed FlightCal spec (None skips the spec check).
    spec_snapshot : dict or None
        ``{"path", "sha256"}`` config snapshot for the detail JSON.
    args : argparse.Namespace
        Parsed CLI arguments (rogue fracs, force, verbose).

    Returns
    -------
    dict
        Summary row: run metadata plus ``status`` and ``reason``.
    """
    parsed = cf.parse_APPN_dataset_path(run_dir)
    row = {key: parsed.get(key)
           for key in ("project", "site", "sensor", "run")}
    row["date"] = parsed.get("date")
    row.update({"status": "skipped", "reason": None})

    # ========== Malformed APPN trees: report why, never grade ==========
    if not parsed["valid"]:
        row["reason"] = ("invalid APPN folder structure: "
                         + " ".join(parsed["errors"]))
        return row

    # ========== Locate bundles ==========
    try:
        gpro, graw = locate_bundles(run_dir)
    except FileNotFoundError as err:
        row["reason"] = str(err)
        return row

    # ========== Skip when cached outputs are current ==========
    qc_data = run_dir / "T1_proc" / "QC_data"
    summary_path, detail_path = qr.report_paths(qc_data, "QC01_FlightCheck")
    inputs = [gpro / "mission_data.yaml"]
    if spec_snapshot is not None:
        inputs.append(pathlib.Path(spec_snapshot["path"]))
    inputs.extend(sorted((gpro / "pipelines").glob("*.yml")))
    if not args.force and cf.outputs_up_to_date(
            [summary_path, detail_path], inputs):
        row.update({"status": "cached", "reason": "outputs up to date"})
        return row

    # ========== Scrape the bundles (ex-DT00 pipeline) ==========
    mission = load_mission(gpro)
    try:
        df = extract_flight_lines(gpro, mission)
    except FileNotFoundError as err:
        # e.g. LiDAR+RGB-only gpro with no hyperspec flight-line
        # products - report the run, don't abort the crawl
        row["reason"] = str(err)
        return row
    if df.empty:
        row["reason"] = "no flight lines extracted from gpro"
        return row
    df = add_agl(df, gpro)
    df = flag_rogue_lines(df, frac=args.rogue_agl_frac,
                          len_frac=args.rogue_len_frac)
    df = add_solar_geometry(df)
    panels = panel_presence(gpro)
    exposure = read_exposure(graw, mission)
    df = add_coverage(df, exposure)
    trigger = read_trigger(graw, mission)
    lever = read_lever_arms(graw, mission)
    lever = airframe_from_system_cal(lever, gpro, graw)
    df, spec_report = add_spec_check(df, exposure, mission, spec)

    # ========== Write artefacts + the contract report ==========
    run_id = gpro.stem
    out_dir = qc_data / "QC01_FlightCheck"
    acq_report = write_outputs(out_dir, gpro, graw, mission, df, panels,
                               exposure, run_id, trigger, lever, spec_report)
    report = build_contract_report(
        parsed, gpro, graw, mission, df, panels, exposure,
        spec_report, spec_snapshot, acq_report)
    qr.write_report(qc_data, report)
    row.update({"status": report["status"], "reason": None})
    if args.verbose:
        tqdm.write(f"{run_dir}: {report['status']}")
    return row


# ==================================================================================
def build_contract_report(
    parsed: Dict[str, Any],
    gpro: pathlib.Path,
    graw: Optional[pathlib.Path],
    mission: dict,
    df: pd.DataFrame,
    panels: dict,
    exposure: dict,
    spec_report: Optional[dict],
    spec_snapshot: Optional[Dict[str, Any]],
    acq_report: dict,
) -> Dict[str, Any]:
    """Assemble the contract detail report for one run.

    Builds the §2 skeleton, adds the bundle-integrity checks and the
    FlightCal spec checks (statuses already use the shared check-level
    vocabulary), and attaches the full acquisition report, the config
    snapshot, and the staleness identity (gpro path + mtime).

    Parameters
    ----------
    parsed : dict
        ``cf.parse_APPN_dataset_path`` output for the run directory.
    gpro : pathlib.Path
        Path to the `.gpro` bundle.
    graw : pathlib.Path or None
        Path to the `.graw` bundle, or None.
    mission : dict
        Parsed mission metadata.
    df : pd.DataFrame
        Completed flight-line table.
    panels : dict
        Panel-presence record from :func:`panel_presence`.
    exposure : dict
        Hyperspec exposure record from :func:`read_exposure`.
    spec_report : dict or None
        Within-spec report block from :func:`add_spec_check`.
    spec_snapshot : dict or None
        Threshold-config provenance (``path`` + ``sha256``).
    acq_report : dict
        The full acquisition report from :func:`write_outputs`.

    Returns
    -------
    dict
        Contract detail-report dict, ready for ``qr.write_report``.
    """
    run_meta = {key: parsed.get(key)
                for key in ("node", "project", "site", "sensor", "run")}
    run_meta["date"] = parsed.get("date")
    run_meta["gpro"] = gpro.name
    run_meta["graw"] = graw.name if graw else None
    report = qr.new_report("QC01_FlightCheck", __version__, run=run_meta)

    # ========== Bundle-integrity checks ==========
    add_integrity_checks(report, gpro, graw, mission, panels, exposure)

    # ========== FlightCal spec checks ==========
    add_spec_contract_checks(report, mission, spec_report)

    # ========== Detail-only payload ==========
    report["acquisition_report"] = acq_report
    report["config"] = spec_snapshot or {"path": None, "sha256": None}
    report["staleness"] = {
        "gpro_path": str(gpro),
        "gpro_mtime_utc": pd.Timestamp(
            gpro.stat().st_mtime, unit="s", tz="UTC").isoformat(),
    }
    report["artifacts"] = [
        f"QC01_FlightCheck/{name}" for name in
        ("flight_lines.csv", "exposure_segments.csv", "run_summary.csv")
        if (pathlib.Path(gpro).parent.parent / "T1_proc" / "QC_data"
            / "QC01_FlightCheck" / name).is_file()
    ]
    return report


# ==================================================================================
def add_integrity_checks(
    report: Dict[str, Any],
    gpro: pathlib.Path,
    graw: Optional[pathlib.Path],
    mission: dict,
    panels: dict,
    exposure: dict,
) -> None:
    """Add the bundle-integrity checks to a contract report.

    Checks: graw presence, dark-reference presence, reflectance-panel
    presence, and per-region reflectance orthomosaic products (the
    ELM-failed/radiance tell).

    Parameters
    ----------
    report : dict
        Contract report dict (mutated in place).
    gpro : pathlib.Path
        Path to the `.gpro` bundle.
    graw : pathlib.Path or None
        Path to the `.graw` bundle, or None.
    mission : dict
        Parsed mission metadata (acquisition types).
    panels : dict
        Panel-presence record from :func:`panel_presence`.
    exposure : dict
        Hyperspec exposure record from :func:`read_exposure`.

    Returns
    -------
    None
    """
    # +++++ graw presence +++++
    if graw is None:
        qr.add_check(report, "graw_present", "warning", value="missing",
                     note="raw bundle absent — exposure/lever arms "
                          "unverifiable")
    else:
        qr.add_check(report, "graw_present", "good", value=graw.name)

    # +++++ dark-reference presence (hyperspec sensors) +++++
    darks = {sid: (rec.get("dark_reference") or {}).get("present")
             for sid, rec in exposure["sensors"].items()}
    if graw is None or not darks:
        qr.add_check(report, "dark_reference", "not_checked",
                     note="no raw bundle / no hyperspec captures found")
    elif all(darks.values()):
        qr.add_check(report, "dark_reference", "good",
                     value=", ".join(sorted(darks)))
    else:
        missing = sorted(sid for sid, ok in darks.items() if not ok)
        qr.add_check(report, "dark_reference", "warning",
                     value=f"missing: {', '.join(missing)}")

    # +++++ reflectance-panel picks (gpro pipeline yml) +++++
    if panels["panels_present"] is None:
        qr.add_check(report, "panels_present", "not_checked",
                     note="no pipeline yml in gpro")
    elif panels["panels_present"]:
        per_sensor = "; ".join(
            f"{name}: {rec['n_picks']} pick(s) in "
            f"{rec['n_capture_frames']} frame(s)"
            for name, rec in sorted(panels["sensors"].items()))
        dual = sorted(name for name, rec in panels["sensors"].items()
                      if rec["n_capture_frames"] >= 2)
        note = (f"panels marked in >=2 capture frames (dual ELM): "
                f"{', '.join(dual)}" if dual else None)
        qr.add_check(report, "panels_present", "good",
                     value=per_sensor,
                     note=note,
                     evidence=panels.get("target_files"))
    elif panels.get("no_panels_suffix"):
        qr.add_check(report, "panels_present", "acceptable", value="none",
                     note="bundle carries a _NoPanels suffix (declared)")
    else:
        qr.add_check(report, "panels_present", "warning", value="none",
                     note="no panel picks in the gpro pipeline yml")

    # +++++ reflectance orthomosaic products (ELM-failed tell) +++++
    flown = {str(a.get("type", "")).upper()
             for a in mission.get("acquisitions", [])}
    for region in ("VNIR", "SWIR"):
        name = f"reflectance_product_{region.lower()}"
        if region not in flown:
            qr.add_check(report, name, "not_checked",
                         note=f"{region} not in this run's acquisitions")
            continue
        status, value, note = _reflectance_product_status(gpro, region)
        qr.add_check(report, name, status, value=value, note=note)


# ==================================================================================
def _reflectance_product_status(
    gpro: pathlib.Path,
    region: str,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Grade one region's reflectance orthomosaic product.

    Parameters
    ----------
    gpro : pathlib.Path
        Path to the `.gpro` bundle.
    region : str
        ``"VNIR"`` or ``"SWIR"``.

    Returns
    -------
    tuple
        ``(status, value, note)`` — ``fail`` when the sensor flew but no
        ``*_{region}_Orthomosaic.bin`` exists (ELM/processing failure
        tell) or the ENVI header is radiance-tagged; ``good`` otherwise.
    """
    bins = sorted((gpro / "products").glob(f"*_{region}_Orthomosaic*.bin"))
    if not bins:
        return ("fail", "missing",
                f"no {region} reflectance orthomosaic in gpro/products — "
                "ELM failed or processing incomplete")
    hdr = bins[0].with_suffix(".hdr")
    if hdr.is_file():
        text = hdr.read_text(encoding="utf-8", errors="replace").lower()
        if "radiance" in text:
            return ("fail", bins[0].name,
                    "orthomosaic header is radiance-tagged — ELM failed")
    return ("good", bins[0].name, None)


# ==================================================================================
def add_spec_contract_checks(
    report: Dict[str, Any],
    mission: dict,
    spec_report: Optional[dict],
) -> None:
    """Project the FlightCal spec report onto contract checks.

    One check per sensor x metric, named by EM region where the sensor
    type is known (``sidelap_vnir_fieldbook`` style, §2 example).
    Statuses come straight from the spec report — already on the shared
    check-level scale.

    Parameters
    ----------
    report : dict
        Contract report dict (mutated in place).
    mission : dict
        Parsed mission metadata (maps sensor_id to VNIR/SWIR type).
    spec_report : dict or None
        Within-spec report from :func:`add_spec_check` (None or a
        skipped block adds a single ``not_checked`` entry).

    Returns
    -------
    None
    """
    if not spec_report or "linescan" not in spec_report:
        qr.add_check(report, "flightcal_spec", "not_checked",
                     note="spec file missing — FlightCal check skipped")
        return
    sensor_type = {str(a.get("sensor_id")): str(a.get("type", "")).lower()
                   for a in mission.get("acquisitions", [])}

    def _rng(pair: Optional[list], unit: str) -> Optional[str]:
        if not pair or all(v is None for v in pair):
            return None
        return f"{pair[0]:.1f}-{pair[1]:.1f} {unit}"

    for sid, rec in (spec_report.get("linescan") or {}).items():
        tag = sensor_type.get(sid) or sid.lower()
        qr.add_check(report, f"gsd_{tag}", rec["gsd_status"],
                     value=_rng(rec.get("gsd_cm_range"), "cm"))
        qr.add_check(report, f"frame_rate_{tag}",
                     rec["achieved_frame_rate_status"],
                     note=f"configured: {rec['configured_frame_rate_status']}")
        qr.add_check(report, f"sidelap_{tag}_calculator",
                     rec["sidelap_status_calculator"],
                     value=_rng(rec.get("sidelap_pct_range"), "%"))
        qr.add_check(report, f"sidelap_{tag}_fieldbook",
                     rec["sidelap_status_fieldbook"],
                     value=_rng(rec.get("sidelap_pct_range"), "%"))
        qr.add_check(report, f"oversampling_{tag}_fieldbook",
                     rec["oversampling_status_fieldbook"])
    lidar = spec_report.get("lidar") or {}
    if lidar.get("sidelap_status"):
        qr.add_check(report, "sidelap_lidar", lidar["sidelap_status"],
                     value=_rng(lidar.get("sidelap_pct_range"), "%"))


# ==================================================================================
def _print_run_summary(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """Print the end-of-run REPORTED/SKIPPED summary and return it.

    Parameters
    ----------
    rows : list of dict
        Per-run summary rows from :func:`process_run`.

    Returns
    -------
    pd.DataFrame
        The summary table (one row per discovered run).
    """
    df = pd.DataFrame(rows)
    if df.empty:
        print("\nNo runs found.")
        return df
    cols = ["project", "site", "sensor", "date", "run", "status", "reason"]
    df = df[[c for c in cols if c in df.columns]]
    skipped = df[df["status"] == "skipped"]
    reported = df[df["status"] != "skipped"]
    if not reported.empty:
        print("\n===== REPORTED runs =====")
        print(reported.drop(columns=["reason"]).to_string(index=False))
    if not skipped.empty:
        print("\n===== SKIPPED runs =====")
        print(skipped.to_string(index=False))
    return df


# ==================================================================================
def locate_bundles(
    run_dir: pathlib.Path,
) -> tuple[pathlib.Path, Optional[pathlib.Path]]:
    """Find the `.gpro` (required) and `.graw` (optional) bundles of a run.

    Parameters
    ----------
    run_dir : pathlib.Path
        Run directory containing `T0_raw/` and `T1_proc/`.

    Returns
    -------
    tuple[pathlib.Path, Optional[pathlib.Path]]
        Paths to the `.gpro` bundle and the `.graw` bundle (None if absent).

    Raises
    ------
    FileNotFoundError
        If ``run_dir`` or its `T1_proc/*.gpro` bundle is missing - this
        script runs post-QC, so "no gpro" is a genuine anomaly (PLAN.md).
    """
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")
    gpros = sorted((run_dir / "T1_proc").glob("*.gpro"))
    if not gpros:
        raise FileNotFoundError(
            f"No .gpro bundle in {run_dir / 'T1_proc'} - flight not "
            "processed. This report requires a processed bundle; no "
            "graw fallback (PLAN.md decision 2026-08-18)."
        )
    if len(gpros) > 1:
        warn.warn(f"Multiple .gpro bundles in {run_dir}; using {gpros[0].name}")
    gpro = gpros[0]
    # Some exports nest the real bundle inside a same-named wrapper folder
    if not (gpro / "mission_data.yaml").exists():
        nested = sorted(gpro.glob("*.gpro"))
        if nested and (nested[0] / "mission_data.yaml").exists():
            gpro = nested[0]
    # graw is matched by stem where possible (bundle names are free-text)
    graws = sorted((run_dir / "T0_raw").glob("*.graw"))
    graw = next((g for g in graws if g.stem == gpro.stem), None)
    if graw is None and graws:
        graw = graws[0]
    return gpro, graw


# ==================================================================================
def load_mission(gpro: pathlib.Path) -> dict:
    """Load `mission_data.yaml` from the gpro bundle.

    Parameters
    ----------
    gpro : pathlib.Path
        Path to the `.gpro` bundle directory.

    Returns
    -------
    dict
        Parsed mission metadata (location, pilot, conditions, acquisitions).

    Raises
    ------
    FileNotFoundError
        If `mission_data.yaml` is missing from the bundle.
    """
    path = gpro / "mission_data.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"mission_data.yaml missing from {gpro}")
    with open(path, encoding="utf-8") as fh:
        mission = yaml.safe_load(fh)
    print(f"Mission: location={mission.get('location')!r}, "
          f"pilot={mission.get('pilot', {}).get('name')!r}")
    return mission


# ==================================================================================
def extract_flight_lines(gpro: pathlib.Path, mission: dict) -> pd.DataFrame:
    """Extract per-flight-line times and geometry from the gpro.

    Keys on `mission_data.yaml -> acquisitions[].type` (never hard-coded
    sensor folder names). Times come from `flight_line_timestamps.yml`
    (per-frame epoch microseconds) cross-checked against
    `flight_line_info.txt`; geometry from `epoch_flightline_NNN.kml`
    (index-aligned with the timestamps yml, unlike `flight_line_NNN.json`).

    Parameters
    ----------
    gpro : pathlib.Path
        Path to the `.gpro` bundle directory.
    mission : dict
        Parsed mission metadata from :func:`load_mission`.

    Returns
    -------
    pd.DataFrame
        One row per flight line: sensor_id, line, n_frames, start/mid/end
        UTC, duration, heading, centroid lat/lon, mean flight height and
        within-line height stability (std, range).

    Raises
    ------
    FileNotFoundError
        If no hyperspectral acquisition has flight-line products - these
        are gpro-only fields and are reported missing, never scraped from
        the graw.
    """
    rows = []
    hyper = [a for a in mission.get("acquisitions", [])
             if a.get("type") in ("VNIR", "SWIR")]
    if not hyper:
        raise FileNotFoundError(f"No hyperspectral acquisition in {gpro}")
    for acq in hyper:
        # Windows-processed bundles write data_root with backslashes
        flight_dir = gpro / acq["data_root"].replace("\\", "/")
        ts_file = flight_dir / "flight_line_timestamps.yml"
        if not ts_file.is_file():
            warn.warn(
                f"{acq['sensor_id']}: flight_line_timestamps.yml missing "
                f"from {flight_dir} - lines not segmented; field reported "
                "missing (no graw fallback)."
            )
            continue
        times = read_line_times(ts_file)
        crosscheck_info_times(flight_dir / "flight_line_info.txt", times)
        kmls = sorted(flight_dir.glob("epoch_flightline_*.kml"))
        kml_coords = [parse_kml_coords(k) for k in kmls]
        if len(kmls) != len(times):
            warn.warn(
                f"{acq['sensor_id']}: {len(kmls)} epoch KMLs vs "
                f"{len(times)} timestamp lines - stale products from "
                "another segmentation pass present; pairing by frame count."
            )
        used: set[int] = set()
        for rec in tqdm(times, desc=f"{acq['sensor_id']} lines"):
            # epoch KMLs hold one vertex per frame, so a line's KML is the
            # one whose point count equals the yml frame count - more
            # robust than index pairing (flight_line_NNN.json and stale
            # KMLs can break index alignment)
            match = [j for j, c in enumerate(kml_coords)
                     if c is not None and len(c) == rec["n_frames"]
                     and j not in used]
            if match:
                used.add(match[0])
                geom = line_geometry(kml_coords[match[0]])
            elif rec["line"] < len(kml_coords) and kml_coords[rec["line"]] is not None:
                warn.warn(
                    f"{acq['sensor_id']} line {rec['line']}: no KML with "
                    f"{rec['n_frames']} points - falling back to index pairing."
                )
                geom = line_geometry(kml_coords[rec["line"]])
            else:
                warn.warn(
                    f"{acq['sensor_id']} line {rec['line']}: no epoch KML - "
                    "line geometry reported missing."
                )
                geom = line_geometry(None)
            rows.append({"sensor_id": acq["sensor_id"], **rec, **geom})
    if not rows:
        raise FileNotFoundError(
            f"No flight-line products found in {gpro} - gpro incomplete."
        )
    df = pd.DataFrame(rows)
    # ground speed is a fieldbook mission parameter (Type 1/2/3 speed bands)
    df["ground_speed_ms"] = np.round(
        df["line_length_m"] / df["duration_s"].replace(0, np.nan), 2
    )
    print(f"Extracted {len(df)} flight lines "
          f"from {df['sensor_id'].nunique()} sensor(s).")
    return df


# ==================================================================================
def read_line_times(ts_file: pathlib.Path) -> list[dict]:
    """Parse per-line start/mid/end times from `flight_line_timestamps.yml`.

    The file is a YAML list (one entry per flight line) of per-frame epoch
    timestamps in microseconds, with no timezone key - values are UTC
    (verified against `flight_line_info.txt`, see notes doc).

    Parameters
    ----------
    ts_file : pathlib.Path
        Path to `flight_line_timestamps.yml`.

    Returns
    -------
    list[dict]
        Per line: line index, n_frames, start/mid/end (tz-aware UTC
        Timestamps), duration_s.
    """
    print(f"Reading {ts_file.name} ...")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with open(ts_file, encoding="utf-8") as fh:
        lines = yaml.load(fh, Loader=loader)
    recs = []
    for i, frames in enumerate(lines):
        start = pd.to_datetime(frames[0], unit="us", utc=True)
        end = pd.to_datetime(frames[-1], unit="us", utc=True)
        dur = (end - start).total_seconds()
        recs.append({
            "line": i,
            "n_frames": len(frames),
            "start_utc": start,
            "mid_utc": start + (end - start) / 2,
            "end_utc": end,
            "duration_s": round(dur, 3),
            # achieved frame period during capture (frame timestamps, not
            # settings span - polygon triggering pauses between lines)
            "achieved_frame_period_ms": round(
                dur * 1000.0 / (len(frames) - 1), 4
            ) if len(frames) > 1 else np.nan,
        })
    return recs


# ==================================================================================
def crosscheck_info_times(
    info_file: pathlib.Path,
    times: list[dict],
    tol_s: float = 30.0,
) -> None:
    """Cross-check epoch-derived line times against `flight_line_info.txt`.

    The timestamps yml carries bare epoch microseconds with no timezone
    key; this validates the UTC interpretation against the info file's
    human-readable per-line start times (time-of-day only). Offsets of a
    few seconds are normal (trajectory-based line detection vs first
    camera frame); a timezone error would be >= 30 min.

    Parameters
    ----------
    info_file : pathlib.Path
        Path to `flight_line_info.txt`.
    times : list[dict]
        Output of :func:`read_line_times`.
    tol_s : float
        Maximum tolerated |difference| in seconds-of-day (default 30.0).

    Returns
    -------
    None
    """
    if not info_file.is_file():
        warn.warn(f"{info_file.name} missing - epoch times not cross-checked.")
        return
    pat = re.compile(r"^(\d{2}:\d{2}:\d{2}\.\d+)\s+(\d{2}:\d{2}:\d{2}\.\d+)")
    starts = []
    with open(info_file, encoding="utf-8", errors="replace") as fh:
        for row in fh:
            m = pat.match(row.strip())
            if m:
                h, mnt, s = m.group(1).split(":")
                starts.append(int(h) * 3600 + int(mnt) * 60 + float(s))
    if len(starts) != len(times):
        warn.warn(
            f"{info_file.name}: {len(starts)} detected lines vs "
            f"{len(times)} exported in timestamps yml - extra detections "
            "were rejected during processing."
        )
    for rec in times:
        t = rec["start_utc"]
        sod = t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6
        # nearest detected line, wrapping across midnight UTC
        diff = min(
            (min(abs(sod - s), 86400.0 - abs(sod - s)) for s in starts),
            default=np.nan,
        )
        if diff > tol_s:
            warn.warn(
                f"Line {rec['line']}: epoch start {sod:.1f}s-of-day is "
                f"{diff:.1f}s from the nearest info line (>{tol_s}s) - "
                "check UTC assumption."
            )
    print(f"Cross-checked {len(times)} line times "
          f"against {info_file.name}.")


# ==================================================================================
def parse_kml_coords(kml_file: pathlib.Path) -> Optional[np.ndarray]:
    """Parse the LineString coordinates from an epoch flight-line KML.

    Parameters
    ----------
    kml_file : pathlib.Path
        Path to `epoch_flightline_NNN.kml` (one vertex per hyperspec frame,
        lon,lat,height in the trajectory's vertical frame).

    Returns
    -------
    Optional[np.ndarray]
        (n, 3) array of lon/lat/height, or None if unreadable.
    """
    if not kml_file.is_file():
        return None
    txt = kml_file.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"<coordinates>(.*?)</coordinates>", txt, re.S)
    if m is None:
        return None
    return np.array(
        [[float(x) for x in c.split(",")] for c in m.group(1).split()],
        dtype=float,
    )


# ==================================================================================
def line_geometry(coords: Optional[np.ndarray]) -> dict:
    """Heading, centroid, and mean flight height from line coordinates.

    Parameters
    ----------
    coords : Optional[np.ndarray]
        (n, 3) lon/lat/height array from :func:`parse_kml_coords`, or None.

    Returns
    -------
    dict
        heading_deg (geodesic forward azimuth first->last vertex, 0-360),
        centroid_lat, centroid_lon, flight_height_m (mean vertex height),
        flight_height_std_m / flight_height_range_m (within-line height
        stability; ellipsoidal height, so terrain-independent),
        line_length_m (geodesic first->last distance); NaNs if ``coords``
        is None.
    """
    if coords is None:
        return {"heading_deg": np.nan, "centroid_lat": np.nan,
                "centroid_lon": np.nan, "flight_height_m": np.nan,
                "flight_height_std_m": np.nan, "flight_height_range_m": np.nan,
                "line_length_m": np.nan}
    geod = pyproj.Geod(ellps="WGS84")
    az, _, dist = geod.inv(coords[0, 0], coords[0, 1], coords[-1, 0], coords[-1, 1])
    heights = coords[:, 2]
    return {
        "heading_deg": round(az % 360.0, 2),
        "centroid_lat": coords[:, 1].mean(),
        "centroid_lon": coords[:, 0].mean(),
        "flight_height_m": round(float(heights.mean()), 2),
        "flight_height_std_m": round(float(heights.std()), 2),
        "flight_height_range_m": round(float(heights.max() - heights.min()), 2),
        "line_length_m": round(float(dist), 1),
    }


# ==================================================================================
def add_agl(df: pd.DataFrame, gpro: pathlib.Path) -> pd.DataFrame:
    """Add DTM ground elevation and AGL columns (gpro DTM only).

    AGL = mean flight height - DTM elevation at the line centroid. Both
    sides share the trajectory's vertical frame, so the difference is
    internally consistent. If the DTM product is missing the fields are
    reported missing - never derived from graw nav data.

    Parameters
    ----------
    df : pd.DataFrame
        Flight-line table from :func:`extract_flight_lines`.
    gpro : pathlib.Path
        Path to the `.gpro` bundle directory.

    Returns
    -------
    pd.DataFrame
        Input with `ground_elev_m` and `agl_m` columns added.
    """
    dtms = sorted((gpro / "products").glob("*_DTM_*.tif"))
    if not dtms:
        warn.warn(
            f"No LiDAR DTM product in {gpro / 'products'} - AGL reported "
            "missing (no graw fallback)."
        )
        df["ground_elev_m"] = np.nan
        df["agl_m"] = np.nan
        return df
    print(f"Sampling DTM {dtms[0].name} at line centroids ...")
    with rasterio.open(dtms[0]) as src:
        xs, ys = rasterio.warp.transform(
            "EPSG:4326", src.crs,
            df["centroid_lon"].tolist(), df["centroid_lat"].tolist(),
        )
        vals = np.array([v[0] for v in src.sample(zip(xs, ys))], dtype=float)
        if src.nodata is not None:
            vals[np.isclose(vals, src.nodata)] = np.nan
    df["ground_elev_m"] = np.round(vals, 2)
    df["agl_m"] = np.round(df["flight_height_m"] - df["ground_elev_m"], 2)
    if df["agl_m"].isna().any():
        warn.warn("Some line centroids fall on DTM nodata - AGL is NaN there.")
    return df


# ==================================================================================
def flag_rogue_lines(df: pd.DataFrame, frac: float = 0.5,
                     len_frac: float = 0.2) -> pd.DataFrame:
    """Flag rogue take-off/landing and capture-stub lines.

    The capture polygon can trigger while the drone is still climbing out
    of / descending into the field (a line far below the survey altitude,
    usually under the minimum capture height) or blip on polygon entry (a
    seconds-long stub at survey altitude, e.g. 124 frames / 2.4 m). Both
    corrupt line-spacing estimation for their neighbours and poison the
    worst-line-wins spec verdicts, so they are flagged here and excluded
    downstream (values still reported). Genuine within-run AGL variation
    is a few percent (campaign max ~6 %), so the median rules separate
    cleanly.

    Parameters
    ----------
    df : pd.DataFrame
        Flight-line table with agl_m (after :func:`add_agl`).
    frac : float
        Flag lines with AGL below ``frac`` x the per-sensor median AGL
        (default 0.5); <= 0 disables the AGL rule.
    len_frac : float
        Flag lines shorter than ``len_frac`` x the per-sensor median line
        length (default 0.2); <= 0 disables the stub rule.

    Returns
    -------
    pd.DataFrame
        Input with a boolean `rogue_line` column added.
    """
    df["rogue_line"] = False
    if frac <= 0 and len_frac <= 0:
        return df
    for sid, sub in df.groupby("sensor_id"):
        med_agl = sub["agl_m"].median()
        med_len = sub["line_length_m"].median()
        low_agl = (sub["agl_m"] < frac * med_agl) \
            if frac > 0 and np.isfinite(med_agl) else pd.Series(False, sub.index)
        stub = (sub["line_length_m"] < len_frac * med_len) \
            if len_frac > 0 and np.isfinite(med_len) else pd.Series(False, sub.index)
        df.loc[sub.index[low_agl | stub], "rogue_line"] = True
        for _, r in sub[low_agl | stub].iterrows():
            why = (f"AGL {r['agl_m']} m < {frac:g} x median ({med_agl:.1f} m)"
                   if low_agl.loc[r.name] else
                   f"length {r['line_length_m']} m < {len_frac:g} x median "
                   f"({med_len:.1f} m)")
            warn.warn(
                f"{sid} line {int(r['line'])}: {why} - flagged as rogue "
                "take-off/landing/stub capture; excluded from spacing and "
                "spec verdicts."
            )
    print(f"Rogue-line flagging: {int(df['rogue_line'].sum())} of "
          f"{len(df)} lines flagged.")
    return df


# ==================================================================================
def add_solar_geometry(df: pd.DataFrame) -> pd.DataFrame:
    """Add solar position, solar-noon offset, sun-line angle, clear-sky.

    Solar position/transit via pvlib SPA at each line's mid time and
    centroid. The sun-line angle is the relative solar azimuth folded to
    0-180 deg (0/180 = flying into/away from sun, 90 = sun abeam).
    Clear-sky GHI/DNI/DHI (Ineichen, climatological turbidity) is a pure
    model - a theoretical ceiling, not flight-day weather.

    Parameters
    ----------
    df : pd.DataFrame
        Flight-line table with mid_utc, centroid lat/lon, heading_deg.

    Returns
    -------
    pd.DataFrame
        Input with solar geometry and clear-sky columns added.
    """
    print("Computing solar geometry (pvlib SPA) ...")
    out = []
    for _, r in df.iterrows():
        if np.isnan(r["centroid_lat"]):
            out.append({k: np.nan for k in (
                "solar_elevation_deg", "solar_azimuth_deg", "solar_zenith_deg",
                "solar_noon_utc", "time_to_solar_noon_min",
                "sun_line_angle_deg", "clearsky_ghi_wm2", "clearsky_dni_wm2",
                "clearsky_dhi_wm2")})
            continue
        t = pd.DatetimeIndex([r["mid_utc"]])
        alt = r["ground_elev_m"] if np.isfinite(r.get("ground_elev_m", np.nan)) else 0.0
        sp = pvlib.solarposition.get_solarposition(
            t, r["centroid_lat"], r["centroid_lon"], altitude=alt
        ).iloc[0]
        # transit is returned for the timestamp's UTC date; the nearest
        # solar noon can fall on the adjacent UTC day (e.g. AEST mornings)
        days = pd.DatetimeIndex([t[0] - pd.Timedelta(days=1), t[0],
                                 t[0] + pd.Timedelta(days=1)])
        transits = pvlib.solarposition.sun_rise_set_transit_spa(
            days, r["centroid_lat"], r["centroid_lon"]
        )["transit"]
        transits = (transits.dt.tz_localize("UTC")
                    if transits.dt.tz is None else transits.dt.tz_convert("UTC"))
        transit = transits.iloc[(transits - r["mid_utc"]).abs().values.argmin()]
        loc = pvlib.location.Location(
            r["centroid_lat"], r["centroid_lon"], tz="UTC", altitude=alt
        )
        cs = loc.get_clearsky(t, model="ineichen").iloc[0]
        rel = abs((r["heading_deg"] - sp["azimuth"] + 180.0) % 360.0 - 180.0)
        out.append({
            "solar_elevation_deg": round(sp["apparent_elevation"], 2),
            "solar_azimuth_deg": round(sp["azimuth"], 2),
            "solar_zenith_deg": round(sp["apparent_zenith"], 2),
            "solar_noon_utc": transit,
            "time_to_solar_noon_min": round(
                (r["mid_utc"] - transit).total_seconds() / 60.0, 1
            ),
            "sun_line_angle_deg": round(rel, 2),
            "clearsky_ghi_wm2": round(cs["ghi"], 1),
            "clearsky_dni_wm2": round(cs["dni"], 1),
            "clearsky_dhi_wm2": round(cs["dhi"], 1),
        })
    return pd.concat([df, pd.DataFrame(out, index=df.index)], axis=1)


# ==================================================================================
def read_exposure(
    graw: Optional[pathlib.Path], mission: dict
) -> dict:
    """Hyperspec (VNIR/SWIR) exposure settings - graw-exclusive fields.

    Exposure exists only in the raw capture files: capture-level
    `settings.txt` and per-segment `raw_N.hdr` ENVI headers. Where they
    disagree, settings.txt holds the applied values and the hdr is a
    stale echo of the previous flight attempt (verified for exposure via
    the operator flight log + radiometry, and for frame period via the
    achieved per-line rate - see gryfn_exposure_hdr_vs_settings.md).
    Segments are the raw capture units and are NOT guaranteed to map 1:1
    onto gpro flight lines, so they are reported per segment. RGB is
    auto-exposed and deliberately not scraped.

    Parameters
    ----------
    graw : Optional[pathlib.Path]
        Path to the `.graw` bundle, or None if absent.
    mission : dict
        Parsed mission metadata (for `acquisitions[].data_root`).

    Returns
    -------
    dict
        Per sensor_id: capture-level settings + per-segment DataFrame
        (key "segments"), plus a source tag. Empty per-sensor dict if the
        graw is missing.
    """
    hyper = [a for a in mission.get("acquisitions", [])
             if a.get("type") in ("VNIR", "SWIR")]
    if graw is None:
        warn.warn("No .graw bundle - exposure settings reported missing.")
        return {"source": "graw (missing)", "sensors": {}}
    out = {"source": "graw", "sensors": {}}
    for acq in hyper:
        sensor_dir = graw / acq["data_root"]
        cap_dirs = sorted(
            d for d in sensor_dir.iterdir()
            if d.is_dir() and list(d.glob("raw_*.hdr"))
            and "_dark_" not in d.name.lower()   # SWIR dark captures are separate dirs
        ) if sensor_dir.is_dir() else []
        if not cap_dirs:
            warn.warn(f"{acq['sensor_id']}: no capture dirs with raw_*.hdr "
                      f"in {sensor_dir} - exposure reported missing.")
            continue
        segs = []
        settings = {}
        for cap in cap_dirs:
            settings = parse_settings_txt(cap / "settings.txt")
            for hdr in sorted(cap.glob("raw_*.hdr"),
                              key=lambda p: int(p.stem.split("_")[1])):
                segs.append({
                    "sensor_id": acq["sensor_id"],
                    "capture": cap.name,
                    "segment": int(hdr.stem.split("_")[1]),
                    **parse_hdr_exposure(hdr),
                })
        dark = dark_reference(sensor_dir, cap_dirs)
        out["sensors"][acq["sensor_id"]] = {
            "settings_txt": settings,
            "segments": pd.DataFrame(segs),
            "dark_reference": dark,
        }
        print(f"{acq['sensor_id']}: exposure from {len(segs)} raw segments "
              f"in {len(cap_dirs)} capture dir(s).")
    return out


# ==================================================================================
def dark_reference(sensor_dir: pathlib.Path, cap_dirs: list[pathlib.Path]) -> dict:
    """Dark-reference presence for a hyperspec sensor (fieldbook step).

    VNIR captures embed `darkReference.bin/.hdr` in each capture dir; SWIR
    dark frames are separate `*_dark_*` capture directories.

    Parameters
    ----------
    sensor_dir : pathlib.Path
        The sensor's `flight_1` parent directory in the graw.
    cap_dirs : list[pathlib.Path]
        Non-dark capture directories found for the sensor.

    Returns
    -------
    dict
        present (bool), acquisition_time (str | None, VNIR only).
    """
    for cap in cap_dirs:
        hdr = cap / "darkReference.hdr"
        if hdr.is_file():
            m = re.search(r"acquisition time\s*=\s*(\S+)",
                          hdr.read_text(encoding="utf-8", errors="replace"))
            return {"present": True,
                    "acquisition_time": m.group(1) if m else None}
    darks = [d for d in sensor_dir.rglob("*dark*") if d.is_dir()]
    return {"present": bool(darks), "acquisition_time": None}


# ==================================================================================
def parse_settings_txt(path: pathlib.Path) -> dict:
    """Parse capture-level fields from a graw `settings.txt`.

    Parameters
    ----------
    path : pathlib.Path
        Path to `settings.txt` in a raw capture directory.

    Returns
    -------
    dict
        exposure_ms, frame_period_ms, gain, gain_description, lens_efl_mm,
        pixel_pitch_um, aoi_width, images, lost_frames, serial, firmware,
        start_time, end_time (ISO strings), span_frame_period_ms
        (settings-window duration/images incl. inter-line gaps; None where
        images == 0, e.g. SWIR settings), or
        empty dict if the file is missing.
    """
    if not path.is_file():
        return {}
    kv = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for row in fh:
            if "=" in row:
                k, _, v = row.partition("=")
                # key spacing varies between VNIR and SWIR settings files
                kv[k.strip().replace(" ", "").lower()] = v.strip()

    def num(key: str, cast=float):
        return cast(kv[key]) if kv.get(key) else None

    # VNIR: "2026/Aug/04 23:56:54.821094"; SWIR: "2026-Apr-15 01:29:54.321"
    start = pd.to_datetime(kv.get("starttime"), errors="coerce")
    end = pd.to_datetime(kv.get("endtime"), errors="coerce")
    images = num("images", int)
    # spans the settings capture window incl. inter-line gaps - the
    # achieved rate lives per line in flight_lines.csv
    span_fp = None
    if images and pd.notna(start) and pd.notna(end) and end > start:
        span_fp = round((end - start).total_seconds() * 1000.0 / images, 4)
    return {
        "exposure_ms": num("exposure(ms)"),
        "frame_period_ms": num("frameperiod(ms)"),
        "gain": kv.get("gain"),
        "gain_description": kv.get("gaindescription"),
        "lens_efl_mm": num("lensefl(mm)"),
        "pixel_pitch_um": num("arraypixelpitch(um)"),
        "aoi_width": num("aoiwidth", int),
        "images": images,
        "lost_frames": num("lostframes", int),
        "serial": kv.get("serialnumber"),
        "firmware": kv.get("version") or kv.get("firmware_version"),
        "start_time": str(start) if pd.notna(start) else None,
        "end_time": str(end) if pd.notna(end) else None,
        "span_frame_period_ms": span_fp,
    }


# ==================================================================================
def parse_hdr_exposure(path: pathlib.Path) -> dict:
    """Parse per-segment exposure fields from a `raw_N.hdr` ENVI header.

    Headers can contain non-UTF8 bytes; read with errors="replace".

    Parameters
    ----------
    path : pathlib.Path
        Path to a `raw_N.hdr` file.

    Returns
    -------
    dict
        exposure_ms, frame_period_ms, analog_gain, digital_gain (float or
        NaN where absent).
    """
    pats = {
        "exposure_ms": re.compile(r"^;?\s*Exposure \(ms\)\s*=\s*([\d.]+)", re.I),
        "frame_period_ms": re.compile(r"^;?\s*Frame Period\(ms\)\s*=\s*([\d.]+)", re.I),
        "analog_gain": re.compile(r"^;?\s*analog gain\s*=\s*([\d.]+)", re.I),
        "digital_gain": re.compile(r"^;?\s*digital gain\s*=\s*([\d.]+)", re.I),
        "gain_mode": re.compile(r"^;?\s*gain\s*=\s*([\d.]+)", re.I),  # SWIR hdrs
    }
    vals = {k: np.nan for k in pats}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for row in fh:
            for key, pat in pats.items():
                m = pat.match(row)
                if m:
                    vals[key] = float(m.group(1))
    return vals


# ==================================================================================
def add_coverage(df: pd.DataFrame, exposure: dict) -> pd.DataFrame:
    """Add fieldbook coverage/sampling columns per flight line.

    Uses sensor optics from settings.txt (lens EFL, pixel pitch, AOI
    width) to estimate swath and cross-track GSD, line-centroid spacing
    to estimate sidelap (fieldbook: VNIR >= 37-40 %, SWIR > 30 %), and
    frame period vs speed/GSD to estimate along-track oversampling
    (fieldbook: >= 20 %, default 30 %). Oversampling is reported for both
    the configured (settings.txt) and achieved (Images / capture
    duration) frame period - the two can differ badly (see
    gryfn_exposure_hdr_vs_settings.md).

    Parameters
    ----------
    df : pd.DataFrame
        Flight-line table with agl_m, heading_deg, centroids, speed.
    exposure : dict
        Output of :func:`read_exposure` (per-sensor settings.txt optics).

    Returns
    -------
    pd.DataFrame
        Input with line_spacing_m, est_swath_m, est_sidelap_pct,
        oversampling_configured_pct, oversampling_actual_pct columns.
    """
    for col in ("line_spacing_m", "est_swath_m", "est_sidelap_pct",
                "oversampling_configured_pct", "oversampling_actual_pct"):
        df[col] = np.nan
    for sid, rec in exposure.get("sensors", {}).items():
        st = rec.get("settings_txt") or {}
        efl, pitch, width = (st.get("lens_efl_mm"),
                             st.get("pixel_pitch_um"), st.get("aoi_width"))
        mask = df["sensor_id"] == sid
        if not mask.any() or not (efl and pitch and width):
            continue
        ifov = (pitch * 1e-6) / (efl * 1e-3)          # rad per pixel
        agl = df.loc[mask, "agl_m"]
        df.loc[mask, "est_swath_m"] = np.round(
            2.0 * agl * np.tan(width * ifov / 2.0), 2)
        # spacing: centroid separation perpendicular to the mean heading;
        # rogue take-off/landing lines are excluded (they sit between or
        # below real lines and corrupt their neighbours' spacing)
        sub = df.loc[mask & ~df["rogue_line"]]
        if len(sub) > 1:
            lat0 = sub["centroid_lat"].mean()
            x = (sub["centroid_lon"] - sub["centroid_lon"].mean()) \
                * np.cos(np.deg2rad(lat0)) * 111320.0
            y = (sub["centroid_lat"] - lat0) * 110540.0
            head = np.deg2rad((sub["heading_deg"] % 180.0).median())
            # unit vector perpendicular to flight direction (E,N frame)
            perp = np.array([-np.cos(head), np.sin(head)])
            pos = x.values * perp[0] + y.values * perp[1]
            order = np.argsort(pos)
            gaps = np.diff(pos[order])
            spacing = np.full(len(sub), np.nan)
            for k, oi in enumerate(order):
                cand = [g for g in (gaps[k - 1] if k > 0 else np.nan,
                                    gaps[k] if k < len(gaps) else np.nan)
                        if np.isfinite(g)]
                spacing[oi] = min(cand) if cand else np.nan
            df.loc[sub.index, "line_spacing_m"] = np.round(np.abs(spacing), 2)
        df.loc[mask, "est_sidelap_pct"] = np.round(
            (1.0 - df.loc[mask, "line_spacing_m"]
             / df.loc[mask, "est_swath_m"]) * 100.0, 1)
        # along-track oversampling: cross-track GSD vs speed * frame period
        gsd = agl * ifov
        speed = df.loc[mask, "ground_speed_ms"]
        fp_cfg = st.get("frame_period_ms")
        if fp_cfg:
            df.loc[mask, "oversampling_configured_pct"] = np.round(
                (gsd / (speed * fp_cfg / 1000.0) - 1.0) * 100.0, 1)
        fp_ach = df.loc[mask, "achieved_frame_period_ms"]
        df.loc[mask, "oversampling_actual_pct"] = np.round(
            (gsd / (speed * fp_ach / 1000.0) - 1.0) * 100.0, 1)
    return df


# ==================================================================================
def load_flightcal_spec(
    path: pathlib.Path,
) -> Tuple[Optional[dict], Optional[Dict[str, Any]]]:
    """Load the FlightCal spec/thresholds YAML with its config snapshot.

    Parameters
    ----------
    path : pathlib.Path
        Path to `flightcal_spec.yml` (repo-relative by default).

    Returns
    -------
    tuple
        ``(spec, snapshot)`` — the parsed spec plus the ``{"path",
        "sha256"}`` provenance snapshot for the detail JSON, or
        ``(None, None)`` if the file is missing (check skipped with a
        warning).
    """
    if not path.is_file():
        warn.warn(f"FlightCal spec {path} missing - within-spec check skipped.")
        return None, None
    loaded = qr.load_thresholds(path.name, thresholds_dir=path.parent)
    spec = loaded["spec"]
    print(f"Loaded FlightCal spec: {spec.get('source')}")
    return spec, {"path": loaded["path"], "sha256": loaded["sha256"]}


# ==================================================================================
def add_spec_check(
    df: pd.DataFrame, exposure: dict, mission: dict, spec: Optional[dict]
) -> tuple[pd.DataFrame, dict]:
    """Classify as-flown parameters against FlightCal/fieldbook thresholds.

    Re-runs the GRYFN Flight Calculator's equations on the as-flown
    values (AGL, ground speed, line spacing, settings.txt optics) and
    classifies each metric under two threshold sets: the calculator's
    conditional-formatting breakpoints and the stricter APEx fieldbook
    targets. Statuses: good / acceptable / warning / fail (fail = hard
    constraint: negative sidelap = coverage gap, or frame period below
    the sensor minimum = physically unachievable) / not_checked (inputs
    missing). LiDAR reuses the hyperspec line spacing/AGL - the platform
    flies one pattern for all sensors.

    Parameters
    ----------
    df : pd.DataFrame
        Flight-line table (after :func:`add_coverage`).
    exposure : dict
        Output of :func:`read_exposure` (settings.txt optics per sensor).
    mission : dict
        Parsed mission metadata (acquisition types, LiDAR sensor_id).
    spec : Optional[dict]
        Parsed spec from :func:`load_flightcal_spec`, or None to skip.

    Returns
    -------
    tuple[pd.DataFrame, dict]
        ``df`` with gsd_cm, LiDAR swath/sidelap and per-metric status
        columns added, and the `spec_check` report block (per-sensor
        summaries + worst-line verdicts under both threshold sets).
    """
    df["gsd_cm"] = np.nan
    df["lidar_swath_m"] = np.nan
    df["lidar_sidelap_pct"] = np.nan
    status_cols = ("gsd_status", "frame_rate_status", "sidelap_status_calc",
                   "sidelap_status_fieldbook", "oversampling_status_fieldbook",
                   "lidar_sidelap_status")
    for col in status_cols:
        df[col] = "not_checked"
    if spec is None:
        return df, {"source": "flightcal spec missing - not checked"}
    print("Running within-spec check ...")
    thr_c = spec["thresholds"]["calculator"]
    thr_f = spec["thresholds"]["fieldbook"]
    acq_type = {a.get("sensor_id"): a.get("type")
                for a in mission.get("acquisitions", [])}
    report = {
        "source": spec.get("source"),
        "note": "actuals-only check: calculator equations re-run on as-flown "
                "AGL/speed/line spacing. Calculator verdict covers GSD + "
                "frame-rate limits + sidelap; fieldbook verdict covers "
                "sidelap + oversampling targets. fail = hard constraint "
                "(negative sidelap / frame period below sensor minimum)",
        "status_scale": ["good", "acceptable", "warning", "fail"],
        "linescan": {},
        "lidar": {"source": "no LiDAR acquisition"},
    }
    cfg_statuses = []
    # ========== line-scan sensors ==========
    for sid, rec in exposure.get("sensors", {}).items():
        mask = df["sensor_id"] == sid
        if not mask.any():
            continue
        ls = (spec.get("linescan_sensors") or {}).get(sid.split("-")[0])
        if ls is None:
            warn.warn(f"{sid}: no line-scan entry in the FlightCal spec - "
                      "not checked.")
            continue
        st = rec.get("settings_txt") or {}
        # +++++ cross-track GSD from as-flown AGL + settings.txt optics +++++
        pitch = st.get("pixel_pitch_um") or ls["pixel_size_um"]
        efl = st.get("lens_efl_mm")
        if efl:
            df.loc[mask, "gsd_cm"] = np.round(
                linescan_gsd_cm(pitch, df.loc[mask, "agl_m"], efl), 3)
        g = thr_c["linescan_gsd_cm"]
        df.loc[mask, "gsd_status"] = [
            classify_low(v, g["good_max"], g["warn_min"])
            for v in df.loc[mask, "gsd_cm"]]
        # +++++ frame rate vs sensor hard limits +++++
        fr = thr_c["frame_rate_frac_of_max"]
        frac = (1000.0
                / df.loc[mask, "achieved_frame_period_ms"].replace(0, np.nan)
                / ls["max_frame_rate_hz"])
        df.loc[mask, "frame_rate_status"] = [
            classify_low(v, fr["good_max"], fr["warn_min"], fr.get("fail_above"))
            for v in frac]
        cfg_fp = st.get("frame_period_ms")
        cfg_status = classify_low(
            1000.0 / cfg_fp / ls["max_frame_rate_hz"] if cfg_fp else np.nan,
            fr["good_max"], fr["warn_min"], fr.get("fail_above"))
        cfg_statuses.append(cfg_status)
        # +++++ sidelap under both threshold sets +++++
        sc = thr_c["linescan_sidelap_pct"]
        df.loc[mask, "sidelap_status_calc"] = [
            classify_high(v, sc["good_min"], sc["accept_min"],
                          sc.get("fail_below"))
            for v in df.loc[mask, "est_sidelap_pct"]]
        sf = (thr_f.get("sidelap_pct") or {}).get(acq_type.get(sid))
        if sf:
            df.loc[mask, "sidelap_status_fieldbook"] = [
                classify_high(v, sf["good_min"], sf["accept_min"],
                              sf.get("fail_below"))
                for v in df.loc[mask, "est_sidelap_pct"]]
        ov = thr_f["oversampling_pct"]
        df.loc[mask, "oversampling_status_fieldbook"] = [
            classify_high(v, ov["good_min"], ov["accept_min"],
                          ov.get("fail_below"))
            for v in df.loc[mask, "oversampling_actual_pct"]]
        sub = df.loc[mask]
        report["linescan"][sid] = {
            "calculator_name": ls.get("calculator_name"),
            "gsd_cm_range": [_nanfloat(sub["gsd_cm"].min()),
                             _nanfloat(sub["gsd_cm"].max())],
            "gsd_status": _worst(sub["gsd_status"]),
            "configured_frame_period_ms": cfg_fp,
            "min_frame_period_ms": ls["min_frame_period_ms"],
            "max_frame_rate_hz": ls["max_frame_rate_hz"],
            "configured_frame_rate_status": cfg_status,
            "achieved_frame_rate_status": _worst(sub["frame_rate_status"]),
            "sidelap_pct_range": [_nanfloat(sub["est_sidelap_pct"].min()),
                                  _nanfloat(sub["est_sidelap_pct"].max())],
            "sidelap_status_calculator": _worst(sub["sidelap_status_calc"]),
            "sidelap_status_fieldbook": _worst(sub["sidelap_status_fieldbook"]),
            "oversampling_status_fieldbook": _worst(
                sub["oversampling_status_fieldbook"]),
        }
    # ========== LiDAR (hyperspec line spacing/AGL; one flight pattern) =====
    lid = next((a for a in mission.get("acquisitions", [])
                if a.get("type") == "LiDAR"), None)
    if lid is not None:
        lrec = (spec.get("lidar_sensors") or {}).get(lid.get("sensor_id"))
        if lrec is None:
            warn.warn(f"{lid.get('sensor_id')}: no LiDAR entry in the "
                      "FlightCal spec - not checked.")
            report["lidar"] = {"sensor_id": lid.get("sensor_id"),
                               "source": "not in spec - not checked"}
        else:
            rmode = (spec.get("assumptions") or {}).get("lidar_return_mode", 2)
            m = lidar_line_metrics(lrec, df["agl_m"], df["ground_speed_ms"],
                                   df["line_spacing_m"], rmode)
            df["lidar_swath_m"] = np.round(m["hfov_m"], 2)
            df["lidar_sidelap_pct"] = np.round(m["sidelap_pct"], 1)
            sl = thr_c["lidar_sidelap_pct"]
            df["lidar_sidelap_status"] = [
                classify_high(v, sl["good_min"], sl["accept_min"],
                              sl.get("fail_below"))
                for v in df["lidar_sidelap_pct"]]
            report["lidar"] = {
                "sensor_id": lid.get("sensor_id"),
                "calculator_name": lrec.get("calculator_name"),
                "corrections_applied": lrec.get("corrections") or None,
                "sidelap_pct_range": [_nanfloat(df["lidar_sidelap_pct"].min()),
                                      _nanfloat(df["lidar_sidelap_pct"].max())],
                "sidelap_status": _worst(df["lidar_sidelap_status"]),
                "est_points_per_s": _nanfloat(m["points_per_s"]),
                "est_point_density_single_pts_m2": _nanfloat(
                    np.nanmedian(m["density_single_pts_m2"])),
                "est_point_density_overlap_pts_m2": _nanfloat(
                    np.nanmedian(m["density_overlap_pts_m2"])),
                "density_note": "ESTIMATE (per-line median, FlightCal "
                                f"section 2.2 formula); return mode {rmode} "
                                "assumed - not recorded in the bundles",
            }
    # ========== rogue lines: excluded from verdicts, marked in statuses ====
    if df["rogue_line"].any():
        for col in status_cols:
            df.loc[df["rogue_line"], col] = "rogue_line"
    report["rogue_lines"] = {
        "n_excluded": int(df["rogue_line"].sum()),
        "lines": [
            {"sensor_id": r["sensor_id"], "line": int(r["line"]),
             "agl_m": _nanfloat(r["agl_m"]),
             "line_length_m": _nanfloat(r["line_length_m"])}
            for _, r in df[df["rogue_line"]].iterrows()
        ] or None,
        "note": "take-off/landing/stub captures (AGL below --rogue-agl-frac "
                "x sensor median, or length below --rogue-len-frac x median); "
                "excluded from line-spacing estimation and spec verdicts",
    }
    # ========== verdicts: worst line wins, configured frame status included ==
    calc_vals = (list(df["gsd_status"]) + list(df["frame_rate_status"])
                 + list(df["sidelap_status_calc"])
                 + list(df["lidar_sidelap_status"]) + cfg_statuses)
    fb_vals = (list(df["sidelap_status_fieldbook"])
               + list(df["oversampling_status_fieldbook"]))
    report["verdict_calculator"] = _worst(calc_vals)
    report["verdict_fieldbook"] = _worst(fb_vals)
    print(f"Spec check: calculator={report['verdict_calculator']}, "
          f"fieldbook={report['verdict_fieldbook']}")
    return df, report


# ==================================================================================
def linescan_gsd_cm(pixel_pitch_um: float, agl_m, efl_mm: float):
    """Cross-track line-scan GSD (FlightCal section 2.1: `p * H / f * 0.1`).

    Parameters
    ----------
    pixel_pitch_um : float
        Detector pixel pitch (um).
    agl_m : float | pd.Series
        Height above ground (m).
    efl_mm : float
        Lens effective focal length (mm).

    Returns
    -------
    float | pd.Series
        Ground sampling distance in cm.
    """
    return pixel_pitch_um * agl_m / efl_mm * 0.1


# ==================================================================================
def lidar_line_metrics(spec_rec: dict, agl_m, speed_ms, spacing_m,
                       return_mode: int = 2) -> dict:
    """LiDAR swath, sidelap and point-density estimate (FlightCal s. 2.2).

    Applies the spec entry's `corrections` overlay (e.g. the VLP-16
    vertical FoV missing from the workbook) before evaluating. Points/s
    uses the workbook's horizontal-resolution path (Ouster) or the
    pulses-per-channel path (Velodyne, where the workbook's horizontal
    resolution is N/A). Inputs may be scalars or aligned Series/arrays.

    Parameters
    ----------
    spec_rec : dict
        LiDAR entry from `flightcal_spec.yml`.
    agl_m, speed_ms, spacing_m : float | pd.Series
        As-flown height AGL, ground speed and line spacing.
    return_mode : int
        1 = single return, 2 = dual (default; not recorded in bundles).

    Returns
    -------
    dict
        points_per_s (scalar), hfov_m, vfov_m, sidelap_pct,
        density_single_pts_m2, density_overlap_pts_m2 (input-shaped).
    """
    eff = {**spec_rec, **(spec_rec.get("corrections") or {})}
    hfov_m = 2.0 * agl_m * np.tan(np.deg2rad(eff["hfov_deg"]) / 2.0)
    vfov_m = (2.0 * agl_m * np.tan(np.deg2rad(eff["vfov_deg"]) / 2.0)
              if eff.get("vfov_deg") else np.nan)
    frac = eff["hfov_deg"] / 360.0
    if eff.get("horizontal_resolution"):
        pts = (frac * eff["rotation_rate_hz"] * eff["horizontal_resolution"]
               * eff["channels"] * return_mode)
    elif eff.get("pulses_per_s_per_channel"):
        pts = (eff["pulses_per_s_per_channel"] * eff["channels"] * frac
               * return_mode)
    else:
        pts = np.nan
    sidelap = (hfov_m - spacing_m) / hfov_m
    density = pts / (hfov_m * (vfov_m + speed_ms))
    return {
        "points_per_s": pts,
        "hfov_m": hfov_m,
        "vfov_m": vfov_m,
        "sidelap_pct": sidelap * 100.0,
        "density_single_pts_m2": density,
        "density_overlap_pts_m2": density * (1.0 + 2.0 * sidelap),
    }


# ==================================================================================
def classify_low(value: float, good_max: float, warn_min: float,
                 fail_above: Optional[float] = None) -> str:
    """Classify a lower-is-better metric against FlightCal breakpoints.

    Parameters
    ----------
    value : float
        Metric value (NaN/None -> "not_checked").
    good_max : float
        Values <= this are "good".
    warn_min : float
        Values >= this are "warning"; between the two is "acceptable".
    fail_above : Optional[float]
        Hard limit; values above it are "fail".

    Returns
    -------
    str
        good | acceptable | warning | fail | not_checked.
    """
    if value is None or not np.isfinite(value):
        return "not_checked"
    if fail_above is not None and value > fail_above:
        return "fail"
    if value <= good_max:
        return "good"
    if value < warn_min:
        return "acceptable"
    return "warning"


# ==================================================================================
def classify_high(value: float, good_min: float, accept_min: float,
                  fail_below: Optional[float] = None) -> str:
    """Classify a higher-is-better metric against FlightCal breakpoints.

    Parameters
    ----------
    value : float
        Metric value (NaN/None -> "not_checked").
    good_min : float
        Values >= this are "good".
    accept_min : float
        Values >= this are "acceptable"; below is "warning".
    fail_below : Optional[float]
        Hard limit; values below it are "fail" (e.g. negative sidelap
        = coverage gap).

    Returns
    -------
    str
        good | acceptable | warning | fail | not_checked.
    """
    if value is None or not np.isfinite(value):
        return "not_checked"
    if fail_below is not None and value < fail_below:
        return "fail"
    if value >= good_min:
        return "good"
    if value >= accept_min:
        return "acceptable"
    return "warning"


# ==================================================================================
def _worst(statuses) -> str:
    """Worst status in an iterable, ignoring "not_checked".

    Parameters
    ----------
    statuses : Iterable[str]
        Status strings from the classify helpers.

    Returns
    -------
    str
        The worst status present, or "not_checked" if none were checked.
    """
    order = ("good", "acceptable", "warning", "fail")
    idx = [order.index(s) for s in statuses if s in order]
    return order[max(idx)] if idx else "not_checked"


# ==================================================================================
def read_trigger(graw: Optional[pathlib.Path], mission: dict) -> dict:
    """Capture-trigger settings from `gpsMonitor.json` (graw-exclusive).

    Fieldbook: triggering by polygon with a minimum altitude trigger of
    at least 20 m recommended.

    Parameters
    ----------
    graw : Optional[pathlib.Path]
        Path to the `.graw` bundle, or None if absent.
    mission : dict
        Parsed mission metadata (for `acquisitions[].data_root`).

    Returns
    -------
    dict
        enabled, min/max altitude triggers, n_polygons, polygon vertex
        count, and a source tag; fields None where unavailable.
    """
    missing = {"enabled": None, "min_altitude_m": None, "max_altitude_m": None,
               "n_polygons": None, "polygon_vertices": None,
               "source": "graw (missing)"}
    if graw is None:
        return missing
    hyper = [a for a in mission.get("acquisitions", [])
             if a.get("type") in ("VNIR", "SWIR")]
    for acq in hyper:
        sensor_dir = graw / acq["data_root"]
        if not sensor_dir.is_dir():
            continue
        for gm in sorted(sensor_dir.rglob("gpsMonitor.json")):
            try:
                with open(gm, encoding="utf-8", errors="replace") as fh:
                    j = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            polys = j.get("polygons") or []
            return {
                "enabled": j.get("enabled"),
                "min_altitude_m": j.get("minAltitude"),
                "max_altitude_m": j.get("maxAltitude"),
                "n_polygons": len(polys),
                "polygon_vertices": (len(polys[0].get("array", [])) // 2
                                     if polys else 0),
                "source": "graw",
            }
    return missing


# ==================================================================================
def read_lever_arms(graw: Optional[pathlib.Path], mission: dict) -> dict:
    """GNSS/INS lever-arm configuration (graw-exclusive).

    The gpro carries no lever-arm record, so this scrapes the raw GNSS
    payload, keyed on `mission_data.yaml -> acquisitions[].type == "GNSS"`.
    Two vendor layouts exist:

    - SBG (GOBI): session settings dump
      `<GNSS>/flight_1/<session>/<name>_0001.json` ->
      `settings.aiding.gnss1.leverArmPrimary/-Secondary` (IMU -> antenna,
      body frame x-fwd/y-right/z-down) plus dual-antenna heading mode.
    - Applanix APX-15 (CALVIS): POSPac cloud logs under
      `<GNSS>/flight_1/cloud_*/` report `Reference to Primary GNSS lever
      arm` and `Reference to IMU lever arm`.

    Lever arms are rig-mount facts, so they double as an airframe proxy:
    the antenna offsets differ wildly between the IF1200 and DJI M350
    mounts, which no other scraped field records. The record therefore
    carries a ``likely_airframe`` inferred via :func:`_infer_airframe`.

    Parameters
    ----------
    graw : Optional[pathlib.Path]
        Path to the `.graw` bundle, or None if absent.
    mission : dict
        Parsed mission metadata (for `acquisitions[].data_root`).

    Returns
    -------
    dict
        vendor, source file, GNSS primary/secondary lever arms, IMU/COG
        lever arms, GNSS lever-arm standard deviations, heading mode,
        likely airframe (+ basis), and a source tag; fields None where
        unavailable.
    """
    missing = {"vendor": None, "file": None,
               "gnss_primary_lever_arm_m": None,
               "gnss_secondary_lever_arm_m": None,
               "imu_lever_arm_m": None, "cog_lever_arm_m": None,
               "gnss_lever_arm_sd_m": None, "heading_mode": None,
               "likely_airframe": None, "airframe_basis": None,
               "source": "graw (missing)"}
    if graw is None:
        return missing
    for acq in mission.get("acquisitions", []):
        if acq.get("type") != "GNSS":
            continue
        gnss_dir = graw / acq["data_root"].replace("\\", "/")
        if not gnss_dir.is_dir():
            continue
        rec = _sbg_lever_arms(gnss_dir, graw) or _apx_lever_arms(gnss_dir, graw)
        if rec:
            rec["likely_airframe"], rec["airframe_basis"] = _infer_airframe(
                rec["gnss_primary_lever_arm_m"])
            return rec
    return missing


# ==================================================================================
def _known_gnss_mounts() -> list[dict]:
    """Known GNSS-antenna mount signatures and their airframes.

    Grounded in the vendor SystemCal `uav:` declarations
    (`data/syscal/<node>/*_SystemCal.yml`): GOBI2403/2404/2410 are
    declared DJI M350-only and share the wide dual-antenna crossbar
    signature; every cAHP (CALVIS) rig is declared IF1200A-only. The
    DPIRD GOBI value matches the same deep-z mast geometry as the
    CALVIS/IF1200A mount. Ambiguous SystemCals (USYD/DPIRD GOBI say
    "DJI M350 / IF1200A") are exactly why the per-flight lever arm is
    the discriminator.

    Returns
    -------
    list[dict]
        signature (primary GNSS lever arm, m), airframe, mount label.
    """
    return [
        {"signature": (-0.198, -0.301, -0.199), "airframe": "DJI M350",
         "mount": "GOBI dual-antenna crossbar"},
        {"signature": (0.213, -0.089, -0.436),
         "airframe": "Inspired Flight IF1200A",
         "mount": "CALVIS/APX mast"},
        {"signature": (0.168, -0.080, -0.448),
         "airframe": "Inspired Flight IF1200A",
         "mount": "GOBI IF1200 mast"},
    ]


# ==================================================================================
def _infer_airframe(
    primary: Optional[list],
) -> tuple[Optional[str], Optional[str]]:
    """Match a primary GNSS lever arm against known mount signatures.

    Parameters
    ----------
    primary : Optional[list]
        Primary GNSS lever arm [x, y, z] in metres, or None.

    Returns
    -------
    tuple[Optional[str], Optional[str]]
        (likely_airframe, basis). ``("unrecognised mount", ...)`` when no
        signature matches within tolerance; ``(None, None)`` when no
        lever arm was scraped.
    """
    tol = 0.06  # per-axis (m); clusters are >0.2 m apart on y or z
    if not primary:
        return None, None
    for m in _known_gnss_mounts():
        if all(abs(float(a) - b) <= tol
               for a, b in zip(primary, m["signature"])):
            return (m["airframe"],
                    f"primary GNSS lever arm within {tol} m of the "
                    f"{m['mount']} signature {list(m['signature'])}")
    return ("unrecognised mount",
            f"no known mount signature within {tol} m of {primary}")


# ==================================================================================
def airframe_from_system_cal(
    lever: dict, gpro: pathlib.Path, graw: Optional[pathlib.Path]
) -> dict:
    """Fall back to the SystemCal `uav:` declaration for the airframe.

    Only used when no lever arm was scraped (e.g. empty GNSS intake or
    missing graw), and only decisive when the rig's SystemCal declares
    exactly one airframe. Ambiguous declarations like "DJI M350/IF1200A"
    stay unknown - resolving those is what the lever arm is for.

    Parameters
    ----------
    lever : dict
        Record from :func:`read_lever_arms` (returned unchanged if it
        already carries an airframe).
    gpro : pathlib.Path
        Path to the `.gpro` bundle (carries a SystemCal copy).
    graw : Optional[pathlib.Path]
        Path to the `.graw` bundle, or None.

    Returns
    -------
    dict
        ``lever`` with likely_airframe/airframe_basis filled where the
        declaration is unambiguous.
    """
    if lever.get("likely_airframe") is not None:
        return lever
    for bundle in (b for b in (gpro, graw) if b is not None):
        for sc in sorted(bundle.glob("*_SystemCal.yml")):
            try:
                with open(sc, encoding="utf-8", errors="replace") as fh:
                    cal = yaml.safe_load(fh) or {}
            except (yaml.YAMLError, OSError):
                continue
            uav = cal.get("uav")
            if isinstance(uav, list):
                uav = uav[0] if uav else {}
            uav_id = (uav or {}).get("id") or ""
            # multi-airframe declarations ("A / B") are not decisive
            if uav_id and "/" not in uav_id:
                lever["likely_airframe"] = uav_id
                lever["airframe_basis"] = (
                    f"no lever arm scraped; single-airframe uav "
                    f"declaration in {sc.name}")
                return lever
    return lever


# ==================================================================================
def _sbg_lever_arms(
    gnss_dir: pathlib.Path, graw: pathlib.Path
) -> Optional[dict]:
    """Lever arms from an SBG Quanta session settings JSON.

    Parameters
    ----------
    gnss_dir : pathlib.Path
        GNSS acquisition folder inside the graw (e.g. `SBG/`).
    graw : pathlib.Path
        The `.graw` bundle root (for relative source paths).

    Returns
    -------
    Optional[dict]
        Lever-arm record, or None if no SBG settings dump is found.
    """
    for jf in sorted(gnss_dir.rglob("*.json")):
        # project/ holds Qinertia processing state, not the settings dump
        if "project" in jf.relative_to(gnss_dir).parts:
            continue
        try:
            with open(jf, encoding="utf-8", errors="replace") as fh:
                j = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        settings = j.get("settings") or {}
        g1 = (settings.get("aiding") or {}).get("gnss1") or {}
        if "leverArmPrimary" not in g1:
            continue
        mech = settings.get("mechanicalSetup") or {}
        return {
            "vendor": "SBG",
            "file": str(jf.relative_to(graw)),
            "gnss_primary_lever_arm_m": g1.get("leverArmPrimary"),
            "gnss_secondary_lever_arm_m": g1.get("leverArmSecondary"),
            "imu_lever_arm_m": None,
            "cog_lever_arm_m": (mech.get("leverArms") or {}).get("cog"),
            "gnss_lever_arm_sd_m": None,
            "heading_mode": g1.get("headingMode"),
            "source": "graw",
        }
    return None


# ==================================================================================
def _apx_lever_arms(
    gnss_dir: pathlib.Path, graw: pathlib.Path
) -> Optional[dict]:
    """Lever arms from Applanix POSPac cloud-processing logs.

    Parameters
    ----------
    gnss_dir : pathlib.Path
        GNSS acquisition folder inside the graw (e.g. `APX-15/`).
    graw : pathlib.Path
        The `.graw` bundle root (for relative source paths).

    Returns
    -------
    Optional[dict]
        Lever-arm record, or None if no log reports a GNSS lever arm.
    """
    triple = r":\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)"
    pat_gnss = re.compile(
        r"reference[- ](?:to[- ])?primary GNSS lever arm\s*" + triple,
        re.IGNORECASE)
    pat_imu = re.compile(
        r"reference[- ](?:to[- ])?IMU lever arm\s*" + triple, re.IGNORECASE)
    pat_sd = re.compile(
        r"primary GNSS lever arm standard deviations\s*" + triple,
        re.IGNORECASE)
    for lf in sorted(gnss_dir.rglob("*.log")):
        try:
            if lf.stat().st_size > 10_000_000:
                continue
            text = lf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = pat_gnss.search(text)
        if not m:
            continue
        m_imu = pat_imu.search(text)
        m_sd = pat_sd.search(text)
        return {
            "vendor": "Applanix",
            "file": str(lf.relative_to(graw)),
            "gnss_primary_lever_arm_m": [float(v) for v in m.groups()],
            "gnss_secondary_lever_arm_m": None,
            "imu_lever_arm_m": ([float(v) for v in m_imu.groups()]
                                if m_imu else None),
            "cog_lever_arm_m": None,
            "gnss_lever_arm_sd_m": ([float(v) for v in m_sd.groups()]
                                    if m_sd else None),
            "heading_mode": None,
            "source": "graw",
        }
    return None


# ==================================================================================
def panel_presence(gpro: pathlib.Path) -> dict:
    """Reflectance-panel picks scraped from the gpro pipeline YAML.

    Reads the newest ``pipelines/*.yml`` in the bundle and collects the
    per-sensor-task ``hsi_preprocessing`` blocks — the operator's actual
    panel picks used for the ELM. Target-file IDs are NOT unique physical
    panels: two panel sets with identical values share one target json
    (normal). Distinct source frames per sensor are the dual-ELM tell
    (panels captured at flight start and end).

    Parameters
    ----------
    gpro : pathlib.Path
        Path to the `.gpro` bundle (also checked for a `_NoPanels`
        name suffix).

    Returns
    -------
    dict
        panels_present (bool | None; None = no pipeline yml),
        n_panel_detections (total picks across sensors),
        n_target_files / target_files (unique target json basenames),
        sensors (per sensor-task: n_picks, target_files,
        n_capture_frames, capture_frames), no_panels_suffix (bool),
        source tag.
    """
    no_panels = gpro.stem.endswith("_NoPanels")
    ymls = sorted((gpro / "pipelines").glob("*.yml"))
    if not ymls:
        return {"panels_present": None, "n_panel_detections": None,
                "target_files": None, "n_target_files": None,
                "sensors": {}, "no_panels_suffix": no_panels,
                "source": "gpro (no pipeline yml)"}
    # timestamp-prefixed names sort chronologically - newest wins
    with open(ymls[-1], encoding="utf-8") as fh:
        pipeline = yaml.safe_load(fh)
    tasks = (pipeline.get("tasks") or []) if isinstance(pipeline, dict) else []
    sensors: Dict[str, dict] = {}
    all_files: set = set()
    total = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        picks = (task.get("configuration") or {}).get("hsi_preprocessing")
        if not isinstance(picks, list) or not picks:
            continue
        files = sorted({
            re.split(r"[\\/]", str(p.get("target_location", "")))[-1]
            .removesuffix(".json")
            for p in picks if isinstance(p, dict) and p.get("target_location")
        })
        frames = sorted({
            (p.get("flight"),
             re.split(r"[\\/]", str(p.get("image", "")))[-1])
            for p in picks if isinstance(p, dict) and p.get("image")
        })
        name = str(task.get("type") or task.get("sensor_id") or "unknown")
        sensors[name] = {
            "sensor_id": task.get("sensor_id"),
            "n_picks": len(picks),
            "target_files": files,
            "n_capture_frames": len(frames),
            "capture_frames": [f"flight_{fl}:{img}" for fl, img in frames],
        }
        all_files.update(files)
        total += len(picks)
    return {"panels_present": total > 0, "n_panel_detections": total,
            "target_files": sorted(all_files),
            "n_target_files": len(all_files),
            "sensors": sensors, "no_panels_suffix": no_panels,
            "source": f"gpro (pipelines/{ymls[-1].name})"}


# ==================================================================================
def write_outputs(
    out_dir: pathlib.Path,
    gpro: pathlib.Path,
    graw: Optional[pathlib.Path],
    mission: dict,
    df: pd.DataFrame,
    panels: dict,
    exposure: dict,
    run_id: Optional[str] = None,
    trigger: Optional[dict] = None,
    lever: Optional[dict] = None,
    spec_report: Optional[dict] = None,
) -> dict:
    """Write flight_lines.csv, exposure_segments.csv, run_summary.csv.

    The full acquisition report is returned (not written as its own
    file) — it is embedded in the contract detail JSON by
    :func:`build_contract_report`.

    Parameters
    ----------
    out_dir : pathlib.Path
        Output directory for this run (created if needed).
    gpro : pathlib.Path
        Path to the `.gpro` bundle.
    graw : Optional[pathlib.Path]
        Path to the `.graw` bundle, or None.
    mission : dict
        Parsed mission metadata.
    df : pd.DataFrame
        Completed flight-line table.
    panels : dict
        Panel-presence record from :func:`panel_presence`.
    exposure : dict
        Hyperspec exposure record from :func:`read_exposure`.
    run_id : Optional[str]
        Run identifier for the outputs (defaults to the gpro bundle stem).
    trigger : Optional[dict]
        Capture-trigger record from :func:`read_trigger`.
    lever : Optional[dict]
        Lever-arm record from :func:`read_lever_arms`.
    spec_report : Optional[dict]
        Within-spec report block from :func:`add_spec_check`.

    Returns
    -------
    dict
        The full acquisition report (per-field source tags).
    """
    run_id = run_id or gpro.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_dir / "flight_lines.csv", index=False)
    # run-level aggregates exclude rogue take-off/landing lines
    good = df[~df["rogue_line"]] if df["rogue_line"].any() else df
    seg_frames = [s["segments"] for s in exposure["sensors"].values()
                  if len(s["segments"])]
    if seg_frames:
        pd.concat(seg_frames, ignore_index=True).to_csv(
            out_dir / "exposure_segments.csv", index=False
        )

    summary = {
        "run_id": run_id,
        "location": mission.get("location"),
        "pilot": mission.get("pilot", {}).get("name"),
        "conditions": mission.get("conditions") or None,
        "gnss_vendor": mission.get("gnss_vendor"),
        "sensors": ",".join(sorted(df["sensor_id"].unique())),
        "n_flight_lines": len(df),
        "n_rogue_lines": int(df["rogue_line"].sum()),
        "first_line_start_utc": df["start_utc"].min(),
        "last_line_end_utc": df["end_utc"].max(),
        "centroid_lat": round(df["centroid_lat"].mean(), 6),
        "centroid_lon": round(df["centroid_lon"].mean(), 6),
        "mean_agl_m": round(good["agl_m"].mean(), 1),
        "max_flight_height_range_m": _nanfloat(df["flight_height_range_m"].max()),
        "mean_solar_elevation_deg": round(df["solar_elevation_deg"].mean(), 1),
        "min_time_to_solar_noon_min": df["time_to_solar_noon_min"].abs().min(),
        "max_time_to_solar_noon_min": df["time_to_solar_noon_min"].abs().max(),
        "mean_ground_speed_ms": _nanfloat(round(good["ground_speed_ms"].mean(), 2)),
        "min_est_sidelap_pct": _nanfloat(good["est_sidelap_pct"].min()),
        "min_oversampling_actual_pct": _nanfloat(
            good["oversampling_actual_pct"].min()),
        "lost_frames_total": sum(
            (s.get("settings_txt") or {}).get("lost_frames") or 0
            for s in exposure["sensors"].values()),
        "dark_reference_present": all(
            (s.get("dark_reference") or {}).get("present", False)
            for s in exposure["sensors"].values()) if exposure["sensors"] else None,
        "panels_present": panels["panels_present"],
        "n_panel_detections": panels["n_panel_detections"],
        "n_target_files": panels.get("n_target_files"),
        "max_capture_frames": max(
            (rec["n_capture_frames"] for rec in panels["sensors"].values()),
            default=None),
        "gnss_lever_arm_primary_m": _fmt_vec(
            (lever or {}).get("gnss_primary_lever_arm_m")),
        "gnss_lever_arm_secondary_m": _fmt_vec(
            (lever or {}).get("gnss_secondary_lever_arm_m")),
        "likely_airframe": (lever or {}).get("likely_airframe"),
        "spec_status_calculator": (spec_report or {}).get("verdict_calculator"),
        "spec_status_fieldbook": (spec_report or {}).get("verdict_fieldbook"),
    }
    pd.DataFrame([summary]).to_csv(out_dir / "run_summary.csv", index=False)

    report = {
        "run": {
            "run_id": run_id,
            "gpro_bundle": str(gpro),
            "graw_bundle": str(graw) if graw else "MISSING",
            "generated_by": f"{__title__} {__version__}",
        },
        "mission": {
            "source": "gpro",
            "location": mission.get("location"),
            "pilot": mission.get("pilot", {}).get("name"),
            "conditions": mission.get("conditions") or None,
            "gnss_vendor": mission.get("gnss_vendor"),
        },
        "acquisition": {
            "source": "gpro",
            "sensors": sorted(df["sensor_id"].unique().tolist()),
            "n_flight_lines": int(len(df)),
            "n_rogue_lines": int(df["rogue_line"].sum()),
            "first_line_start_utc": str(df["start_utc"].min()),
            "last_line_end_utc": str(df["end_utc"].max()),
            "mean_line_duration_s": float(round(df["duration_s"].mean(), 1)),
        },
        "geometry": {
            "source": "gpro",
            "centroid_lat": float(round(df["centroid_lat"].mean(), 6)),
            "centroid_lon": float(round(df["centroid_lon"].mean(), 6)),
            "line_headings_deg": [float(h) for h in df["heading_deg"]],
            "mean_agl_m": _nanfloat(good["agl_m"].mean()),
            "agl_note": "flight height minus LiDAR DTM at line centroid "
                        "(shared trajectory vertical frame); mean excludes "
                        "rogue lines",
            "flight_height_range_m_max": _nanfloat(
                df["flight_height_range_m"].max()),
            "flight_height_std_m_max": _nanfloat(
                df["flight_height_std_m"].max()),
            "height_stability_note": "worst within-line spread of per-frame "
                                     "KML vertex heights (ellipsoidal, so "
                                     "aircraft drift only, not terrain)",
        },
        "solar": {
            "source": "computed (pvlib SPA from gpro time + position)",
            "solar_elevation_deg_range": [
                _nanfloat(df["solar_elevation_deg"].min()),
                _nanfloat(df["solar_elevation_deg"].max()),
            ],
            "solar_azimuth_deg_range": _azimuth_arc(df["solar_azimuth_deg"]),
            "azimuth_range_note": "wrap-aware arc [start, end] clockwise, "
                                  "0-360 deg from north (SPA convention); "
                                  "can straddle north for noon flights",
            "time_to_solar_noon_min_range": [
                _nanfloat(df["time_to_solar_noon_min"].min()),
                _nanfloat(df["time_to_solar_noon_min"].max()),
            ],
            "sun_line_angle_deg_range": [
                _nanfloat(df["sun_line_angle_deg"].min()),
                _nanfloat(df["sun_line_angle_deg"].max()),
            ],
            "clearsky_ghi_wm2_range": [
                _nanfloat(df["clearsky_ghi_wm2"].min()),
                _nanfloat(df["clearsky_ghi_wm2"].max()),
            ],
            "clearsky_note": "Ineichen clear-sky model, climatological "
                             "turbidity - theoretical ceiling, NOT "
                             "flight-day weather",
        },
        "exposure": {
            "source": exposure["source"],
            "note": "hyperspec only (RGB is auto-exposed); settings.txt is "
                    "the applied ET and frame period (hdr values can be a "
                    "stale echo of the previous attempt); segments "
                    "are raw capture units, not 1:1 with gpro flight lines",
            "sensors": {
                sid: {
                    "settings_txt": rec["settings_txt"],
                    "dark_reference": rec.get("dark_reference"),
                    "n_segments": int(len(rec["segments"])),
                    "hdr_exposure_ms_range": [
                        _nanfloat(rec["segments"]["exposure_ms"].min()),
                        _nanfloat(rec["segments"]["exposure_ms"].max()),
                    ],
                    "hdr_frame_period_ms_range": [
                        _nanfloat(rec["segments"]["frame_period_ms"].min()),
                        _nanfloat(rec["segments"]["frame_period_ms"].max()),
                    ],
                    "hdr_analog_gain": sorted(
                        rec["segments"]["analog_gain"].dropna().unique().tolist()
                    ),
                    "hdr_digital_gain": sorted(
                        rec["segments"]["digital_gain"].dropna().unique().tolist()
                    ),
                }
                for sid, rec in exposure["sensors"].items()
            },
        },
        "coverage": {
            "source": "computed (gpro line geometry + settings.txt optics)",
            "note": "fieldbook targets: VNIR sidelap >= 37-40 %, SWIR "
                    "> 30 %; along-track oversampling >= 20 % (default "
                    "30 %); speed bands Type 1/2/3 = 2.1/3.2/5.1 m/s; "
                    "rogue lines excluded",
            "sensors": {
                sid: {
                    "ground_speed_ms_range": [
                        _nanfloat(sub["ground_speed_ms"].min()),
                        _nanfloat(sub["ground_speed_ms"].max()),
                    ],
                    "est_swath_m": _nanfloat(sub["est_swath_m"].mean()),
                    "line_spacing_m_range": [
                        _nanfloat(sub["line_spacing_m"].min()),
                        _nanfloat(sub["line_spacing_m"].max()),
                    ],
                    "est_sidelap_pct_range": [
                        _nanfloat(sub["est_sidelap_pct"].min()),
                        _nanfloat(sub["est_sidelap_pct"].max()),
                    ],
                    "oversampling_configured_pct": _nanfloat(
                        sub["oversampling_configured_pct"].median()),
                    "oversampling_actual_pct": _nanfloat(
                        sub["oversampling_actual_pct"].median()),
                }
                for sid, sub in good.groupby("sensor_id")
            },
        },
        "spec_check": spec_report or {"source": "not checked"},
        "trigger": trigger or {"source": "not read"},
        "gnss_lever_arms": {
            **(lever or {"source": "not read"}),
            "note": "SBG: IMU->antenna, body frame x-fwd/y-right/z-down; "
                    "Applanix: POSPac reference-point->sensor. Rig-mount "
                    "fact, so also an airframe proxy (IF1200 vs DJI M350 "
                    "mounts differ)",
        },
        "panels": panels,
    }
    tqdm.write(f"Wrote flight_lines.csv ({len(df)} lines) + run tables "
               f"to {out_dir}")
    return report


# ==================================================================================
def _azimuth_arc(az: pd.Series) -> list[Optional[float]]:
    """Wrap-aware [start, end] arc for azimuths (deg, 0-360 from north).

    Raw min/max misleads when values straddle north (e.g. 358 and 2 deg
    would report [2, 358]). This returns the tight arc about the circular
    mean instead; presentation-only - per-line values and the sun-line
    angle are computed from unwrapped azimuths elsewhere.

    Parameters
    ----------
    az : pd.Series
        Azimuth values in degrees.

    Returns
    -------
    list[Optional[float]]
        [start, end] of the arc travelled clockwise, each in [0, 360);
        [None, None] if no finite values.
    """
    vals = az.dropna().to_numpy(dtype=float)
    if vals.size == 0:
        return [None, None]
    rad = np.deg2rad(vals)
    mean = np.rad2deg(np.arctan2(np.sin(rad).mean(), np.cos(rad).mean())) % 360.0
    dev = (vals - mean + 180.0) % 360.0 - 180.0
    return [round(float((mean + dev.min()) % 360.0), 2),
            round(float((mean + dev.max()) % 360.0), 2)]


# ==================================================================================
def _fmt_vec(vec: Optional[list]) -> Optional[str]:
    """Compact `x y z` string for a lever-arm vector in a CSV cell.

    Parameters
    ----------
    vec : Optional[list]
        Three-element vector, or None.

    Returns
    -------
    Optional[str]
        Space-joined values (e.g. ``"-0.198 -0.301 -0.199"``), or None.
    """
    if not vec:
        return None
    return " ".join(f"{float(v):g}" for v in vec)


# ==================================================================================
def _nanfloat(x: float) -> Optional[float]:
    """Cast to float for YAML, mapping NaN to None.

    Parameters
    ----------
    x : float
        Value to cast (possibly NaN / numpy scalar).

    Returns
    -------
    Optional[float]
        Plain float, or None if ``x`` is NaN.
    """
    return None if pd.isna(x) else float(x)


# ==================================================================================
if __name__ == "__main__":
    # ========== chdir to git root (resolved at module top) ==========
    os.chdir(_git_root)

    # ========== Parse args ==========
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--path", type=str, default=None,
        help="Folder to crawl for processed runs (any APPN tree level). "
             "Defaults to the git repo root.",
    )
    parser.add_argument(
        "--spec", type=str,
        default="reference/thresholds/flightcal_spec.yml",
        help="FlightCal spec/thresholds YAML relative to the repo root "
             "(within-spec check skipped if missing).",
    )
    parser.add_argument(
        "--rogue-agl-frac", type=float, default=0.5,
        help="Flag lines with AGL below this fraction of the per-sensor "
             "median as rogue take-off/landing captures (0 disables).",
    )
    parser.add_argument(
        "--rogue-len-frac", type=float, default=0.2,
        help="Flag lines shorter than this fraction of the per-sensor "
             "median line length as capture stubs (0 disables).",
    )
    parser.add_argument(
        "--exclude-dir", type=str, nargs="+", default=[],
        help="Directory names to exclude from the crawl.",
    )
    parser.add_argument(
        "-f", "--force", default=False, action="store_true",
        help="Regenerate reports even when they are newer than every input.",
    )
    parser.add_argument(
        "-v", "--verbose", default=False, action="store_true",
        help="Enable verbose output.",
    )
    args = parser.parse_args()

    main(args)
