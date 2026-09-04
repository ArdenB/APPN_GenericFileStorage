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
for payloads with no record, ``flight_compliance`` block when Deviations
flips on, ``triggers`` list sync); never modify existing content; skip +
warn on unparseable files, never "repair".

The ``flight_compliance`` list is the Deviations trigger's intent-axis
payload. Like every intent list it is authored delete-down: the template
emits the fully compliant state and the operator DELETES the axis the
flight deliberately broke (e.g. ``solar_window`` for a solar-window
sweep) — missing entries are the declared deviations. Declared
deviations open no tickets and leave the run ``clean``, but exclude it
from QA cross-run baselines by default (``--include-flight-deviations``
re-adds) and let QC01 annotate/waive the covered checks.
"""

import json
import pathlib
import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
import warnings as warn

import pandas as pd
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.comments import CommentedMap, CommentedSeq

__all__ = [
    "read_triggers",
    "read_duplicate",
    "run_disposition",
    "load_issue_yaml",
    "flight_deviation_vocab",
    "run_flight_deviations",
    "finding_states",
    "finding_groups",
    "ensure_finding_tickets",
    "classify_run",
    "RunDecision",
    "run_decision",
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
def run_disposition(date_dir: pathlib.Path) -> Dict[str, Dict[str, Any]]:
    """Resolve duplicate groups and their winners from RunOverview.csv.

    A duplicate group is an original run plus every row whose ``DupOf``
    (run number of the original, chains resolved to the root) points at
    it. The group's winner is the single row flagged ``BestRun``; when
    no row is flagged the original wins. Legacy flat duplicates
    (``DuplicateRun`` set, no ``DupOf``) have no resolvable group: they
    are never winners unless self-promoted via ``BestRun``.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder containing ``RunOverview.csv``.

    Returns
    -------
    dict
        Maps each run name to ``{"is_duplicate": bool, "dup_of":
        str or None, "winner": str or None, "is_winner": bool}``.
        ``dup_of`` is the resolved root original; ``winner`` is None
        only for flat duplicates with no group and no ``BestRun``.
        Empty when the file is missing (callers treat unknown runs as
        winners).

    Raises
    ------
    ValueError
        If more than one row in a duplicate group is flagged
        ``BestRun`` (ambiguous curation — fix the table).

    Warns
    -----
    UserWarning
        On a ``DupOf`` that is unparseable, self-referencing, cyclic or
        naming no run (row degrades to a flat duplicate), and on a
        ``DupOf`` without ``DuplicateRun`` (treated as duplicate).
    """
    fpath = date_dir / "RunOverview.csv"
    if not fpath.is_file():
        return {}
    df = pd.read_csv(fpath, index_col="Run")
    runs = [str(r) for r in df.index]

    # +++++ Per-row flag parsing (same truthy set as read_triggers) +++++
    def _flag(run: str, col: str) -> bool:
        if col not in df.columns:
            return False
        val = df.loc[run, col]
        if isinstance(val, str):
            return val.strip().lower() in {"true", "t", "1", "yes", "y"}
        return bool(val) if pd.notna(val) else False

    def _dupof(run: str) -> Optional[str]:
        if "DupOf" not in df.columns:
            return None
        val = df.loc[run, "DupOf"]
        if pd.isna(val) or (isinstance(val, str) and not val.strip()):
            return None
        if isinstance(val, str):
            s = val.strip()
            if s.lower().startswith("run"):
                return s
            try:
                val = float(s)
            except ValueError:
                warn.warn(f"{fpath}: {run} has unparseable DupOf {s!r} "
                          f"— ignored")
                return None
        return f"run_{int(val):02d}"

    dup_flag = {r: _flag(r, "DuplicateRun") for r in runs}
    best_flag = {r: _flag(r, "BestRun") for r in runs}
    dup_of: Dict[str, Optional[str]] = {}
    for r in runs:
        target = _dupof(r)
        if target is not None and (target == r or target not in runs):
            warn.warn(f"{fpath}: {r} DupOf points at "
                      f"{'itself' if target == r else target + ', which is not a run in the table'}"
                      f" — ignored")
            target = None
        if target is not None and not dup_flag[r]:
            warn.warn(f"{fpath}: {r} has DupOf but DuplicateRun is not set "
                      f"— treating as duplicate")
            dup_flag[r] = True
        dup_of[r] = target

    # +++++ Resolve DupOf chains (dup of a dup) to the root original +++++
    def _root(run: str) -> Optional[str]:
        seen, cur = {run}, run
        while dup_of[cur] is not None:
            cur = dup_of[cur]
            if cur in seen:
                warn.warn(f"{fpath}: circular DupOf chain involving {run} "
                          f"— treating as flat duplicate")
                return None
            seen.add(cur)
        return cur

    groups: Dict[str, List[str]] = {}
    root_of: Dict[str, Optional[str]] = {}
    for r in runs:
        root = _root(r) if dup_of[r] is not None else None
        root_of[r] = root
        if root is not None:
            groups.setdefault(root, []).append(r)

    # +++++ Pick each group's winner (single BestRun, else the root) +++++
    out = {r: {"is_duplicate": dup_flag[r], "dup_of": root_of[r],
               "winner": None} for r in runs}
    grouped = set()
    for root, dups in groups.items():
        group = [root] + dups
        grouped.update(group)
        bests = [g for g in group if best_flag[g]]
        if len(bests) > 1:
            raise ValueError(
                f"{fpath}: multiple BestRun rows in the duplicate group of "
                f"{root}: {bests}. A group may declare at most one winner.")
        winner = bests[0] if bests else root
        for g in group:
            out[g]["winner"] = winner
    for r in runs:
        if r not in grouped:
            # Flat duplicates stay winnerless unless self-promoted.
            out[r]["winner"] = (r if (not dup_flag[r]) or best_flag[r]
                                else None)
        out[r]["is_winner"] = out[r]["winner"] == r
    return out


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
def flight_deviation_vocab() -> Dict[str, Tuple[str, ...]]:
    """Vocabulary of deliberate flight deviations -> QC01 check prefixes.

    The keys are the full ``flight_compliance`` list emitted into the
    issue-YAML template (operators DELETE the axis the flight
    deliberately broke; missing entries are the declared deviations);
    the values are the QC01 check-name prefixes each entry covers, so
    QC01 can annotate the matching checks as "declared flight
    deviation".

    Returns
    -------
    dict of str -> tuple of str
        ``entry -> check-name prefixes``:

        - ``solar_window``  — flights deliberately outside the solar
          window (``time_to_solar_noon``);
        - ``flight_pattern`` — line geometry / overlap flown off the
          normal plan (``sidelap_*``);
        - ``sensor_config`` — anything configured on the sensor or
          platform (altitude/GSD, frame rate, speed, exposure/gain;
          ``design_note`` says which).
    """
    return {
        "solar_window": ("time_to_solar_noon",),
        "flight_pattern": ("sidelap_",),
        "sensor_config": ("gsd_", "frame_rate_", "oversampling_"),
    }


# ==================================================================================
def run_flight_deviations(date_dir: pathlib.Path,
                          run_name: str) -> List[str]:
    """Derive one run's declared flight deviations from its issue YAML.

    The ``flight_compliance`` list is delete-down: the operator removes
    the axis the flight deliberately broke, so the declared deviations
    are the vocabulary entries *missing* from the kept list. An absent
    or unparseable yaml, or a missing ``flight_compliance`` key, reads
    as fully compliant (no deviations) — an untouched template never
    declares anything. Entries outside
    :func:`flight_deviation_vocab` are ignored with a warning (typo
    guard); they never subtract from compliance.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder holding ``<run>_Issues.yaml``.
    run_name : str
        Run folder name.

    Returns
    -------
    list of str
        The declared deviations in vocabulary order (empty = compliant
        or nothing declared).
    """
    yaml_data, yaml_state = load_issue_yaml(date_dir, run_name)
    if yaml_state != "parsed":
        return []
    raw = yaml_data.get("flight_compliance")
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple, CommentedSeq)):
        warn.warn(f"{run_name}_Issues.yaml in {date_dir}: flight_compliance "
                  f"is not a list ({raw!r}); ignoring it.")
        return []
    kept = [str(e) for e in raw]
    vocab = flight_deviation_vocab()
    unknown = [e for e in kept if e not in vocab]
    if unknown:
        warn.warn(f"{run_name}_Issues.yaml in {date_dir}: unknown "
                  f"flight_compliance entries {unknown} (vocabulary: "
                  f"{sorted(vocab)}).")
    return [e for e in vocab if e not in kept]


# ==================================================================================
def finding_states() -> Dict[str, Tuple[str, ...]]:
    """State vocabulary for machine-authored ``qc_findings`` entries.

    Deliberately split from the payload-ticket vocabulary (QC findings
    plan, development-master repo): a finding can never be ``ok`` — the
    measurement happened, so there is no false-alarm closure (a
    miscalibrated threshold is fixed in the spec yml, and the
    config-sha/version re-run closes the finding) — and ``fixed`` can
    never be hand-set: it is written only by a QC re-run that measures
    every member check good/acceptable.

    Returns
    -------
    dict of str -> tuple of str
        ``open`` (machine-created ``TODO``, human ``wip``),
        ``human_closures`` (``accepted`` — note required — ``caution``,
        ``failed``) and ``machine_closures`` (``fixed``).
    """
    return {
        "open": ("TODO", "wip"),
        "human_closures": ("accepted", "caution", "failed"),
        "machine_closures": ("fixed",),
    }


# ==================================================================================
def finding_groups(report: Dict[str, Any]) -> Dict[str, List[str]]:
    """Canonical per-script finding-grouping policy (one-writer contract).

    Every writer of ``qc_findings`` (the QC scripts inline, PS00 as the
    scheduled backstop) must author identical ``(script, finding)``
    identities or they duplicate each other's tickets, so the
    per-script grouping policies live in this one dispatch:

    - ``QC01_FlightCheck`` — ``flight_spec_{tag}`` groups the
      per-sensor spec family (GSD + frame rate + sidelap +
      oversampling: one mis-set config is one ticket);
      ``sidelap_lidar``, bundle integrity and the flightcal gate stay
      singletons;
    - ``QC03_RasterCheck`` — ``raster_{label}`` groups every
      per-product check (raster problems on one product share a
      cause);
    - anything else — singletons (per product-layer / per region fails
      have independent causes).

    Parameters
    ----------
    report : dict
        Contract report dict (``script.name`` + ``checks`` keys;
        statuses are irrelevant here — :func:`ensure_finding_tickets`
        keeps only failing members).

    Returns
    -------
    dict of str -> list of str
        Finding key -> member check names. Checks outside every group
        become singleton findings.
    """
    script = str((report.get("script") or {}).get("name", ""))
    checks = report.get("checks") or {}
    if script == "QC01_FlightCheck":
        groups: Dict[str, List[str]] = {}
        for name in checks:
            m = re.match(r"(?:gsd|frame_rate|sidelap|oversampling)_"
                         r"(?!lidar$)(.+?)(?:_calculator|_fieldbook)?$", name)
            if m:
                groups.setdefault(f"flight_spec_{m.group(1)}", []).append(name)
        return groups
    if script == "QC03_RasterCheck":
        return {f"raster_{label}": [n for n in checks
                                    if n.endswith(f"_{label}")]
                for label in (report.get("products") or {})}
    return {}


# ==================================================================================
def _live_findings(yaml_data: Dict[str, Any],
                   ) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Resolve the live ``qc_findings`` entry per ``(script, finding)``.

    Entries are append-only history; the *last* entry per identity is
    the live one (a fixed-then-refailed finding gets a fresh appended
    entry, so later always supersedes earlier).

    Parameters
    ----------
    yaml_data : dict
        Parsed issue-YAML mapping.

    Returns
    -------
    dict
        ``(script, finding) -> entry`` for the live entries. Non-mapping
        list items are ignored.
    """
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    entries = yaml_data.get("qc_findings")
    if not isinstance(entries, (list, tuple, CommentedSeq)):
        return out
    for entry in entries:
        if not isinstance(entry, (dict, CommentedMap)):
            continue
        out[(str(entry.get("script")), str(entry.get("finding")))] = entry
    return out


# ==================================================================================
def ensure_finding_tickets(date_dir: pathlib.Path, run_name: str,
                           report: Dict[str, Any],
                           groups: Optional[Dict[str, List[str]]] = None,
                           write: bool = True) -> List[str]:
    """Author/refresh/auto-close machine finding-tickets from a QC report.

    The QC-fail -> ticket bridge (QC findings plan, development-master
    repo). Call **before** ``qr.write_report`` — Issues.yaml is a QC01
    mtime-cache input, so patching it after the report write would make
    every report stale-by-construction. Idempotent: an unchanged file is
    never rewritten. Failures here must never gate the QC script — the
    caller wraps in nothing; this function only warns and returns.

    Per report, three passes over the calling script's own entries:

    - **auto-close**: an open (``TODO``/``wip``) finding whose member
      checks all measure good/acceptable flips to ``fixed`` +
      ``resolved_utc`` (machine-only closure; entry kept). Human
      closures are never touched.
    - **refresh**: an open finding still failing gets its machine fields
      (``checks``/``value``/``script_version``/``report_utc``) updated.
    - **author**: a gating check measuring ``fail`` with no live entry
      gets a new ``TODO`` entry; a ``fixed`` entry that refails gets a
      fresh appended entry (last-per-identity is live). Entries closed
      ``accepted``/``caution``/``failed`` are never re-authored — the
      closure covers the finding.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder holding ``<run>_Issues.yaml`` (created here if
        absent and there is something to author).
    run_name : str
        Run folder name.
    report : dict
        The contract report dict the caller is about to pass to
        ``write_report`` (``script``/``generated_utc``/``checks``/
        ``run`` keys). Only gating checks author findings: ``fail``
        status, not advisory, not waived — the same set that makes the
        run status ``fail``.
    groups : dict of str -> list of str, optional
        Grouping override: finding key -> member check names. Default
        None derives the canonical per-script policy via
        :func:`finding_groups` — pass an explicit mapping only in
        tests. Failing checks not claimed by any group become singleton
        findings keyed by check name.
    write : bool
        When False, report planned actions without writing.

    Returns
    -------
    list of str
        Human-readable actions taken/planned (empty = nothing to do).
    """
    script = str((report.get("script") or {}).get("name", "unknown"))
    version = str((report.get("script") or {}).get("version", ""))
    report_utc = str(report.get("generated_utc", ""))
    checks = report.get("checks") or {}
    if groups is None:
        groups = finding_groups(report)

    # +++++ gating fails (advisory/waived never fail the run -> no finding) +++++
    failing = [n for n, c in checks.items()
               if isinstance(c, dict) and c.get("status") == "fail"
               and not c.get("advisory") and not c.get("waived")]
    passing = {n for n, c in checks.items()
               if isinstance(c, dict)
               and c.get("status") in {"good", "acceptable"}}

    # +++++ apply the grouping policy +++++
    grouped: Dict[str, List[str]] = {}
    claimed: set = set()
    for key, members in groups.items():
        mem = [m for m in members if m in failing]
        if mem:
            grouped[str(key)] = mem
            claimed.update(mem)
    for name in failing:
        if name not in claimed:
            grouped[name] = [name]

    # +++++ load or create the yaml +++++
    fpath = date_dir / f"{run_name}_Issues.yaml"
    yaml_rt = YAML(typ="rt")
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    created = False
    if fpath.is_file():
        try:
            with open(fpath, encoding="utf-8") as f:
                data = yaml_rt.load(f)
        except YAMLError as err:
            warn.warn(f"Skipping unparseable issue YAML {fpath}: {err}")
            return []
        if not isinstance(data, (dict, CommentedMap)):
            warn.warn(f"Skipping issue YAML with non-mapping root: {fpath}")
            return []
    else:
        if not grouped:
            return []
        data = CommentedMap()
        data["schema_version"] = 1.1
        data["run"] = run_name
        sensor = (report.get("run") or {}).get("sensor")
        if sensor:
            data["sensor"] = str(sensor)
        data.yaml_set_comment_before_after_key(
            "schema_version",
            before=f"{run_name}_Issues.yaml - created by {script} "
                   f"(qc_findings only; RunOverview triggers add their "
                   f"blocks via ProjectBuilder/scan)")
        created = True

    findings_seq = data.get("qc_findings")
    if findings_seq is not None and not isinstance(
            findings_seq, (list, CommentedSeq)):
        warn.warn(f"{fpath}: qc_findings is not a list "
                  f"({type(findings_seq).__name__}); leaving file untouched.")
        return []
    live = _live_findings(data)
    actions: List[str] = []
    open_states = set(finding_states()["open"])

    def _headline(members: List[str]) -> str:
        if len(members) == 1:
            c = checks.get(members[0]) or {}
            return str(c.get("value", "fail"))
        return "; ".join(
            f"{m}: {(checks.get(m) or {}).get('value', 'fail')}"
            for m in members)

    # +++++ auto-close: open findings whose members all pass +++++
    for (fscript, fkey), entry in live.items():
        if fscript != script:
            continue
        if str(entry.get("state", "TODO")).strip() not in open_states:
            continue
        members = [str(m) for m in (entry.get("checks") or [])]
        if members and all(m in passing for m in members):
            actions.append(f"close {fkey} as fixed")
            entry["state"] = "fixed"
            entry["resolved_utc"] = (
                report_utc or pd.Timestamp.now(tz="UTC").isoformat())

    # +++++ author / refresh failing findings +++++
    for fkey in sorted(grouped):
        members = grouped[fkey]
        headline = _headline(members)
        entry = live.get((script, fkey))
        reopened = False
        if entry is not None:
            state = str(entry.get("state", "TODO")).strip()
            if state in open_states:
                fields = {"checks": members, "value": headline,
                          "script_version": version,
                          "report_utc": report_utc}
                current = {"checks": [str(m) for m in
                                      (entry.get("checks") or [])],
                           "value": str(entry.get("value", "")),
                           "script_version": str(
                               entry.get("script_version", "")),
                           "report_utc": str(entry.get("report_utc", ""))}
                if current != {k: (v if k == "checks" else str(v))
                               for k, v in fields.items()}:
                    actions.append(f"refresh {fkey}")
                    cseq = CommentedSeq(members)
                    cseq.fa.set_flow_style()
                    entry["checks"] = cseq
                    entry["value"] = headline
                    entry["script_version"] = version
                    entry["report_utc"] = report_utc
                continue
            if state != "fixed":
                continue  # human closure covers the finding
            reopened = True
        rec = CommentedMap()
        rec["script"] = script
        rec["finding"] = fkey
        cseq = CommentedSeq(members)
        cseq.fa.set_flow_style()
        rec["checks"] = cseq
        rec["status"] = "fail"
        rec["value"] = headline
        rec["script_version"] = version
        rec["report_utc"] = report_utc
        rec["state"] = "TODO"
        rec["note"] = ""
        rec.yaml_add_eol_comment(
            "wip | accepted | caution | failed ('fixed' is machine-only,"
            " set by a passing re-run; never 'ok')", key="state")
        rec.yaml_add_eol_comment("required when state is 'accepted'",
                                 key="note")
        if not isinstance(data.get("qc_findings"), (list, CommentedSeq)):
            data["qc_findings"] = CommentedSeq()
            data.yaml_set_comment_before_after_key(
                "qc_findings",
                before="---- QC findings: machine tickets, check-scoped"
                       " (QC scripts author/refresh/auto-close) ----\n"
                       "Close by setting 'state': accepted (tolerable,"
                       " run rejoins QA annotated - fill 'note') |\n"
                       "caution/failed (confirmed problem, run excluded)."
                       " 'wip' keeps it open; a passing QC\n"
                       "re-run closes it as 'fixed' automatically.")
        data["qc_findings"].append(rec)
        actions.append(f"open finding {fkey}"
                       + (" (refailed after fixed)" if reopened else ""))

    if actions and write:
        with open(fpath, "w", encoding="utf-8") as f:
            yaml_rt.dump(data, f)
        if created:
            actions.insert(0, "created Issues.yaml")
    return actions


# ==================================================================================
def _accepted_annotations(yaml_data: Optional[Dict[str, Any]],
                          run_name: str) -> Tuple[str, ...]:
    """Render annotation strings for a run's accepted findings.

    Parameters
    ----------
    yaml_data : dict or None
        Parsed issue-YAML mapping (None = no annotations).
    run_name : str
        Run folder name (for the empty-note warning).

    Returns
    -------
    tuple of str
        One ``accepted: <script>/<finding> — <note>`` per accepted live
        finding. An empty note warns (the reason is the point) but the
        annotation still renders.
    """
    if not yaml_data:
        return ()
    out: List[str] = []
    for (fscript, fkey), entry in sorted(_live_findings(yaml_data).items()):
        if str(entry.get("state", "TODO")).strip() != "accepted":
            continue
        note = str(entry.get("note", "") or "").strip()
        if not note:
            warn.warn(f"{run_name}: accepted finding {fscript}/{fkey} has "
                      f"an empty note — the acceptance reason is required.")
        out.append(f"accepted: {fscript}/{fkey}"
                   + (f" — {note}" if note else " — (no note)"))
    return tuple(out)


# ==================================================================================
def classify_run(date_dir: pathlib.Path, run_name: str) -> Tuple[str, str]:
    """Classify one run's severity from its flags, tickets and findings.

    The severity ladder (worst-wins) drives the QA scripts' filtering;
    per-run QC scripts ignore it. ``accepted`` is the reviewed-but-
    tolerable middle rung (QC findings plan, development-master repo),
    reachable only through ``qc_findings`` entries closed ``accepted``.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder containing ``RunOverview.csv`` and the issue YAML.
    run_name : str
        Run folder name (e.g. ``run_00``).

    Returns
    -------
    tuple of (str, str)
        ``(severity, detail)`` with severity one of (worst wins):

        - ``clean``     — no flags, ``Deviations`` only, or every
          ticket/finding resolved (``ok``/``fixed``);
        - ``accepted``  — all resolved, ≥1 finding closed ``accepted``
          (QA-included by default, annotated);
        - ``untriaged`` — open tickets/findings (``TODO``/``wip``), or
          ``Issues`` set with no Issues.yaml yet;
        - ``degraded``  — a confirmed problem (``caution``/``failed``
          ticket or finding) or an unparseable yaml;
        - ``failed``    — ``RunFailed`` set (yaml never consulted).

    Notes
    -----
    ``qc_findings`` classify regardless of the RunOverview ``Issues``
    bool — the machine never flips bools, findings are its channel into
    the ladder. ``payload_outcomes`` tickets keep requiring the bool,
    which is what authors them.
    """
    triggers = read_triggers(date_dir, run_name)
    if triggers["RunFailed"]:
        return "failed", "RunFailed flagged in RunOverview.csv"
    yaml_data, yaml_state = load_issue_yaml(date_dir, run_name)
    if yaml_state == "unparseable":
        prefix = "Issues flagged, " if triggers["Issues"] else ""
        return "degraded", prefix + "Issues.yaml unparseable"
    if yaml_state == "absent":
        if triggers["Issues"]:
            return "untriaged", "Issues flagged, no Issues.yaml yet"
        return "clean", "no exclusion flags"

    # +++++ machine findings (consulted regardless of the Issues bool) +++++
    fstates = {f"{s}/{k}": str(e.get("state", "TODO")).strip()
               for (s, k), e in _live_findings(yaml_data).items()}
    f_bad = sorted(k for k, s in fstates.items()
                   if s in {"caution", "failed"})
    f_open = sorted(k for k, s in fstates.items() if s in {"TODO", "wip"})
    f_acc = sorted(k for k, s in fstates.items() if s == "accepted")

    # +++++ payload tickets (outcome axis — only authored when Issues set) +++++
    t_bad: List[str] = []
    t_open: List[str] = []
    t_none = False
    if triggers["Issues"]:
        tickets = yaml_data.get("payload_outcomes") or []
        states = {str(t.get("payload")):
                  str(t.get("state", "TODO")).strip().lower()
                  for t in tickets if isinstance(t, dict)}
        t_bad = sorted(p for p, s in states.items()
                       if s in {"caution", "failed"})
        t_open = sorted(p for p, s in states.items()
                        if s not in {"ok", "fixed"})
        t_none = not states

    if t_bad or f_bad:
        if t_bad and not f_bad:
            return "degraded", ("Issues flagged, caution/failed "
                                "ticket(s): " + ", ".join(t_bad))
        return "degraded", ("caution/failed ticket(s)/finding(s): "
                            + ", ".join(t_bad + f_bad))
    if t_open or f_open or t_none:
        if not f_open:
            return "untriaged", ("Issues flagged, open ticket(s): "
                                 + (", ".join(t_open) or "none authored"))
        return "untriaged", ("open ticket(s)/finding(s): "
                             + ", ".join(t_open + f_open))
    if f_acc:
        return "accepted", "accepted finding(s): " + ", ".join(f_acc)
    if triggers["Issues"]:
        return "clean", "Issues flagged, all tickets resolved (ok/fixed)"
    return "clean", "no exclusion flags"


# ==================================================================================
class RunDecision(NamedTuple):
    """QA crawl decision for one run.

    Attributes
    ----------
    included : bool
        Process the run (True) or skip it (False).
    reason : str or None
        Exclusion reason naming the flag that re-includes it (None when
        included).
    annotations : tuple of str
        Caveats to carry into every listing/report for an *included*
        run (``accepted: <script>/<finding> — <note>`` per accepted
        finding). Always empty for excluded runs.
    """

    included: bool
    reason: Optional[str]
    annotations: Tuple[str, ...]


# ==================================================================================
def run_decision(date_dir: pathlib.Path, run_name: str,
                 include_runs: Optional[str] = None,
                 include_duplicates: bool = False,
                 include_flight_deviations: bool = False,
                 exclude_accepted: bool = False) -> RunDecision:
    """Decide whether a QA crawl processes this run, with annotations.

    Three orthogonal exclusion axes checked in order (first hit wins):
    duplicate-group winners, declared flight deviations, then the
    severity ladder. ``accepted`` runs are included by default and carry
    their acceptance annotations; ``--exclude-accepted`` drops them for
    pristine-baseline work.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder containing ``RunOverview.csv``.
    run_name : str
        Run folder name.
    include_runs : str or None
        Cumulative severity ladder from the ``--include-runs`` flag:
        None (clean + accepted), ``untriaged``, ``degraded`` or
        ``failed``. Each level also includes everything below it.
    include_duplicates : bool
        Include every member of each duplicate group (orthogonal axis).
        By default only the group's winner is processed: the single
        ``BestRun`` row, or the original when no ``BestRun`` is set —
        so a winning reprocess demotes its original (see
        ``run_disposition``).
    include_flight_deviations : bool
        Include runs with declared flight deviations (entries deleted
        from the ``flight_compliance`` list in their issue YAML) —
        deliberately off-spec flights that would otherwise pollute
        cross-run baselines (orthogonal axis). Deployable/payload
        intent (panels, gcps) never feeds this flag.
    exclude_accepted : bool
        Exclude runs whose severity is ``accepted`` (reviewed-but-
        tolerable findings) instead of including them annotated.

    Returns
    -------
    RunDecision
        ``included`` + exclusion ``reason`` (naming the re-include
        flag) + acceptance ``annotations`` for included runs.

    Raises
    ------
    ValueError
        If *include_runs* is not one of the ladder levels.
    """
    valid = ("untriaged", "degraded", "failed")
    if include_runs is not None and include_runs not in valid:
        raise ValueError(f"include_runs must be one of {sorted(valid)} "
                         f"or None, got '{include_runs}'")
    if not include_duplicates:
        disp = run_disposition(date_dir).get(run_name)
        if disp is not None and not disp["is_winner"]:
            if not disp["is_duplicate"]:
                return RunDecision(
                    False, f"superseded by {disp['winner']} (BestRun) "
                           "(use --include-duplicates)", ())
            if disp["winner"]:
                return RunDecision(
                    False, f"DuplicateRun flagged, group winner is "
                           f"{disp['winner']} (use --include-duplicates)",
                    ())
            return RunDecision(
                False, "DuplicateRun flagged (use --include-duplicates)",
                ())
    if not include_flight_deviations:
        deviations = run_flight_deviations(date_dir, run_name)
        if deviations:
            return RunDecision(
                False, f"flight deviation(s) declared: "
                       f"{', '.join(deviations)} "
                       "(use --include-flight-deviations)", ())
    severity, detail = classify_run(date_dir, run_name)
    if severity == "accepted" and exclude_accepted:
        return RunDecision(
            False, f"{detail} (excluded by --exclude-accepted)", ())
    rank = {"clean": 0, "accepted": 1, "untriaged": 2, "degraded": 3,
            "failed": 4}
    # accepted is included by default — the ladder floor sits above it
    threshold = max(rank[include_runs or "clean"], rank["accepted"])
    if rank[severity] > threshold:
        return RunDecision(
            False, f"{severity}: {detail} (use --include-runs {severity})",
            ())
    yaml_data, yaml_state = load_issue_yaml(date_dir, run_name)
    annotations = (_accepted_annotations(yaml_data, run_name)
                   if yaml_state == "parsed" else ())
    return RunDecision(True, None, annotations)


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
    # +++++ flight-compliance axis: Deviations declares deliberate
    # departures by DELETING the broken axis - delete-down, never tickets +++++
    if triggers.get("Deviations"):
        lines += [
            "",
            "# ---- flight compliance (DELETE the entries this run deliberately deviated from) ----",
            "# Same delete-down grammar as the intent lists above: what remains declares",
            "# compliance, what you delete declares a deliberate deviation (design intent,",
            "# not a problem - no tickets, the run stays 'clean'). QA crawls exclude runs",
            "# with declared deviations from cross-run baselines",
            "# (--include-flight-deviations re-adds). 'design_note' says why - required",
            "# when anything is deleted, especially sensor_config.",
            f"flight_compliance: [{', '.join(flight_deviation_vocab())}]",
            'design_note: ""',
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
    # +++++ add flight_compliance block when Deviations flips on +++++
    if triggers.get("Deviations") and "flight_compliance" not in data:
        actions.append("add flight_compliance block")
        devs = CommentedSeq(list(flight_deviation_vocab()))
        devs.fa.set_flow_style()
        data["flight_compliance"] = devs
        data.yaml_set_comment_before_after_key(
            "flight_compliance",
            before="---- flight compliance (DELETE the entries this run"
                   " deliberately deviated from) ----\n"
                   "What remains declares compliance, what you delete"
                   " declares a deliberate\n"
                   "deviation (design intent, not a problem - no tickets,"
                   " the run stays 'clean').\n"
                   "QA crawls exclude runs with declared deviations"
                   " (--include-flight-deviations\n"
                   "re-adds). 'design_note' says why - required when"
                   " anything is deleted.")
        if "design_note" not in data:
            data["design_note"] = ""
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
