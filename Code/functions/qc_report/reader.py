"""QC report reader, tolerant of legacy filenames and schemas.

Implements the reader half of section 6 of
``Code/DS02_DatasetQA/QC_PIPELINE_PLAN.md``: pre-migration JSON reports in
existing ``QC_data/`` folders (``QC_GCP*distances*_report.json``,
``QC_spectra_report.json`` — ``status.result`` schema) remain readable next
to contract ``<script>/<script>_detail.json`` reports, and legacy files are
found whether still loose at the top of ``QC_data/`` or already migrated
into their script's subfolder (section 4 transition rule).

Every read normalises to one shape: the raw report plus a script-level
``status`` in the shared vocabulary (``pass | warn | fail | not_evaluated``).
"""

import json
import pathlib
from typing import Any, Dict, List, Optional

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
