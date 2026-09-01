"""Shared issue-YAML template logic (P9 processing-status tracking).

Single home for creating and additively patching the per-run
``run_XX_Issues.yaml`` ticket files specced in the DataSync repo's
``PROCESSING_STATUS_PLAN.md`` §3.1. Two consumers:

- ``ProjectBuilder.py`` — the user-facing path: operators flip a trigger
  bool in ``RunOverview.csv`` and re-run ProjectBuilder to get their
  template immediately;
- ``PS00_ProcessingStatus.py`` — the scheduled path: every scan (any
  host) creates missing templates and additively patches existing ones.

The files are git-tracked (same class as ``RunOverview.csv``) and ride
the Unison sync; concurrent creation on two hosts is self-limiting
because the trigger bool itself only reaches the other host via the same
sync that delivers the template.

Generator contract (plan §3.1): create when absent; when bools flip on an
existing file append only what is missing (``run_failure`` block, tickets
for payloads with no record, ``triggers`` list sync); never modify
existing content; skip + warn on unparseable files, never "repair".
"""

import json
import pathlib
from typing import Any, Dict, List, Optional, Tuple
import warnings as warn

import pandas as pd
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.comments import CommentedMap, CommentedSeq

__all__ = [
    "read_triggers",
    "read_duplicate",
    "load_issue_yaml",
    "classify_run",
    "run_exclusion",
    "load_sensor_pipeline",
    "render_issue_template",
    "patch_issue_yaml",
    "ensure_issue_yaml",
]


# ==================================================================================
def read_triggers(date_dir: pathlib.Path, run_name: str) -> Dict[str, bool]:
    """Read the trigger bools for one run from RunOverview.csv.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder containing ``RunOverview.csv``.
    run_name : str
        Run folder name (e.g. ``run_00``).

    Returns
    -------
    dict
        Keys ``Deviations``, ``Issues``, ``RunFailed`` (bool). A missing
        file, row, or column reads as all-False (legacy files are valid).
    """
    out = {"Deviations": False, "Issues": False, "RunFailed": False}
    fpath = date_dir / "RunOverview.csv"
    if not fpath.is_file():
        return out
    df = pd.read_csv(fpath, index_col="Run")
    if run_name not in df.index:
        return out
    truthy = {"true", "t", "1", "yes", "y"}
    for col in out:
        if col in df.columns:
            val = df.loc[run_name, col]
            if isinstance(val, str):
                out[col] = val.strip().lower() in truthy
            elif pd.notna(val):
                out[col] = bool(val)
    return out


# ==================================================================================
def read_duplicate(date_dir: pathlib.Path, run_name: str) -> bool:
    """Read the DuplicateRun flag for one run from RunOverview.csv.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder containing ``RunOverview.csv``.
    run_name : str
        Run folder name (e.g. ``run_00``).

    Returns
    -------
    bool
        True when the run is a duplicate (a reprocessing of another
        run's raw, e.g. a BaseStation GNSS re-run — the raw lives with
        the primary run, so registry rules marked ``primary_only`` are
        N/A). A missing file, row, or column reads as False.
    """
    fpath = date_dir / "RunOverview.csv"
    if not fpath.is_file():
        return False
    df = pd.read_csv(fpath, index_col="Run")
    if run_name not in df.index or "DuplicateRun" not in df.columns:
        return False
    val = df.loc[run_name, "DuplicateRun"]
    if isinstance(val, str):
        return val.strip().lower() in {"true", "t", "1", "yes", "y"}
    return bool(val) if pd.notna(val) else False


# ==================================================================================
def load_issue_yaml(date_dir: pathlib.Path,
                    run_name: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Load the per-run issue YAML if present.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder holding ``<run>_Issues.yaml``.
    run_name : str
        Run folder name.

    Returns
    -------
    tuple
        ``(data or None, yaml_state)`` where ``yaml_state`` is one of
        ``absent`` / ``parsed`` / ``unparseable``. Unparseable files are
        skipped and flagged, never repaired (plan §3.1 generator rules).
    """
    fpath = date_dir / f"{run_name}_Issues.yaml"
    if not fpath.is_file():
        return None, "absent"
    yaml_rt = YAML(typ="rt")
    try:
        with open(fpath, encoding="utf-8") as f:
            data = yaml_rt.load(f)
    except YAMLError as err:
        warn.warn(f"Unparseable issue YAML (operator broke syntax?): "
                  f"{fpath} -> {err}")
        return None, "unparseable"
    if data is None:
        return None, "unparseable"
    return dict(data), "parsed"


# ==================================================================================
def classify_run(date_dir: pathlib.Path, run_name: str) -> Tuple[str, str]:
    """Classify one run's severity from its RunOverview flags + tickets.

    The severity ladder (worst-wins) drives the QA scripts'
    ``--include-runs`` filtering; per-run QC scripts ignore it.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder containing ``RunOverview.csv`` and the issue YAML.
    run_name : str
        Run folder name (e.g. ``run_00``).

    Returns
    -------
    tuple of (str, str)
        ``(severity, detail)`` with severity one of:

        - ``clean``     — no flags, ``Deviations`` only, or ``Issues``
          with every ticket resolved (``ok``/``fixed``);
        - ``untriaged`` — ``Issues`` set but tickets still open
          (``TODO``/``wip``/unknown state) or no Issues.yaml yet;
        - ``degraded``  — ``Issues`` set with a confirmed problem
          (``caution``/``failed`` ticket) or an unparseable yaml;
        - ``failed``    — ``RunFailed`` set (yaml never consulted).
    """
    triggers = read_triggers(date_dir, run_name)
    if triggers["RunFailed"]:
        return "failed", "RunFailed flagged in RunOverview.csv"
    if not triggers["Issues"]:
        return "clean", "no exclusion flags"
    yaml_data, yaml_state = load_issue_yaml(date_dir, run_name)
    if yaml_state == "absent":
        return "untriaged", "Issues flagged, no Issues.yaml yet"
    if yaml_state == "unparseable":
        return "degraded", "Issues flagged, Issues.yaml unparseable"
    tickets = yaml_data.get("payload_outcomes") or []
    states = {str(t.get("payload")): str(t.get("state", "TODO")).strip().lower()
              for t in tickets if isinstance(t, dict)}
    confirmed = sorted(p for p, s in states.items()
                       if s in {"caution", "failed"})
    if confirmed:
        return "degraded", ("Issues flagged, caution/failed ticket(s): "
                            + ", ".join(confirmed))
    open_tickets = sorted(p for p, s in states.items()
                          if s not in {"ok", "fixed"})
    if open_tickets or not states:
        return "untriaged", ("Issues flagged, open ticket(s): "
                             + (", ".join(open_tickets) or "none authored"))
    return "clean", "Issues flagged, all tickets resolved (ok/fixed)"


# ==================================================================================
def run_exclusion(date_dir: pathlib.Path, run_name: str,
                  include_runs: Optional[str] = None,
                  include_duplicates: bool = False) -> Optional[str]:
    """Decide whether a QA crawl should exclude this run.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder containing ``RunOverview.csv``.
    run_name : str
        Run folder name.
    include_runs : str or None
        Cumulative severity ladder from the ``--include-runs`` flag:
        None (clean only), ``untriaged``, ``degraded`` or ``failed``.
        Each level also includes everything below it.
    include_duplicates : bool
        Include runs flagged ``DuplicateRun`` (orthogonal axis).

    Returns
    -------
    str or None
        None when the run should be processed; otherwise a reason
        string carrying the CLI flag that would re-include it.

    Raises
    ------
    ValueError
        If *include_runs* is not one of the ladder levels.
    """
    rank = {"clean": 0, "untriaged": 1, "degraded": 2, "failed": 3}
    level = include_runs or "clean"
    if level not in rank:
        raise ValueError(f"include_runs must be one of "
                         f"{sorted(rank)[1:]} or None, got '{include_runs}'")
    if not include_duplicates and read_duplicate(date_dir, run_name):
        return "DuplicateRun flagged (use --include-duplicates)"
    severity, detail = classify_run(date_dir, run_name)
    if rank[severity] > rank[level]:
        return f"{severity}: {detail} (use --include-runs {severity})"
    return None


# ==================================================================================
def load_sensor_pipeline(repo_root: pathlib.Path,
                         sensor: str) -> Optional[Dict[str, Any]]:
    """Load one sensor's pipeline block from reference/sensor_pipelines.

    Parameters
    ----------
    repo_root : pathlib.Path
        APPN repo root containing ``reference/sensor_pipelines/``.
    sensor : str
        Sensor name (file stem, e.g. ``CALVIS``).

    Returns
    -------
    dict or None
        The ``pipeline`` block, or None when the sensor has no file or no
        pipeline ("no pipeline defined" is an explicit state).
    """
    fpath = repo_root / "reference" / "sensor_pipelines" / f"{sensor}.json"
    if not fpath.is_file():
        return None
    with open(fpath, encoding="utf-8") as f:
        return json.load(f).get("pipeline")


# ==================================================================================
def render_issue_template(run: str, sensor: str, triggers: Dict[str, bool],
                          pipeline: Optional[Dict[str, Any]],
                          evidence: Optional[Dict[str, bool]] = None) -> str:
    """Render a brand-new issue-YAML template (plan §3.1 layout).

    Parameters
    ----------
    run : str
        Run folder name.
    sensor : str
        Sensor name.
    triggers : dict
        Trigger bools (at least one is set).
    pipeline : dict or None
        The sensor's pipeline block (payloads/deployables defaults).
    evidence : dict or None
        Optional payload -> products-present scan evidence for the hint
        comments (PS00 supplies it; ProjectBuilder has no scan and passes
        None).

    Returns
    -------
    str
        Full YAML text with guidance comments.
    """
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    trig = [k for k, v in triggers.items() if v]
    payloads = list(pipeline.get("payloads", [])) if pipeline else []
    deploys = list(pipeline.get("deployables", [])) if pipeline else []
    lines = [
        f"# {run}_Issues.yaml - generated {today}",
        "schema_version: 1.0",
        f"run: {run}",
        f"sensor: {sensor}",
        f"triggers: [{', '.join(trig)}]"
        "                # mirrors RunOverview.csv bools; generator keeps in sync",
        "",
        "# ---- intent axis (DELETE entries that don't apply - never type new ones) ----",
        f"intended_payloads: [{', '.join(payloads)}]",
        f"deployables_placed: [{', '.join(deploys)}]",
    ]
    # +++++ outcome axis: only Issues/RunFailed need tickets - a
    # Deviations-only run is an intent-axis event (edit the lists above)
    # and must not spawn open TODO tickets that nag forever +++++
    if not (triggers.get("Issues") or triggers.get("RunFailed")):
        lines += [
            "",
            "# ---- outcome axis: nothing to do - no Issues/RunFailed set. ----",
            "# If something did go wrong, flip the bool in RunOverview.csv and the",
            "# next scan (or ProjectBuilder) appends pre-filled tickets here.",
        ]
        return "\n".join(lines) + "\n"
    lines += [
        "",
        "# ---- outcome axis (one ticket per record; close each one) ----",
    ]
    if triggers.get("RunFailed"):
        lines += [
            "# NOTE: RunFailed is set - the run_failure block below supersedes"
            " payload_outcomes.",
            "# Leave the tickets as TODO unless a payload has its own story"
            " worth recording.",
        ]
    lines += [
        "payload_outcomes:",
        "  # One ticket per payload. Close a ticket by setting 'state' to one of:",
        "  #   ok      - nothing was wrong with this payload (the other fields"
        " are then ignored - delete or keep them)",
        "  #   fixed   - had a problem, now reworked - data fully usable",
        "  #   caution - usable, but with caveats (explain in 'note')",
        "  #   failed  - unrecoverable - no usable data for this payload",
        "  # Or set 'wip' while it is still being worked on - the ticket stays"
        " OPEN (as does the default TODO).",
        "  # For fixed/caution/failed, also fill in 'detected_stage' and"
        " 'reason' ('note' is required when reason is 'other').",
    ]
    if not payloads:
        lines += ["  []   # no payloads in registry - author tickets manually"]
    stage_hint = "field | " + " | ".join(
        s["name"] for s in (pipeline["steps"] if pipeline else []))
    reason_hint = ("gnss | sensor_fault | weather | power | operator"
                   " | hazard | design_flaw | other")
    for payload in payloads:
        if evidence is None:
            hint = "not yet scanned by PS00"
        else:
            hint = ("products present" if evidence.get(payload)
                    else "no products found")
        lines += [
            f"  - payload: {payload}          # scan: {hint}",
            "    state: TODO            # TODO | wip | ok | fixed | caution"
            " | failed",
            f"    detected_stage: TODO   # {stage_hint}",
            f"    reason: TODO           # {reason_hint}",
            '    note: ""',
        ]
    if triggers.get("RunFailed"):
        lines += [
            "",
            "# RunFailed is set - document the total loss (supersedes"
            " payload_outcomes; closing this block is enough)",
            "run_failure:",
            f"  detected_stage: TODO     # {stage_hint}",
            f"  reason: TODO             # {reason_hint}",
            '  note: ""                 # required when reason is \'other\'',
        ]
    return "\n".join(lines) + "\n"


# ==================================================================================
def patch_issue_yaml(fpath: pathlib.Path, triggers: Dict[str, bool],
                     pipeline: Optional[Dict[str, Any]],
                     write_enabled: bool) -> List[str]:
    """Additively patch an existing issue YAML (never modify content).

    Parameters
    ----------
    fpath : pathlib.Path
        The existing YAML file.
    triggers : dict
        Current trigger bools.
    pipeline : dict or None
        Sensor pipeline block.
    write_enabled : bool
        When False, report planned actions without writing.

    Returns
    -------
    list of str
        Human-readable actions taken/planned (empty = nothing to do).
        Unparseable files are skipped + flagged, never repaired.
    """
    yaml_rt = YAML(typ="rt")
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    try:
        with open(fpath, encoding="utf-8") as f:
            data = yaml_rt.load(f)
    except YAMLError as err:
        warn.warn(f"Skipping unparseable issue YAML {fpath}: {err}")
        return []
    if not isinstance(data, (dict, CommentedMap)):
        warn.warn(f"Skipping issue YAML with non-mapping root: {fpath}")
        return []
    actions: List[str] = []
    # +++++ sync the triggers list (the one sanctioned in-place update) +++++
    trig = [k for k, v in triggers.items() if v]
    existing_trig = [str(t) for t in (data.get("triggers") or [])]
    if sorted(existing_trig) != sorted(trig):
        actions.append(f"sync triggers {existing_trig} -> {trig}")
        new_trig = CommentedSeq(trig)
        new_trig.fa.set_flow_style()
        data["triggers"] = new_trig
    # +++++ add run_failure block when RunFailed flips on +++++
    if triggers.get("RunFailed") and "run_failure" not in data:
        actions.append("add run_failure block")
        stage_hint = "field | " + " | ".join(
            s["name"] for s in (pipeline["steps"] if pipeline else []))
        rf = CommentedMap()
        rf["detected_stage"] = "TODO"
        rf["reason"] = "TODO"
        rf["note"] = ""
        rf.yaml_add_eol_comment(stage_hint, key="detected_stage")
        rf.yaml_add_eol_comment(
            "gnss | sensor_fault | weather | power | operator"
            " | hazard | design_flaw | other",
            key="reason")
        rf.yaml_add_eol_comment("required when reason is 'other'", key="note")
        data["run_failure"] = rf
        data.yaml_set_comment_before_after_key(
            "run_failure",
            before="RunFailed is set - document the total loss (supersedes"
                   " payload_outcomes; closing this block is enough)")
    # +++++ add tickets for intended payloads with no record (outcome axis
    # is Issues/RunFailed territory - Deviations alone never adds tickets) +++++
    if triggers.get("Issues") or triggers.get("RunFailed"):
        intended = [str(p) for p in (data.get("intended_payloads")
                                     or (pipeline.get("payloads", []) if pipeline else []))]
        outcomes = data.get("payload_outcomes")
        have = {str(r.get("payload")) for r in (outcomes or []) if isinstance(r, dict)}
        missing = [p for p in intended if p not in have]
    else:
        missing = []
    if missing:
        actions.append(f"add ticket(s) for {missing}")
        if outcomes is None:
            outcomes = CommentedSeq()
            data["payload_outcomes"] = outcomes
        for payload in missing:
            rec = CommentedMap()
            rec["payload"] = payload
            rec["state"] = "TODO"
            rec["detected_stage"] = "TODO"
            rec["reason"] = "TODO"
            rec["note"] = ""
            # +++++ same guidance comments as render_issue_template +++++
            rec.yaml_add_eol_comment(
                "TODO | wip | ok | fixed | caution | failed", key="state")
            rec.yaml_add_eol_comment(
                "field | " + " | ".join(
                    s["name"] for s in (pipeline["steps"] if pipeline else [])),
                key="detected_stage")
            rec.yaml_add_eol_comment(
                "gnss | sensor_fault | weather | power | operator"
                " | hazard | design_flaw | other",
                key="reason")
            outcomes.append(rec)
    if actions and write_enabled:
        with open(fpath, "w", encoding="utf-8") as f:
            yaml_rt.dump(data, f)
    return actions


# ==================================================================================
def ensure_issue_yaml(date_dir: pathlib.Path, run_name: str, sensor: str,
                      triggers: Dict[str, bool],
                      pipeline: Optional[Dict[str, Any]],
                      evidence: Optional[Dict[str, bool]] = None,
                      write: bool = True) -> Optional[str]:
    """Create or additively patch a run's issue YAML if triggers are set.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder (holds ``RunOverview.csv`` and the YAML).
    run_name : str
        Run folder name.
    sensor : str
        Sensor name.
    triggers : dict
        Trigger bools from :func:`read_triggers`.
    pipeline : dict or None
        Sensor pipeline block.
    evidence : dict or None
        Optional payload -> products-present scan evidence.
    write : bool
        When False, report the planned action without writing.

    Returns
    -------
    str or None
        Action description (``"created"`` or the patch actions joined),
        or None when no trigger is set / nothing to do.
    """
    if not any(triggers.values()):
        return None
    fpath = date_dir / f"{run_name}_Issues.yaml"
    if not fpath.is_file():
        if write:
            text = render_issue_template(run_name, sensor, triggers,
                                         pipeline, evidence)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(text)
        return "created"
    actions = patch_issue_yaml(fpath, triggers, pipeline, write)
    return ", ".join(actions) if actions else None
