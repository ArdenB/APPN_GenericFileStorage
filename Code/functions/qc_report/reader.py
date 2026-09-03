"""QC report reader, tolerant of legacy filenames and schemas.

Implements the reader half of the shared-helper design (retired QC
pipeline plan §6, in this repo's git history): pre-migration JSON reports in
existing ``QC_data/`` folders (``QC_GCP*distances*_report.json``,
``QC_spectra_report.json`` — ``status.result`` schema) remain readable next
to contract ``<script>/<script>_detail.json`` reports, and legacy files are
found whether still loose at the top of ``QC_data/`` or already migrated
into their script's subfolder (section 4 transition rule).

Every read normalises to one shape: the raw report plus a script-level
``status`` in the shared vocabulary (``pass | warn | fail | not_evaluated``).

Also home to version-aware cache invalidation (:func:`report_is_current`):
the detail JSON records the producing script's version, so a numeric
version bump automatically marks the run's report stale — the scripts
re-do exactly the runs written by older versions, no ``--force`` needed.
"""

import json
import pathlib
import re
from typing import Any, Dict, List, Optional, Tuple

from Code.functions.qc_report.status import script_levels
from Code.functions.qc_report.report import report_paths


# ==================================================================================
def legacy_report_globs(script_name: str) -> List[str]:
    """Return the legacy report-filename globs for a contract script.

    Parameters
    ----------
    script_name : str
        Contract script name, e.g. ``"QC00_GCPCheck"``.

    Returns
    -------
    list of str
        Globs matching that script's pre-migration report files (empty
        for scripts with no legacy format, e.g. the net-new QC03).
    """
    mapping = {
        # ex-QA01_PointDistanceComparison (per-pair + roll-up reports)
        "QC00_GCPCheck": ["QC_GCP*distances*_report.json"],
        # ex-QA00_SpectralValidation
        "QC02_SpectralCheck": ["QC_spectra_report.json"],
    }
    return mapping.get(script_name, [])


# ==================================================================================
def read_report(
        qc_data_dir: pathlib.Path,
        script_name: str,
        scope: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
    """Read a script's QC report from a run's ``QC_data/`` folder.

    The contract detail JSON (``<script>/<script>_detail.json``) wins when
    present; otherwise legacy report files are searched at the top level
    and inside the script's subfolder. Legacy ``status.result`` values are
    normalised to the shared script-level vocabulary.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``T1_proc/QC_data/`` folder (or routed ``QAReports/``).
    script_name : str
        Contract script name, e.g. ``"QC00_GCPCheck"``.
    scope : str, optional
        Scope label for cross-run (QA) reports (see ``report_paths``).

    Returns
    -------
    dict or None
        ``{"path", "legacy", "status", "schema_version", "report"}`` for
        the newest matching report, or None if no report exists.
    """
    qc_data_dir = pathlib.Path(qc_data_dir)
    _, detail_path = report_paths(qc_data_dir, script_name, scope=scope)
    if detail_path.is_file():
        report = _load_json(detail_path)
        return {
            "path": detail_path,
            "legacy": False,
            "status": _normalise_status(report.get("status")),
            "schema_version": report.get("schema_version"),
            "report": report,
        }

    candidates: List[pathlib.Path] = []
    for pattern in legacy_report_globs(script_name):
        candidates.extend(qc_data_dir.glob(pattern))
        candidates.extend((qc_data_dir / script_name).glob(pattern))
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    report = _load_json(newest)
    return {
        "path": newest,
        "legacy": True,
        "status": _legacy_status(report),
        "schema_version": report.get("schema_version"),
        "report": report,
    }


# ==================================================================================
def version_key(version: Any) -> Optional[Tuple[int, int]]:
    """Extract the numeric ``(major, minor)`` from a script version string.

    DS02 versions look like ``"v2.2(03.09.2026)"``; only the numeric part
    identifies an output-affecting change — the date suffix is free to
    move on doc-only touches without invalidating cached reports.

    Parameters
    ----------
    version : Any
        Version string (or None/unexpected type).

    Returns
    -------
    tuple of (int, int) or None
        ``(major, minor)``, or None when nothing parseable is found.
    """
    match = re.search(r"v?(\d+)\.(\d+)", str(version or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


# ==================================================================================
def report_is_current(
        qc_data_dir: pathlib.Path,
        script_name: str,
        current_version: str,
        scope: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
    """Check whether a run's contract report was written by the current
    script version.

    Complements the mtime-based ``outputs_up_to_date`` caching: inputs
    unchanged but script numerically newer ⇒ the report is stale and the
    run should be re-done (no ``--force`` needed). Compared via
    :func:`version_key`, so date-only version-string changes do not
    invalidate.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``T1_proc/QC_data/`` folder (or routed ``QAReports/``).
    script_name : str
        Contract script name, e.g. ``"QC01_FlightCheck"``.
    current_version : str
        The running script's ``__version__``.
    scope : str, optional
        Scope label for cross-run (QA) reports (see ``report_paths``).

    Returns
    -------
    tuple of (bool, str or None)
        ``(True, None)`` when the recorded version matches, else
        ``(False, reason)``.
    """
    _, detail_path = report_paths(
        pathlib.Path(qc_data_dir), script_name, scope=scope)
    if not detail_path.is_file():
        return False, "no contract report"
    try:
        report = _load_json(detail_path)
    except (OSError, json.JSONDecodeError) as err:
        return False, f"unreadable contract report ({err})"
    recorded = report.get("script", {}).get("version")
    if version_key(recorded) is None:
        return False, (f"no parseable script version in "
                       f"{detail_path.name} ({recorded!r})")
    if version_key(recorded) != version_key(current_version):
        return False, (f"report written by {recorded}, "
                       f"current script is {current_version}")
    return True, None


# ==================================================================================
def _legacy_status(report: Dict[str, Any]) -> str:
    """Extract and normalise the status from a legacy report dict.

    Legacy DS02 reports carry ``status.result`` (``pass | fail |
    not_evaluated | unknown | skipped``); anything unrecognised maps to
    ``not_evaluated`` rather than raising, so one malformed historic file
    cannot break a store-wide crawl.

    Parameters
    ----------
    report : dict
        The raw legacy report.

    Returns
    -------
    str
        One of ``pass | warn | fail | not_evaluated``.
    """
    status = report.get("status")
    result = status.get("result") if isinstance(status, dict) else status
    return _normalise_status(result)


# ==================================================================================
def _normalise_status(value: Any) -> str:
    """Map a raw status value onto the script-level vocabulary.

    Parameters
    ----------
    value : Any
        Raw status string (or None/unexpected type).

    Returns
    -------
    str
        One of ``pass | warn | fail | not_evaluated``.
    """
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in script_levels():
            return lowered
        if lowered == "warning":
            return "warn"
    return "not_evaluated"


# ==================================================================================
def _load_json(path: pathlib.Path) -> Dict[str, Any]:
    """Load a JSON report file.

    Parameters
    ----------
    path : pathlib.Path
        Report file to read.

    Returns
    -------
    dict
        Parsed JSON content.
    """
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
