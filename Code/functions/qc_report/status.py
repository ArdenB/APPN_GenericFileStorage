"""Shared QC/QA status vocabulary and worst-wins collapse.

Implements the DS02 status vocabulary (``Code/DS02_DatasetQA/README.md``;
design record: retired QC pipeline plan §3, git history):

- Check level: ``good | acceptable | warning | fail | not_checked``
- Script/run level: ``pass | warn | fail | not_evaluated``

One implementation shared by all DS02 scripts — no per-script vocabularies.
"""

from typing import Any, Iterable, Mapping, Tuple


# ==================================================================================
def check_levels() -> Tuple[str, ...]:
    """Return the valid check-level statuses (most to least healthy).

    Returns
    -------
    tuple of str
        ``("good", "acceptable", "warning", "fail", "not_checked")``.
    """
    return ("good", "acceptable", "warning", "fail", "not_checked")


# ==================================================================================
def script_levels() -> Tuple[str, ...]:
    """Return the valid script/run-level statuses (most to least healthy).

    Returns
    -------
    tuple of str
        ``("pass", "warn", "fail", "not_evaluated")``.
    """
    return ("pass", "warn", "fail", "not_evaluated")


# ==================================================================================
def collapse(status: str) -> str:
    """Collapse a check-level status to the script/run-level vocabulary.

    ``good``/``acceptable`` map to ``pass``, ``warning`` to ``warn``,
    ``fail`` to ``fail`` and ``not_checked`` to ``not_evaluated``.
    Script-level statuses pass through unchanged, so :func:`worst` can
    also aggregate script statuses across runs.

    Parameters
    ----------
    status : str
        A check-level or script-level status string.

    Returns
    -------
    str
        The script-level status.

    Raises
    ------
    ValueError
        If *status* is not in either vocabulary.
    """
    mapping = {
        "good": "pass",
        "acceptable": "pass",
        "warning": "warn",
        "fail": "fail",
        "not_checked": "not_evaluated",
        # script-level statuses are idempotent
        "pass": "pass",
        "warn": "warn",
        "not_evaluated": "not_evaluated",
    }
    if status not in mapping:
        raise ValueError(
            f"Unknown status {status!r}. Valid check-level statuses: "
            f"{check_levels()}; script-level: {script_levels()}.")
    return mapping[status]


# ==================================================================================
def worst(statuses: Iterable[str]) -> str:
    """Worst-wins collapse of statuses to a single script-level status.

    Each status is first collapsed via :func:`collapse`. ``not_evaluated``
    entries are ignored unless *all* entries are ``not_evaluated`` (or the
    iterable is empty), in which case ``not_evaluated`` is returned.

    Parameters
    ----------
    statuses : iterable of str
        Check-level and/or script-level statuses.

    Returns
    -------
    str
        One of ``pass | warn | fail | not_evaluated``.

    Raises
    ------
    ValueError
        If any status is not in either vocabulary.
    """
    severity = {"not_evaluated": 0, "pass": 1, "warn": 2, "fail": 3}
    worst_seen = "not_evaluated"
    for status in statuses:
        level = collapse(status)
        if severity[level] > severity[worst_seen]:
            worst_seen = level
    return worst_seen


# ==================================================================================
def derive_status(checks: Mapping[str, Mapping[str, Any]]) -> str:
    """Derive a script-level status from a report's ``checks`` mapping.

    Checks flagged ``advisory: true`` are excluded from the collapse
    (they are recorded but never gate the run status — see the retired
    plan's §7 Phase 3 homogeneity wire-in). A check carrying a
    truthy ``waived`` reason (e.g. a declared flight deviation) keeps
    its measured status but contributes at most ``warn`` — a waived
    fail flags the run instead of failing it.

    Parameters
    ----------
    checks : mapping of str to mapping
        Check name to check object. Each check object must carry a
        ``status`` key; an optional truthy ``advisory`` key excludes it;
        an optional truthy ``waived`` key caps its contribution at warn.

    Returns
    -------
    str
        One of ``pass | warn | fail | not_evaluated``.

    Raises
    ------
    KeyError
        If a check object has no ``status`` key.
    ValueError
        If a status is not in either vocabulary.
    """
    gating = []
    for chk in checks.values():
        if chk.get("advisory", False):
            continue
        status = chk["status"]
        if chk.get("waived") and collapse(status) == "fail":
            status = "warning"
        gating.append(status)
    return worst(gating)
