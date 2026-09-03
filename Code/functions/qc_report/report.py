"""JSON-first QC report writer and YAML-summary projector.

Implements the DS02 reporting contract (``Code/DS02_DatasetQA/README.md``;
design record: retired QC pipeline plan §2, git history): every
DS02 script writes a dual-file report per invocation —

- ``<script>_detail.json`` — everything (full check objects, per-item data,
  config snapshot, provenance, warnings log).
- ``<script>_summary.yaml`` — a pure projection of the JSON: identity,
  statuses, one line per check, pointer to the detail file.

The script builds the detail dict first (via :func:`new_report` plus its own
keys) and the summary is derived from it — the two can never disagree.

On-disk layout (section 4): summaries at the top of ``QC_data/`` so a bare
``ls`` answers "what state is this run in?"; the detail JSON and all other
artefacts live in one subfolder per script::

    QC_data/QC01_FlightCheck_summary.yaml
    QC_data/QC01_FlightCheck/QC01_FlightCheck_detail.json

The detail JSON is written first and the summary YAML last, so the summary
doubles as the completion marker (same idiom as the pipeline metadata
sidecars).
"""

import json
import pathlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import yaml

from Code.functions.qc_report.status import (check_levels, script_levels,
                                              derive_status)
from Code.functions.core_functions.run_metadata import to_yaml_compatible


# ==================================================================================
def schema_version() -> str:
    """Return the current report schema version.

    The detail JSON and summary YAML share this version and it is bumped
    for both together.

    Returns
    -------
    str
        The schema version string.
    """
    return "1.0"


# ==================================================================================
def new_report(
        script_name: str,
        script_version: str,
        run: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
    """Create a contract-shaped detail-report skeleton.

    The caller fills in ``checks`` (and any script-specific keys such as
    ``config``, ``provenance`` or per-item data) and passes the dict to
    :func:`write_report`, which derives the run status and the summary.

    Parameters
    ----------
    script_name : str
        Contract script name, e.g. ``"QC01_FlightCheck"``.
    script_version : str
        The producing script's ``__version__``.
    run : dict, optional
        Run/scope identity (``parse_APPN_dataset_path`` fields plus bundle
        identifiers such as gpro/graw names). Omitted keys are allowed.

    Returns
    -------
    dict
        Skeleton with ``schema_version``, ``script``, ``run``,
        ``generated_utc``, empty ``checks``/``artifacts``/``warnings``.
    """
    return {
        "schema_version": schema_version(),
        "script": {"name": script_name, "version": script_version},
        "run": dict(run) if run else {},
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "status": "not_evaluated",
        "checks": {},
        "artifacts": [],
        "warnings": [],
    }


# ==================================================================================
def add_check(
        report: Dict[str, Any],
        name: str,
        status: str,
        value: Any = None,
        note: Optional[str] = None,
        advisory: bool = False,
        waived: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
    """Add a check object to a report's ``checks`` mapping.

    Parameters
    ----------
    report : dict
        A report dict (from :func:`new_report`).
    name : str
        Check name, e.g. ``"sidelap_vnir_fieldbook"``.
    status : str
        Check-level status (``good | acceptable | warning | fail |
        not_checked``).
    value : Any, optional
        Headline value (string with units preferred for the summary).
    note : str, optional
        One-line note shown in the summary.
    advisory : bool, optional
        When True the check is recorded but excluded from the worst-wins
        run status. Default False.
    waived : str, optional
        Waiver reason (e.g. a declared flight deviation). The measured
        status is kept but a waived fail contributes at most ``warn``
        to the run status.
    **extra : Any
        Detail-only fields (``threshold``, ``units``, ``evidence``, ...).

    Returns
    -------
    dict
        The check object that was inserted.

    Raises
    ------
    ValueError
        If *status* is not a valid check-level status.
    """
    if status not in check_levels():
        raise ValueError(
            f"Check {name!r}: status {status!r} not in {check_levels()}.")
    check: Dict[str, Any] = {"status": status}
    if value is not None:
        check["value"] = value
    if note is not None:
        check["note"] = note
    if advisory:
        check["advisory"] = True
    if waived:
        check["waived"] = waived
    check.update(extra)
    report["checks"][name] = check
    return check


# ==================================================================================
def report_paths(
        qc_data_dir: pathlib.Path,
        script_name: str,
        scope: Optional[str] = None,
    ) -> Tuple[pathlib.Path, pathlib.Path]:
    """Return the contract (summary, detail) paths for a script.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``T1_proc/QC_data/`` folder (QC scripts) or the routed
        ``QAReports/`` folder (QA scripts).
    script_name : str
        Contract script name, e.g. ``"QC00_GCPCheck"``.
    scope : str, optional
        Scope label for cross-run (QA) reports — inserted into the
        filenames so crawls at different scopes never clobber
        (``QA01_FlightComparison_AU-2026Rosedale-CALVIS_summary.yaml``).

    Returns
    -------
    tuple of pathlib.Path
        ``(summary_yaml, detail_json)`` — the summary at the top level,
        the detail inside the per-script subfolder.
    """
    stem = script_name if not scope else f"{script_name}_{scope}"
    summary = qc_data_dir / f"{stem}_summary.yaml"
    detail = qc_data_dir / stem / f"{stem}_detail.json"
    return summary, detail


# ==================================================================================
def summarize(report: Dict[str, Any]) -> Dict[str, Any]:
    """Project a detail report to its summary dict.

    Keeps identity, run status, one line per check (status + headline
    value + optional note + advisory flag), the detail pointer and the
    artifact list; drops evidence arrays, per-item data and provenance.

    Parameters
    ----------
    report : dict
        The full detail-report dict.

    Returns
    -------
    dict
        The summary projection (not yet YAML-serialised).
    """
    script_name = report["script"]["name"]
    scope = report.get("scope")
    stem = script_name if not scope else f"{script_name}_{scope}"
    checks_summary: Dict[str, Any] = {}
    for name, check in report["checks"].items():
        line = {"status": check["status"]}
        for key in ("value", "note"):
            if key in check:
                line[key] = check[key]
        if check.get("advisory", False):
            line["advisory"] = True
        if check.get("waived"):
            line["waived"] = check["waived"]
        checks_summary[name] = line
    summary = {
        "schema_version": report["schema_version"],
        "script": report["script"],
        "run": report["run"],
        "generated_utc": report["generated_utc"],
        "status": report["status"],
        "checks": checks_summary,
        "detail": f"{stem}/{stem}_detail.json",
        "artifacts": list(report.get("artifacts", [])),
    }
    if scope:
        summary["scope"] = scope
    return summary


# ==================================================================================
def write_report(
        qc_data_dir: pathlib.Path,
        report: Dict[str, Any],
        derive: bool = True,
    ) -> Tuple[pathlib.Path, pathlib.Path]:
    """Validate a report, derive its status, and write both contract files.

    The detail JSON is written first (into the per-script subfolder,
    created if missing) and the summary YAML last so it doubles as the
    completion marker.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``T1_proc/QC_data/`` folder (created if missing).
    report : dict
        The detail-report dict (from :func:`new_report` + caller keys).
    derive : bool, optional
        When True (default) the run ``status`` is (re)derived from the
        non-advisory checks via worst-wins collapse. Pass False to keep a
        caller-set status (e.g. advisory-only scripts forcing
        ``not_evaluated``).

    Returns
    -------
    tuple of pathlib.Path
        ``(summary_yaml, detail_json)`` paths that were written.

    Raises
    ------
    ValueError
        If required keys are missing or a status is out of vocabulary.
    """
    _validate(report)
    if derive:
        report["status"] = derive_status(report["checks"])
    elif report["status"] not in script_levels():
        raise ValueError(
            f"Run status {report['status']!r} not in {script_levels()}.")

    summary_path, detail_path = report_paths(
        pathlib.Path(qc_data_dir), report["script"]["name"],
        scope=report.get("scope"))
    detail_path.parent.mkdir(parents=True, exist_ok=True)

    serialisable = to_yaml_compatible(report)
    with open(detail_path, "w", encoding="utf-8") as fh:
        json.dump(serialisable, fh, indent=2, sort_keys=False)

    summary = summarize(serialisable)
    with open(summary_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(summary, fh, sort_keys=False, allow_unicode=True)
    return summary_path, detail_path


# ==================================================================================
def _validate(report: Dict[str, Any]) -> None:
    """Check a report dict has the contract-required shape.

    Parameters
    ----------
    report : dict
        Candidate detail-report dict.

    Returns
    -------
    None

    Raises
    ------
    ValueError
        If a required key is missing or a check status is invalid.
    """
    required: List[str] = ["schema_version", "script", "run",
                           "generated_utc", "status", "checks"]
    missing = [key for key in required if key not in report]
    if missing:
        raise ValueError(f"Report missing required keys: {missing}.")
    if "name" not in report["script"]:
        raise ValueError("Report 'script' mapping needs a 'name' key.")
    for name, check in report["checks"].items():
        if "status" not in check:
            raise ValueError(f"Check {name!r} has no 'status' key.")
        if check["status"] not in check_levels():
            raise ValueError(
                f"Check {name!r}: status {check['status']!r} "
                f"not in {check_levels()}.")
