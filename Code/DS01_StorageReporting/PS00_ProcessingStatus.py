"""P9 processing-status collector (PS00).

Read-only scanner that answers "where is every run up to, and what has
stalled or been forgotten?" for every ``project/site/sensor/date/run_XX``
under a node's data tree (ported from the development-master repo; see
``Code/DS01_StorageReporting/README.md``):

- evaluates each sensor's declarative ``pipeline`` block from
  ``reference/sensor_pipelines/*.json`` (named stages with composable
  ``done_when`` rules, ``requires`` gates, ephemeral-stage inference);
- reads the ``RunOverview.csv`` trigger bools (``Deviations`` / ``Issues``
  / ``RunFailed``) and the per-run issue YAML
  (``<date>/run_XX_Issues.yaml``) to fold operator tickets into a derived
  ``run_status``;
- writes layer-1 outputs per project (append-only ``events_<SYS>.parquet``
  ledger + regenerable ``state_<SYS>.parquet``) into
  ``<project>/Documentation/ProcessingStatus/`` and layer-2 aggregates into
  ``<node>/Documents/StorageReports/ProcessingStatus/`` plus the human
  json/xlsx/csv reports;
- generates pre-filled issue-YAML templates for runs whose
  trigger bools are set: created when absent, additively patched when
  bools flip, existing operator content never modified.

The scanner never moves, deletes, or fixes data. The only write into the
data tree is the sanctioned issue-YAML template generation, which is
additive, idempotent, and opt-in. After template generation the scan
auto-commits and pushes any changed/new run metadata (``*_Issues.yaml``,
``RunOverview.csv``) — nothing else is ever staged (``--no-gitsync``
disables this).

Command-line Arguments
----------------------
--dryrun : flag
    Scan and report to stdout only; write nothing anywhere.
--projects : str [str ...]
    Only scan these project folder names (debug convenience).
--dataroot : str
    Folder containing the node dir (default: the git repo root).
--node : str
    Node folder name (default: the single node in NodeSummary.yaml).
--system : str
    System tag used in output file names, WS or NAS (default WS).
--no-gitsync : flag
    Skip the automatic commit/push of changed run metadata
    (``*_Issues.yaml``, ``RunOverview.csv``).
"""

# ==============================================================================

__title__ = "PS00 Processing Status"
__author__ = "Arden Burrell"
__version__ = "v1.2(04.09.2026)"
__email__ = "arden.burrell@sydney.edu.au"

# ==============================================================================
# ========== Import core packages ==========
import os
import sys
import re
import json
import socket
import fnmatch
import argparse
import pathlib
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# ========== Import other packages ==========
import git
from git import exc as git_exc
import pandas as pd
from tqdm import tqdm
import warnings as warn

import jsonschema
from openpyxl.styles import Font, PatternFill
from ruamel.yaml import YAML

# ========== Resolve git root (must happen before importing functions.*) ==========
try:
    _git_root = git.Repo(os.getcwd(), search_parent_directories=True
                         ).git.rev_parse("--show-toplevel")
except git_exc.InvalidGitRepositoryError as err:
    raise git_exc.InvalidGitRepositoryError(
        f"Script must be run from inside a git repo (cwd={os.getcwd()})."
    ) from err
if _git_root not in sys.path:
    sys.path.insert(0, _git_root)

# ========== Import custom packages ==========
import Code.functions.core_functions as cf  # pyright: ignore[reportMissingImports] # noqa: E402
import Code.functions.issue_yaml as iy  # pyright: ignore[reportMissingImports] # noqa: E402


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
    # ========== Step 1: resolve host + registry ==========
    cfg = resolve_host_config(args, pathlib.Path(_git_root))
    print(f"PS00 scan starting: system={cfg['system']} node={cfg['node']} "
          f"dataroot={cfg['dataroot']} dryrun={args.dryrun}")
    registry = load_registry(pathlib.Path(_git_root))

    # ========== Step 2: enumerate runs ==========
    runs = enumerate_runs(cfg, args.projects)
    if runs.empty:
        warn.warn("No runs found - nothing to do.")
        return

    # ========== Step 3: scan every run against the pipeline registry ==========
    state, tickets_by_run = scan_runs(runs, registry, cfg)

    # ========== Step 4: issue-YAML template generation ==========
    generate_issue_templates(state, registry, cfg, args)

    # ========== Step 4a: QC finding-ticket backstop ==========
    backfill_findings(state, cfg, args)

    # ========== Step 4b: commit + push operator run metadata ==========
    if not args.no_gitsync:
        sync_run_metadata(pathlib.Path(_git_root), args.dryrun)

    # ========== Step 5: layer-1 per-project outputs (events/state/json/xlsx) ==========
    events = write_layer1(state, tickets_by_run, cfg, args)

    # ========== Step 6: layer-2 node/sensor aggregates ==========
    write_layer2(state, tickets_by_run, cfg, args)

    # ========== Step 7: summary ==========
    print_summary(state, events)


# ==================================================================================
# ========== Host + registry ==========
# ==================================================================================
def resolve_host_config(args: argparse.Namespace,
                        repo_root: pathlib.Path) -> Dict[str, Any]:
    """Resolve the system tag, dataroot, and node for this scan.

    Defaults suit the standard layout where this repo sits at the root of
    the data tree: dataroot = repo root, node = the single node declared
    in ``NodeSummary.yaml``, system = WS.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments; ``--dataroot``/``--node``/``--system``
        override the defaults.
    repo_root : pathlib.Path
        Git repo root (holds ``NodeSummary.yaml``).

    Returns
    -------
    dict
        Keys ``system`` (str, WS or NAS), ``dataroot`` (pathlib.Path),
        ``node`` (str), ``node_root`` (pathlib.Path), ``is_ws`` (bool),
        ``reports`` (bool - always True here: build the human
        json/xlsx/csv), ``hostname`` (str).

    Raises
    ------
    ValueError
        If the node can't be resolved, the system tag is invalid, or the
        node folder does not exist.
    """
    system = args.system or "WS"
    if system not in ("WS", "NAS"):
        raise ValueError(f"--system must be WS or NAS, got '{system}'")
    node = args.node
    if node is None:
        with open(repo_root / "NodeSummary.yaml", encoding="utf-8") as f:
            nodes = [n["name"] for n in (YAML(typ="safe").load(f) or {}
                                         ).get("nodes") or []]
        if len(nodes) != 1:
            raise ValueError(
                f"NodeSummary.yaml declares {len(nodes)} nodes ({nodes}); "
                "pass --node to pick one.")
        node = nodes[0]
    dataroot = pathlib.Path(args.dataroot) if args.dataroot else repo_root
    node_root = dataroot / node
    if not node_root.is_dir():
        raise ValueError(f"Node root does not exist: {node_root}")
    return {
        "system": system, "dataroot": dataroot, "node": node,
        "node_root": node_root, "is_ws": system == "WS",
        "reports": True, "hostname": socket.gethostname(),
    }


# ==================================================================================
def load_registry(repo_root: pathlib.Path) -> Dict[str, Optional[Dict[str, Any]]]:
    """Load and schema-validate the sensor pipeline registry.

    Parameters
    ----------
    repo_root : pathlib.Path
        Repo root containing ``reference/sensor_pipelines/``.

    Returns
    -------
    dict of str to (dict or None)
        Sensor name -> ``pipeline`` block, or None for sensors without one
        ("no pipeline defined" is an explicit state, never guessed).

    Raises
    ------
    jsonschema.ValidationError
        If a sensor file fails schema validation (registry drift is a
        hard error - PS00 must never evaluate rules it can't trust).
    """
    reg_dir = repo_root / "reference" / "sensor_pipelines"
    print(f"Loading pipeline registry from {reg_dir} ...")
    with open(reg_dir / "_schema.json", encoding="utf-8") as f:
        schema = json.load(f)
    registry: Dict[str, Optional[Dict[str, Any]]] = {}
    for fpath in sorted(reg_dir.glob("*.json")):
        if fpath.name.startswith("_"):
            continue
        with open(fpath, encoding="utf-8") as f:
            spec = json.load(f)
        jsonschema.validate(spec, schema)
        registry[fpath.stem] = spec.get("pipeline")
    n_pipe = sum(1 for v in registry.values() if v is not None)
    print(f"  {len(registry)} sensors loaded, {n_pipe} with pipeline blocks.")
    return registry


# ==================================================================================
# ========== Run enumeration ==========
# ==================================================================================
def enumerate_runs(cfg: Dict[str, Any],
                   projects: Optional[List[str]]) -> pd.DataFrame:
    """Walk the node tree and enumerate every run folder.

    Parameters
    ----------
    cfg : dict
        Host config from :func:`resolve_host_config`.
    projects : list of str or None
        If given, only these project folder names are scanned.

    Returns
    -------
    pd.DataFrame
        One row per run: ``project``, ``site``, ``sensor``, ``run_date``
        (YYYYMMDD str), ``run`` (run_XX str), ``run_path`` (str),
        ``date_dir`` (str).
    """
    node_root = cfg["node_root"]
    print(f"Enumerating runs under {node_root} ...")
    skip_dirs = {"Documents", "StorageReports", "Code", "Documentation"}
    date_re = re.compile(r"^(\d{8}|\d{4}-\d{2}-\d{2})$")
    run_re = re.compile(r"^run_\d+$")
    rows: List[Dict[str, Any]] = []
    for proj_dir in sorted(node_root.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name in skip_dirs:
            continue
        if not re.match(r"^\d{4}_", proj_dir.name):
            continue
        if projects and proj_dir.name not in projects:
            continue
        for site_dir in sorted(proj_dir.iterdir()):
            if not site_dir.is_dir() or site_dir.name in skip_dirs:
                continue
            for sensor_dir in sorted(site_dir.iterdir()):
                if not sensor_dir.is_dir() or sensor_dir.name in skip_dirs:
                    continue
                for date_dir in sorted(sensor_dir.iterdir()):
                    if not date_dir.is_dir() or not date_re.match(date_dir.name):
                        continue
                    for run_dir in sorted(date_dir.iterdir()):
                        if not run_dir.is_dir() or not run_re.match(run_dir.name):
                            continue
                        rows.append({
                            "project": proj_dir.name,
                            "site": site_dir.name,
                            "sensor": sensor_dir.name,
                            "run_date": date_dir.name.replace("-", ""),
                            "run": run_dir.name,
                            "run_path": str(run_dir),
                            "date_dir": str(date_dir),
                        })
    df = pd.DataFrame(rows)
    print(f"  {len(df)} runs found across "
          f"{df['project'].nunique() if not df.empty else 0} projects.")
    return df


# ==================================================================================
# ========== Trigger + issue-YAML reading ==========
# ==================================================================================
# ==================================================================================
def resolve_intent(pipeline: Optional[Dict[str, Any]],
                   yaml_data: Optional[Dict[str, Any]],
                   ) -> Tuple[Set[str], Set[str], List[str]]:
    """Resolve intended payloads and placed deployables for a run.

    Parameters
    ----------
    pipeline : dict or None
        The sensor's pipeline block (None = no pipeline defined).
    yaml_data : dict or None
        Parsed issue YAML, or None.

    Returns
    -------
    tuple
        ``(intended_payloads, deployables_placed, invalid_names)``.
        Registry defaults (everything intended / placed) apply when no
        YAML exists. Names not in the registry are provably errors (the
        operator only ever deletes) and are returned in ``invalid_names``.
    """
    reg_payloads = set(pipeline.get("payloads", [])) if pipeline else set()
    reg_deploy = set(pipeline.get("deployables", [])) if pipeline else set()
    intended = set(reg_payloads)
    placed = set(reg_deploy)
    invalid: List[str] = []
    if yaml_data is not None:
        if "intended_payloads" in yaml_data:
            listed = {str(p) for p in (yaml_data["intended_payloads"] or [])}
            invalid += sorted(listed - reg_payloads)
            intended = listed & reg_payloads
        if "deployables_placed" in yaml_data:
            listed = {str(d) for d in (yaml_data["deployables_placed"] or [])}
            invalid += sorted(listed - reg_deploy)
            placed = listed & reg_deploy
    return intended, placed, invalid


# ==================================================================================
def fold_tickets(yaml_data: Optional[Dict[str, Any]],
                 triggers: Dict[str, bool]) -> Dict[str, Any]:
    """Fold issue-YAML tickets into per-payload states + run facts.

    Parameters
    ----------
    yaml_data : dict or None
        Parsed issue YAML, or None.
    triggers : dict
        Trigger bools from :func:`read_triggers`.

    Returns
    -------
    dict
        Keys: ``payload_states`` (dict payload -> worst-wins state),
        ``failed_payloads`` (set, closed-failed only), ``any_open``,
        ``any_investigation``, ``any_caution``, ``any_fixed``,
        ``any_failed`` (bools), ``caution_stages`` (set - the
        ``detected_stage`` values of closed caution tickets; a
        ``failed_check`` on such a stage is documented-accepted and no
        longer gates completion), ``accepted_checks`` (dict script ->
        set of check names covered by live ``accepted`` qc_findings; a
        QC report failing only on those checks is documented-tolerated
        and no longer gates completion), ``run_failure_present``,
        ``run_failure_closed`` (bools), ``run_failure_stage`` (str — the
        resolved ``detected_stage`` of the run_failure block, "" while
        TODO), ``tickets`` (list of dict summaries for the JSON report).

    Notes
    -----
    A ticket is *closed* only when its state is ok/fixed/caution/failed
    AND (except for ok) ``detected_stage`` and ``reason`` are resolved
    (not TODO), with ``note`` required for ``reason: other``. Anything
    half-closed counts as open (plan §3.1). The typed state ``wip``
    (formerly ``investigation``) marks a ticket as owned but still being
    worked; it drives ``any_investigation`` / ``under_investigation``.
    """
    severity = {"failed": 5, "wip": 4, "todo": 3, "caution": 2,
                "fixed": 1, "ok": 0}
    out: Dict[str, Any] = {
        "payload_states": {}, "failed_payloads": set(), "any_open": False,
        "any_investigation": False, "any_caution": False, "any_fixed": False,
        "any_failed": False, "caution_stages": set(),
        "accepted_checks": {},
        "run_failure_present": False,
        "run_failure_closed": False, "run_failure_stage": "", "tickets": [],
    }
    if yaml_data is None:
        return out
    out["accepted_checks"] = iy.accepted_checks(yaml_data)

    def _resolved(val: Any) -> bool:
        return val is not None and str(val).strip().lower() not in ("todo", "")

    def _closed(rec: Dict[str, Any], state: str) -> bool:
        if state == "ok":
            return True
        if state not in ("fixed", "caution", "failed"):
            return False
        if not (_resolved(rec.get("detected_stage")) and _resolved(rec.get("reason"))):
            return False
        if str(rec.get("reason", "")).strip().lower() == "other" \
                and not str(rec.get("note", "")).strip():
            return False
        return True

    for rec in (yaml_data.get("payload_outcomes") or []):
        if not isinstance(rec, dict):
            continue
        payload = str(rec.get("payload", "")).strip()
        state = str(rec.get("state", "todo")).strip().lower()
        if state not in severity:
            state = "todo"
        closed = _closed(dict(rec), state)
        if not closed:
            out["any_open"] = True
        if state == "wip":
            out["any_investigation"] = True
        if closed and state == "caution":
            out["any_caution"] = True
            out["caution_stages"].add(
                str(rec.get("detected_stage", "")).strip().lower())
        if closed and state == "fixed":
            out["any_fixed"] = True
        if closed and state == "failed":
            out["any_failed"] = True
            out["failed_payloads"].add(payload)
        prev = out["payload_states"].get(payload)
        if prev is None or severity[state] > severity[prev]:
            out["payload_states"][payload] = state
        out["tickets"].append({
            "payload": payload, "state": state, "closed": closed,
            "detected_stage": rec.get("detected_stage"),
            "reason": rec.get("reason"), "note": rec.get("note"),
        })
    rf = yaml_data.get("run_failure")
    if isinstance(rf, dict):
        out["run_failure_present"] = True
        if _resolved(rf.get("detected_stage")):
            out["run_failure_stage"] = str(rf["detected_stage"]).strip()
        rf_state_ok = _resolved(rf.get("detected_stage")) and _resolved(rf.get("reason"))
        if str(rf.get("reason", "")).strip().lower() == "other" \
                and not str(rf.get("note", "")).strip():
            rf_state_ok = False
        out["run_failure_closed"] = rf_state_ok
        if not rf_state_ok and triggers.get("RunFailed"):
            out["any_open"] = True
    return out


# ==================================================================================
def derive_run_status(triggers: Dict[str, bool], fold: Dict[str, Any],
                      yaml_state: str) -> str:
    """Derive the run status from trigger bools + folded tickets (§3.1).

    Parameters
    ----------
    triggers : dict
        Trigger bools.
    fold : dict
        Output of :func:`fold_tickets`.
    yaml_state : str
        ``absent`` / ``parsed`` / ``unparseable`` / ``invalid_names``.

    Returns
    -------
    str
        One of the §3.1 derived states; the vocabulary is open by
        contract (§5.5) so consumers must pass unknown values through.
    """
    if not any(triggers.values()):
        return "ok"
    documented = yaml_state in ("parsed", "invalid_names")
    if triggers["RunFailed"]:
        if documented and fold["run_failure_present"] and fold["run_failure_closed"]:
            return "failed_total"
        return "failed_total_undocumented"
    if not documented:
        # Trigger set but no readable YAML: everything is conservative.
        base = "issues" if triggers["Issues"] else "deviation"
        return f"{base}_undocumented"
    if fold["any_failed"]:
        base = "failed_partial"
    elif fold["any_investigation"]:
        base = "under_investigation"
    elif fold["any_caution"]:
        base = "degraded"
    elif fold["any_fixed"]:
        base = "ok_fixed"
    elif triggers["Deviations"] and not triggers["Issues"]:
        base = "ok_deviation"
    else:
        base = "issues" if fold["any_open"] else "ok"
    if fold["any_open"]:
        return f"{base}_undocumented"
    return base


# ==================================================================================
# ========== Rule evaluation ==========
# ==================================================================================
def junk_entry(name: str) -> bool:
    """Return True for filesystem junk that must not affect folder rules.

    Parameters
    ----------
    name : str
        Directory-entry name.

    Returns
    -------
    bool
        True if the entry is Synology/Windows/macOS cruft (@eaDir,
        #recycle, Thumbs.db, Zone.Identifier ADS markers, ...) - needed
        so NAS-side scans of `only`/`extras_beyond` rules agree with WS.
    """
    junk = {"@eaDir", "#recycle", "#snapshot", "Thumbs.db", "desktop.ini",
            ".DS_Store"}
    return name in junk or name.endswith(":Zone.Identifier")


# ==================================================================================
def parse_size(txt: str) -> int:
    """Parse a human size string (e.g. ``10 KB``) to bytes.

    Parameters
    ----------
    txt : str
        Size string: number + optional unit (B, KB, MB, GB, TB; 1024-based).

    Returns
    -------
    int
        Size in bytes.

    Raises
    ------
    ValueError
        If the string cannot be parsed.
    """
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]*)\s*$", txt)
    if not m:
        raise ValueError(f"Cannot parse size string '{txt}'")
    num, unit = float(m.group(1)), (m.group(2) or "B").upper()
    if unit not in units:
        raise ValueError(f"Unknown size unit '{unit}' in '{txt}'")
    return int(num * units[unit])


# ==================================================================================
def gate_met(requires: Union[None, str, List[Any]], intended: Set[str],
             placed: Set[str], failed: Set[str]) -> bool:
    """Evaluate a ``requires`` gate against the run's effective intent.

    Parameters
    ----------
    requires : None, str, or list
        Gate spec: a name, or a list ANDed together where an inner
        string-list is satisfied by ANY member (registry schema).
    intended : set of str
        Intended payloads (issue-YAML intent or registry default).
    placed : set of str
        Deployables placed.
    failed : set of str
        Payloads with closed-failed tickets (expectations shrink).

    Returns
    -------
    bool
        True when the gate is met (rule applies). Unmet = N/A, never
        failed.
    """
    effective = (intended - failed) | placed
    if requires is None:
        return True
    if isinstance(requires, str):
        return requires in effective
    for item in requires:
        if isinstance(item, str):
            if item not in effective:
                return False
        else:  # inner list = OR
            if not any(sub in effective for sub in item):
                return False
    return True


# ==================================================================================
def json_verdict(fpath: pathlib.Path, dotted: str,
                 expected: Any) -> Optional[str]:
    """Check a machine-readable verdict inside a JSON file.

    Parameters
    ----------
    fpath : pathlib.Path
        JSON file to inspect (a ``contains`` match).
    dotted : str
        Dotted path into the document (e.g. ``status.result``).
    expected : Any
        Value the lookup must equal, or a list of acceptable values
        (case-insensitive string compare).

    Returns
    -------
    str or None
        None when the verdict matches; otherwise a short failure message
        (unparseable JSON, missing path, or value mismatch all fail -
        an existing-but-failing report is failed_check, never done).
    """
    try:
        with open(fpath, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        return f"{fpath.name}: unparseable JSON ({err})"
    val: Any = data
    for part in dotted.split("."):
        val = val.get(part) if isinstance(val, dict) else None
    if val is None:
        return f"{fpath.name}: no value at '{dotted}'"
    allowed = expected if isinstance(expected, list) else [expected]
    if str(val).strip().lower() not in {str(e).strip().lower()
                                        for e in allowed}:
        return f"{fpath.name}: {dotted}={val}"
    return None


# ==================================================================================
def report_fails_accepted(fpath: pathlib.Path,
                          accepted: Dict[str, set]) -> bool:
    """Decide whether accepted findings cover a failing contract QC report.

    Mirrors the gating-fail definition in
    :func:`Code.functions.issue_yaml.ensure_finding_tickets` (``fail``,
    not ``advisory``, not ``waived``) so the waiver can only reach
    checks a finding could have been authored for. Deferred classes
    (QC03 entirely, QC01 ``reflectance_product_*``) never get findings,
    so a report failing on them is never covered - by design.

    Parameters
    ----------
    fpath : pathlib.Path
        A ``*_detail.json`` contract report whose verdict failed.
    accepted : dict of str -> set of str
        ``script name -> accepted check names`` from
        :func:`Code.functions.issue_yaml.accepted_checks`.

    Returns
    -------
    bool
        True when the report names at least one gating fail and every
        gating fail is an accepted check of that script. Unparseable or
        non-contract reports (no ``script.name`` / ``checks``) are never
        covered.
    """
    try:
        with open(fpath, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    script = str((data.get("script") or {}).get("name", ""))
    checks = data.get("checks") or {}
    gating = {n for n, c in checks.items()
              if isinstance(c, dict) and c.get("status") == "fail"
              and not c.get("advisory") and not c.get("waived")}
    deferred = iy.finding_exclusions(script)
    if deferred and any(n.startswith(deferred) for n in gating):
        return False
    return bool(gating) and gating <= accepted.get(script, set())


# ==================================================================================
def evaluate_rule(run_path: pathlib.Path,
                  rule: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate one ``done_when`` rule against a run folder.

    Parameters
    ----------
    run_path : pathlib.Path
        The run folder.
    rule : dict
        A registry ``doneRule`` (gate already checked by the caller).

    Returns
    -------
    dict
        ``status`` (pass / failed_check / folder_missing / folder_empty /
        no_match), ``matches`` (list of pathlib.Path), ``message`` (str);
        a ``json_path`` verdict failure also carries ``failing_reports``
        (the report files whose verdict failed) so the caller can decide
        whether accepted findings cover the failure.
    """
    # +++++ any_of: alternative sub-rules, first pass wins +++++
    if "any_of" in rule:
        results = [evaluate_rule(run_path, sub) for sub in rule["any_of"]]
        for res in results:
            if res["status"] == "pass":
                return res
        order = ["failed_check", "no_match", "folder_empty",
                 "folder_missing"]
        status = next(s for s in order
                      if any(r["status"] == s for r in results))
        matches = sorted({m for r in results for m in r["matches"]})
        message = "; ".join(dict.fromkeys(
            r["message"] for r in results if r["message"]))
        out = {"status": status, "matches": matches, "message": message}
        reports = [p for r in results for p in r.get("failing_reports", [])]
        if reports:
            out["failing_reports"] = reports
        return out

    folders = sorted(p for p in run_path.glob(rule["folder"]) if p.is_dir())
    label = rule["folder"]
    if not folders:
        return {"status": "folder_missing", "matches": [],
                "message": f"{label}: folder absent"}

    def _entries(folder: pathlib.Path) -> List[pathlib.Path]:
        return [e for e in folder.iterdir() if not junk_entry(e.name)]

    # +++++ contains (+ min_count / max_count / min_size) +++++
    if "contains" in rule:
        patterns = rule["contains"]
        if isinstance(patterns, str):
            patterns = [patterns]
        matches: List[pathlib.Path] = []
        for folder in folders:
            for pat in patterns:
                matches += [m for m in folder.glob(pat) if not junk_entry(m.name)]
        matches = sorted(set(matches))
        min_count = int(rule.get("min_count", 1))
        if not matches:
            if all(len(_entries(f)) == 0 for f in folders):
                return {"status": "folder_empty", "matches": [],
                        "message": f"{label}: folder empty"}
            return {"status": "no_match", "matches": [],
                    "message": f"{label}: no match for {patterns}"}
        if "min_size" in rule:
            floor = parse_size(rule["min_size"])
            undersized = [m for m in matches
                          if m.is_file() and m.stat().st_size < floor]
            if undersized:
                sizes = ", ".join(f"{u.name}={u.stat().st_size} B"
                                  for u in undersized[:3])
                return {"status": "failed_check", "matches": matches,
                        "message": (f"{label}: {len(undersized)} match(es) under "
                                    f"min_size {rule['min_size']} ({sizes})")}
        if len(matches) < min_count:
            return {"status": "no_match", "matches": matches,
                    "message": (f"{label}: {len(matches)} match(es) < "
                                f"min_count {min_count}")}
        # +++++ max_count: too many matches is a blocked state, not done +++++
        if "max_count" in rule and len(matches) > int(rule["max_count"]):
            shown = ", ".join(m.name for m in matches[:3])
            return {"status": "failed_check", "matches": matches,
                    "message": (f"{label}: {len(matches)} match(es) > "
                                f"max_count {rule['max_count']} ({shown})")}
        # +++++ json_path / json_equals: machine-readable verdict inside +++++
        if "json_path" in rule:
            failing_reports = [m for m in matches if m.is_file()
                               and json_verdict(m, rule["json_path"],
                                                rule["json_equals"])]
            if failing_reports:
                failing = [json_verdict(m, rule["json_path"],
                                        rule["json_equals"])
                           for m in failing_reports]
                shown = "; ".join(failing[:3])
                return {"status": "failed_check", "matches": matches,
                        "failing_reports": failing_reports,
                        "message": (f"{label}: {len(failing)} match(es) fail "
                                    f"{rule['json_path']} == "
                                    f"'{rule['json_equals']}' ({shown})")}
        rest: Dict[str, Any] = {"status": "pass", "matches": matches,
                                "message": ""}
    else:
        rest = {"status": "pass", "matches": [], "message": ""}

    # +++++ only: nothing but the keepers +++++
    if rest["status"] == "pass" and "only" in rule:
        keepers = list(rule["only"])
        for folder in folders:
            extras = [e.name for e in _entries(folder)
                      if not any(fnmatch.fnmatch(e.name, k) for k in keepers)]
            if extras:
                shown = ", ".join(extras[:3])
                return {"status": "no_match", "matches": rest["matches"],
                        "message": (f"{label}: {len(extras)} entr(ies) beyond "
                                    f"{keepers} ({shown})")}

    # +++++ extras_beyond: at least one non-keeper entry +++++
    if rest["status"] == "pass" and "extras_beyond" in rule:
        keepers = list(rule["extras_beyond"])
        any_extra = False
        for folder in folders:
            entries = _entries(folder)
            if any(not any(fnmatch.fnmatch(e.name, k) for k in keepers)
                   for e in entries):
                any_extra = True
                break
        if not any_extra:
            if all(len(_entries(f)) == 0 for f in folders):
                return {"status": "folder_empty", "matches": rest["matches"],
                        "message": f"{label}: folder empty"}
            return {"status": "no_match", "matches": rest["matches"],
                    "message": f"{label}: only {keepers} present"}
    return rest


# ==================================================================================
def evaluate_stage(run_path: pathlib.Path, step: Dict[str, Any],
                   intended: Set[str], placed: Set[str],
                   failed: Set[str], duplicate: bool = False) -> Dict[str, Any]:
    """Evaluate one pipeline stage (all its ``done_when`` rules).

    Parameters
    ----------
    run_path : pathlib.Path
        The run folder.
    step : dict
        Registry ``pipelineStep``.
    intended : set of str
        Intended payloads.
    placed : set of str
        Deployables placed.
    failed : set of str
        Closed-failed payloads.
    duplicate : bool
        True when the run is a duplicate (RunOverview ``DuplicateRun``);
        rules marked ``primary_only`` are then N/A — skipped, never
        failed.

    Returns
    -------
    dict
        ``stage_status`` (done / missing / empty / failed_check /
        not_applicable), ``applicable`` (bool), ``artefact_count`` (int),
        ``newest_mtime`` (pd.Timestamp or NaT), ``message`` (str) and
        ``failed_checks`` (list of (failed-rule-count, [report paths]) —
        one entry per rule that read ``failed_check``, empty paths when
        the failure was not a report verdict; consumed by the
        accepted-finding waiver in :func:`evaluate_run` and stripped
        before the row is recorded).
    """
    rules = step["done_when"]
    if isinstance(rules, dict):
        rules = [rules]
    applicable_rules = [r for r in rules
                        if not (duplicate and r.get("primary_only"))
                        and gate_met(r.get("requires"), intended, placed, failed)]
    if not applicable_rules:
        return {"stage_status": "not_applicable", "applicable": False,
                "artefact_count": 0, "newest_mtime": pd.NaT,
                "message": "all rules gated off by run intent"
                           + (" / duplicate-run exemption" if duplicate else ""),
                "failed_checks": []}
    results = [evaluate_rule(run_path, r) for r in applicable_rules]
    matches = [m for r in results for m in r["matches"]]
    mtimes = [m.stat().st_mtime for m in matches if m.exists()]
    newest = (pd.Timestamp(max(mtimes), unit="s") if mtimes else pd.NaT)
    statuses = [r["status"] for r in results]
    seen: List[str] = []
    for r in results:
        if r["message"] and r["message"] not in seen:
            seen.append(r["message"])
    fails = "; ".join(seen)
    if all(s == "pass" for s in statuses):
        status = "done"
    elif "failed_check" in statuses:
        status = "failed_check"
    elif all(s in ("folder_missing", "folder_empty") for s in statuses) \
            and "folder_empty" in statuses:
        status = "empty"
    else:
        status = "missing"
    failed_checks = [r.get("failing_reports", []) for r in results
                     if r["status"] == "failed_check"]
    return {"stage_status": status, "applicable": True,
            "artefact_count": len(matches), "newest_mtime": newest,
            "message": fails, "failed_checks": failed_checks}


# ==================================================================================
def evaluate_payloads(run_path: pathlib.Path, step: Dict[str, Any],
                      stage_res: Dict[str, Any], intended: Set[str],
                      failed: Set[str], attempt_grace_h: float = 12.0,
                      ) -> List[Tuple[str, Dict[str, Any]]]:
    """Grade one stage's per-payload artefact expectations.

    A payload with no matching artefact is graded relative to the stage's
    own evidence. While the stage roll-up is not ``done`` the pipeline has
    not run yet, so the payload is ``missing`` (work still owed —
    ``pending``). Once the stage evidence exists an attempt has been made,
    and processing never takes more than ``attempt_grace_h`` hours once
    started — so a product still absent after the stage's newest artefact
    is older than the grace window means the attempt finished without it:
    ``failed_check`` (→ run ``blocked``). Inside the window it stays
    ``missing`` (the attempt may still be running).

    Parameters
    ----------
    run_path : pathlib.Path
        The run folder.
    step : dict
        Registry ``pipelineStep``.
    stage_res : dict
        This stage's roll-up result from :func:`evaluate_stage` (after
        ephemeral inference).
    intended : set of str
        Intended payloads.
    failed : set of str
        Closed-failed payloads (expectations already shrunk).
    attempt_grace_h : float, optional
        Maximum age (hours) of the stage's newest artefact for a missing
        product to still count as in-flight rather than failed.

    Returns
    -------
    list of (str, dict)
        ``(payload, result)`` pairs in registry order; result fields match
        :func:`evaluate_stage` output.
    """
    # Artefact mtimes are epoch-based (UTC-naive) — compare against UTC now.
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    rows: List[Tuple[str, Dict[str, Any]]] = []
    for payload, pat in (step.get("payload_artefacts") or {}).items():
        if payload in failed:
            pres = {"stage_status": "not_applicable", "applicable": False,
                    "artefact_count": 0, "newest_mtime": pd.NaT,
                    "message": "payload failed (closed ticket); not expected"}
        elif payload not in intended:
            pres = {"stage_status": "not_applicable", "applicable": False,
                    "artefact_count": 0, "newest_mtime": pd.NaT,
                    "message": "payload not intended for this run"}
        else:
            marr = sorted(run_path.glob(pat))
            mt = [m.stat().st_mtime for m in marr if m.exists()]
            if marr:
                pres = {"stage_status": "done", "applicable": True,
                        "artefact_count": len(marr),
                        "newest_mtime": (pd.Timestamp(max(mt), unit="s")
                                         if mt else pd.NaT),
                        "message": ""}
            else:
                attempted = stage_res["stage_status"] == "done"
                in_grace = (pd.notna(stage_res["newest_mtime"])
                            and now - stage_res["newest_mtime"]
                            <= pd.Timedelta(hours=attempt_grace_h))
                if attempted and not in_grace:
                    pres = {"stage_status": "failed_check",
                            "applicable": True, "artefact_count": 0,
                            "newest_mtime": pd.NaT,
                            "message": f"stage attempted but no match for "
                                       f"{pat} (evidence older than "
                                       f"{attempt_grace_h:.0f} h — the "
                                       f"attempt failed)"}
                elif attempted:
                    pres = {"stage_status": "missing", "applicable": True,
                            "artefact_count": 0, "newest_mtime": pd.NaT,
                            "message": f"no match for {pat} (attempt "
                                       f"< {attempt_grace_h:.0f} h old — "
                                       f"may still be running)"}
                else:
                    pres = {"stage_status": "missing", "applicable": True,
                            "artefact_count": 0, "newest_mtime": pd.NaT,
                            "message": f"no match for {pat}"}
        rows.append((payload, pres))
    return rows


# ==================================================================================
def evaluate_run(run_row: pd.Series, pipeline: Optional[Dict[str, Any]],
                 triggers: Dict[str, bool], yaml_data: Optional[Dict[str, Any]],
                 yaml_state: str, fold: Dict[str, Any],
                 duplicate: bool = False,
                 ) -> Tuple[List[Dict[str, Any]], str]:
    """Evaluate every stage (and payload artefact) of one run.

    Parameters
    ----------
    run_row : pd.Series
        One row from :func:`enumerate_runs`.
    pipeline : dict or None
        The sensor's pipeline block.
    triggers : dict
        Trigger bools.
    yaml_data : dict or None
        Parsed issue YAML.
    yaml_state : str
        Issue-YAML state string.
    fold : dict
        Output of :func:`fold_tickets`.
    duplicate : bool
        RunOverview ``DuplicateRun`` flag (see
        :func:`Code.functions.issue_yaml.read_duplicate`).

    Returns
    -------
    tuple
        ``(records, run_status)`` - a list of state-row dicts (stage rows
        with ``payload=""``, per-payload artefact rows, and ``ticket``
        rows — the latter carry ``detected_stage`` and a ``ticket_open``
        bool, additive columns that are NaN on non-ticket rows) plus the
        derived run status. Every record carries the run-level
        ``completion`` roll-up (see :func:`derive_completion`).
    """
    run_path = pathlib.Path(run_row["run_path"])
    intended, placed, invalid = resolve_intent(pipeline, yaml_data)
    if invalid:
        yaml_state = "invalid_names"
        warn.warn(f"{run_path}: issue YAML names not in registry "
                  f"(hand-authored or stale template?): {invalid}")
    run_status = derive_run_status(triggers, fold, yaml_state)
    base = {k: run_row[k] for k in ("project", "site", "sensor", "run_date", "run")}
    base.update({"deviations": triggers["Deviations"],
                 "issues": triggers["Issues"],
                 "run_failed": triggers["RunFailed"],
                 "duplicate": duplicate,
                 "yaml_state": yaml_state, "run_status": run_status})
    records: List[Dict[str, Any]] = []

    if pipeline is None:
        records.append({**base, "completion": "no_pipeline",
                        "stage": "", "payload": "",
                        "stage_status": "no_pipeline", "applicable": False,
                        "artefact_count": 0, "newest_mtime": pd.NaT,
                        "message": "no pipeline defined for this sensor"})
        return records, run_status

    failed = set(fold["failed_payloads"])
    steps = pipeline["steps"]
    # +++++ first pass: evaluate each stage on its own evidence +++++
    stage_results = [evaluate_stage(run_path, s, intended, placed, failed,
                                    duplicate)
                     for s in steps]
    # +++++ second pass: ephemeral inference (evidence destroyed later) +++++
    for i, step in enumerate(steps):
        if step.get("ephemeral") and stage_results[i]["stage_status"] not in ("done",):
            if any(r["stage_status"] == "done" for r in stage_results[i + 1:]):
                stage_results[i]["stage_status"] = "done"
                stage_results[i]["message"] = \
                    "inferred done: ephemeral evidence removed by a later stage"
    # +++++ third pass: attempt-aware payload grading + stage escalation +++++
    # A stage whose own evidence is done but whose attempt (older than the
    # grace window) left an intended product missing did not succeed: the
    # payload row reads failed_check and the roll-up follows, so the run
    # gates to blocked (waivable below, like any failed_check).
    payload_results = [evaluate_payloads(run_path, step, res, intended, failed)
                       for step, res in zip(steps, stage_results)]
    for res, prows in zip(stage_results, payload_results):
        bad = [p for p, pr in prows if pr["stage_status"] == "failed_check"]
        if bad and res["stage_status"] == "done":
            res["stage_status"] = "failed_check"
            res["message"] = ((res["message"] + " | ") if res["message"]
                              else "") + \
                "attempted but no product for payload(s): " + ", ".join(bad)
    # +++++ fourth pass: closed caution tickets waive their stage's failed_check
    # (documented-accepted: still visible as failed_check, no longer a
    # completion gate; 'fixed' never waives - a fixed claim against a
    # still-failing check leaves the run blocked) +++++
    for step, res in zip(steps, stage_results):
        if res["stage_status"] == "failed_check" \
                and step["name"].lower() in fold["caution_stages"]:
            res["waived"] = True
            res["message"] += " | waived: closed caution ticket on this stage"
    # +++++ fifth pass: accepted QC findings waive a report-verdict failure
    # when every failing rule is a report whose gating fails are all
    # covered (check-scoped: the measured status stays 'fail', the run
    # is documented-tolerated - QC findings plan §3.5 'accept' exit) +++++
    for res in stage_results:
        failed_checks = res.pop("failed_checks", [])
        if res["stage_status"] != "failed_check" or res.get("waived"):
            continue
        if failed_checks and all(
                reports and all(report_fails_accepted(p, fold["accepted_checks"])
                                for p in reports)
                for reports in failed_checks):
            res["waived"] = True
            res["message"] += " | waived: accepted QC finding(s) cover every failing check"
    base["completion"] = derive_completion(stage_results, run_status,
                                           str(run_row["run_date"]))
    for step, res, prows in zip(steps, stage_results, payload_results):
        records.append({**base, "stage": step["name"], "payload": "", **res})
        # +++++ per-payload artefact rows (graded above) +++++
        for payload, pres in prows:
            records.append({**base, "stage": step["name"], "payload": payload,
                            **pres})
    # +++++ ticket rows (issue-YAML lifecycle rides the same diff machinery) +++++
    # Ticket rows also carry the ticket's resolved detected_stage ("" while
    # TODO) — additive column, NaN on non-ticket rows; consumers that don't
    # know it ignore it (§5.5 open-contract rule).
    tstage = {str(t["payload"]): (str(t.get("detected_stage") or "").strip()
                                  if str(t.get("detected_stage") or ""
                                         ).strip().lower() != "todo" else "")
              for t in fold["tickets"]}
    # ticket_open: additive bool column (NaN on non-ticket rows) — True only
    # while documentation is still owed, so consumers can hide closed tickets.
    # A closed run_failure block documents the whole run (same rule that
    # yields run_status "failed_total"), so leftover payload TODOs are moot.
    run_documented = triggers["RunFailed"] and fold["run_failure_closed"]
    open_payloads = (set() if run_documented else
                     {str(t["payload"]) for t in fold["tickets"]
                      if not t["closed"]})
    for payload, tstate in sorted(fold["payload_states"].items()):
        records.append({**base, "stage": "ticket", "payload": payload,
                        "stage_status": tstate, "applicable": True,
                        "artefact_count": 0, "newest_mtime": pd.NaT,
                        "detected_stage": tstage.get(payload, ""),
                        "ticket_open": payload in open_payloads,
                        "message": ""})
    if fold["run_failure_present"]:
        records.append({**base, "stage": "ticket", "payload": "_run",
                        "stage_status": ("closed" if fold["run_failure_closed"]
                                         else "open"),
                        "applicable": True, "artefact_count": 0,
                        "newest_mtime": pd.NaT,
                        "detected_stage": fold["run_failure_stage"],
                        "ticket_open": not fold["run_failure_closed"],
                        "message": "run_failure block"})
    return records, run_status


# ==================================================================================
def derive_completion(stage_results: List[Dict[str, Any]],
                      run_status: str, run_date: str,
                      stall_days: int = 14) -> str:
    """Roll the per-stage facts into one per-run completion category.

    Parameters
    ----------
    stage_results : list of dict
        Whole-stage evaluation results (post ephemeral inference).
    run_status : str
        Derived outcome status (§3.1).
    run_date : str
        Capture date (YYYYMMDD) — the stall-age fallback when no stage
        has any artefact mtime yet.
    stall_days : int, optional
        Age threshold splitting ``pending`` from ``stalled`` (D-P9-4:
        14 d global; per-sensor registry override deferred).

    Returns
    -------
    str
        ``complete`` - every *applicable* stage is done: expectations were
        already shrunk by intent edits, deployable gates, and closed-failed
        payloads, so a documented loss never blocks completion;
        ``pending`` - applicable stages remain and something moved within
        the last ``stall_days`` days (healthy but unfinished);
        ``stalled`` - applicable stages remain and the newest artefact
        (or, with no artefacts at all, the capture date) is older than
        ``stall_days`` days - the run has been forgotten;
        ``blocked`` - a stage fails a quality rule (``failed_check``) and
        needs rework before the run can ever complete. A closed
        ``caution`` ticket whose ``detected_stage`` names the failing
        stage *waives* the gate: the failure stays visible as
        ``failed_check`` but is documented-accepted and counts as
        satisfied here (``fixed`` never waives - if the check still
        fails, the fix claim is contradicted and the run stays blocked).
        Likewise a QC report whose gating fails are all covered by closed
        ``accepted`` qc_findings waives its verdict rule (check-scoped);
        ``failed`` - the run is condemned (``failed_total``, incl. the
        undocumented-conservative case) so completion is moot.

    Notes
    -----
    Orthogonal to ``run_status``: a ``failed_partial`` run whose surviving
    payloads finished reads ``complete``, and a clean-but-unprocessed run
    reads ``run_status=ok`` + ``completion=pending`` ("ok" only ever means
    "no issues declared", never "nothing left to do").
    """
    if run_status.startswith("failed_total"):
        return "failed"
    if any(r["stage_status"] == "failed_check" and not r.get("waived")
           for r in stage_results):
        return "blocked"
    if all(r["stage_status"] in ("done", "not_applicable")
           or (r["stage_status"] == "failed_check" and r.get("waived"))
           for r in stage_results):
        return "complete"
    # +++++ pending vs stalled: age of the newest artefact anywhere +++++
    mtimes = [r["newest_mtime"] for r in stage_results
              if pd.notna(r["newest_mtime"])]
    newest = (max(mtimes) if mtimes
              else pd.to_datetime(run_date, format="%Y%m%d", errors="coerce"))
    if pd.notna(newest) \
            and pd.Timestamp.now() - newest > pd.Timedelta(days=stall_days):
        return "stalled"
    return "pending"


# ==================================================================================
def scan_runs(runs: pd.DataFrame,
              registry: Dict[str, Optional[Dict[str, Any]]],
              cfg: Dict[str, Any],
              ) -> Tuple[pd.DataFrame, Dict[str, List[Dict[str, Any]]]]:
    """Scan every enumerated run and build the tidy state table.

    Parameters
    ----------
    runs : pd.DataFrame
        Output of :func:`enumerate_runs`.
    registry : dict
        Sensor -> pipeline block (or None).
    cfg : dict
        Host config.

    Returns
    -------
    tuple
        ``(state, tickets_by_run)``: the tidy state DataFrame (one row
        per run x stage x payload) and a dict of run key -> ticket
        summaries for the JSON reports.
    """
    print(f"Scanning {len(runs)} runs ...")
    as_of = pd.Timestamp.now().floor("s")
    all_records: List[Dict[str, Any]] = []
    tickets_by_run: Dict[str, List[Dict[str, Any]]] = {}
    for _, row in tqdm(runs.iterrows(), total=len(runs), desc="runs"):
        date_dir = pathlib.Path(row["date_dir"])
        parsed = cf.parse_APPN_dataset_path(pathlib.Path(row["run_path"]),
                                            path_level="run")
        if not parsed["valid"]:
            warn.warn(f"{row['run_path']}: path failed parse_APPN_dataset_path "
                      f"({parsed['errors']}); using raw folder names")
        triggers = iy.read_triggers(date_dir, row["run"])
        duplicate = iy.read_duplicate(date_dir, row["run"])
        yaml_data, yaml_state = iy.load_issue_yaml(date_dir, row["run"])
        fold = fold_tickets(yaml_data, triggers)
        pipeline = registry.get(row["sensor"])
        records, _ = evaluate_run(row, pipeline, triggers, yaml_data,
                                  yaml_state, fold, duplicate)
        run_key = "_".join([row["project"], row["site"], row["sensor"],
                            row["run_date"], row["run"]])
        if fold["tickets"]:
            tickets_by_run[run_key] = fold["tickets"]
        all_records += records
    state = pd.DataFrame(all_records)
    state.insert(0, "as_of", as_of)
    state.insert(1, "system", cfg["system"])
    state.insert(2, "node", cfg["node"])
    state["newest_mtime"] = pd.to_datetime(state["newest_mtime"])
    return state, tickets_by_run


# ==================================================================================
# ========== Issue-YAML template generation (any host; shared logic) ==========
# ==================================================================================
def generate_issue_templates(state: pd.DataFrame,
                             registry: Dict[str, Optional[Dict[str, Any]]],
                             cfg: Dict[str, Any],
                             args: argparse.Namespace) -> None:
    """Create/patch pre-filled issue-YAML templates for triggered runs.

    Parameters
    ----------
    state : pd.DataFrame
        Tidy state table from :func:`scan_runs`.
    registry : dict
        Sensor -> pipeline block.
    cfg : dict
        Host config.
    args : argparse.Namespace
        ``--dryrun`` blocks all writes.

    Returns
    -------
    None

    Notes
    -----
    Any host may generate (the files are git-tracked operator metadata,
    same class as RunOverview.csv; the trigger bool only reaches the
    other host via the sync that also delivers the template, so
    concurrent creation is self-limiting). ProjectBuilder.py shares the
    same ``Code.functions.issue_yaml`` logic for on-demand creation.
    """
    write_enabled = not args.dryrun
    mode = "WRITING" if write_enabled else "dry-run"
    print(f"Issue-YAML template generation [{mode}] ...")
    runs = state[(state["deviations"] | state["issues"] | state["run_failed"])]
    run_keys = runs[["project", "site", "sensor", "run_date", "run"]
                    ].drop_duplicates()
    planned = 0
    for _, rk in run_keys.iterrows():
        sub = state[(state[list(rk.index)] == rk.values).all(axis=1)]
        date_dir = _run_dir_from_state(cfg, rk).parent
        triggers = {"Deviations": bool(sub["deviations"].iloc[0]),
                    "Issues": bool(sub["issues"].iloc[0]),
                    "RunFailed": bool(sub["run_failed"].iloc[0])}
        # +++++ scan evidence: payload -> any artefacts found +++++
        prows = sub[(sub["payload"] != "") & (sub["stage"] != "ticket")
                    & (sub["stage_status"] != "not_applicable")]
        evidence = {str(p): bool((g["artefact_count"] > 0).any())
                    for p, g in prows.groupby("payload")}
        action = iy.ensure_issue_yaml(date_dir, rk["run"], rk["sensor"],
                                      triggers, registry.get(rk["sensor"]),
                                      evidence=evidence, write=write_enabled)
        if action:
            planned += 1
            label = "CREATE" if action == "created" else "PATCH "
            print(f"  {label} {date_dir / (rk['run'] + '_Issues.yaml')}"
                  + (f": {action}" if action != "created" else ""))
    print(f"  {planned} template file(s) "
          f"{'written/patched' if write_enabled else 'planned'}.")


# ==================================================================================
def backfill_findings(state: pd.DataFrame, cfg: Dict[str, Any],
                      args: argparse.Namespace) -> None:
    """Author/refresh/auto-close QC finding-tickets from existing reports.

    Scheduled backstop for the QC scripts' inline authoring
    (QC findings plan step 5, development-master repo): covers runs whose
    contract reports predate the finding hooks and will not re-run
    soon. Same helper, same idempotent additive patches — the two
    writers author identical identities because the grouping policy
    lives in ``issue_yaml.finding_groups``.

    Parameters
    ----------
    state : pd.DataFrame
        Tidy state table from :func:`scan_runs`.
    cfg : dict
        Host config.
    args : argparse.Namespace
        ``--dryrun`` blocks all writes.

    Returns
    -------
    None
    """
    write_enabled = not args.dryrun
    mode = "WRITING" if write_enabled else "dry-run"
    print(f"QC finding-ticket backstop [{mode}] ...")
    run_keys = state[["project", "site", "sensor", "run_date", "run"]
                     ].drop_duplicates()
    touched = 0
    for _, rk in run_keys.iterrows():
        run_dir = _run_dir_from_state(cfg, rk)
        qc_data = run_dir / "T1_proc" / "QC_data"
        # ponytail: parses every detail JSON each scan (MBs on QC03 runs);
        # gate on the summary YAML status if scans get slow
        for det in sorted(qc_data.glob("QC0*/QC0*_detail.json")):
            try:
                with open(det, encoding="utf-8") as fh:
                    report = json.load(fh)
            except (OSError, json.JSONDecodeError) as err:
                warn.warn(f"Skipping unreadable detail report {det}: {err}")
                continue
            actions = iy.ensure_finding_tickets(
                run_dir.parent, str(rk["run"]), report,
                write=write_enabled)
            if actions:
                touched += 1
                print(f"  {run_dir.parent / (str(rk['run']) + '_Issues.yaml')}"
                      f": {', '.join(actions)}")
    print(f"  {touched} report(s) produced finding actions "
          f"({'written' if write_enabled else 'planned'}).")


# ==================================================================================
def sync_run_metadata(repo_root: pathlib.Path, dryrun: bool = False) -> None:
    """Commit and push operator run metadata changed in the working tree.

    Scope is strictly the per-run operator metadata git tracks: modified
    ``*_Issues.yaml`` and ``RunOverview.csv`` files anywhere in the repo,
    plus untracked ``*_Issues.yaml`` (templates generated by this or an
    earlier scan). Nothing else is ever staged — code, reports, and any
    other working-tree changes are left alone, and the commit is skipped
    entirely (with a warning) if someone already has unrelated changes
    staged, so their staging area is never hijacked.

    Push strategy: plain push first; on rejection (another host pushed
    since) one ``pull --rebase --autostash`` then push again. Any git
    failure is a warning, never a crash — the metadata stays committed
    locally and the next scheduled scan retries the push.

    Parameters
    ----------
    repo_root : pathlib.Path
        The data repo root (PS00 always runs from it).
    dryrun : bool, optional
        Print what would be committed and stop before touching git state.

    Returns
    -------
    None
    """
    print(f"Run-metadata git sync [{'dry-run' if dryrun else 'WRITING'}] ...")
    repo = git.Repo(repo_root)

    def _wanted(path: str) -> bool:
        name = pathlib.PurePosixPath(path).name
        return name == "RunOverview.csv" or name.endswith("_Issues.yaml")

    changed = [d.a_path for d in repo.index.diff(None)
               if d.a_path and _wanted(d.a_path)]
    fresh = [p for p in repo.untracked_files if _wanted(p)]
    paths = sorted(set(changed) | set(fresh))
    if not paths:
        print("  no run-metadata changes to commit.")
        return
    for p in paths:
        print(f"  {'new' if p in fresh else 'mod'}  {p}")
    if dryrun:
        print(f"  dry-run: would commit + push {len(paths)} file(s).")
        return
    staged = [d.a_path for d in repo.index.diff("HEAD")]
    foreign = [p for p in staged if p not in paths and not _wanted(p)]
    if foreign:
        warn.warn("Unrelated changes are already staged "
                  f"({foreign[:3]}{' ...' if len(foreign) > 3 else ''}); "
                  "skipping the metadata auto-commit to avoid committing "
                  "them. Commit or unstage those first.")
        return
    repo.index.add(paths)
    hostname = socket.gethostname()
    msg = (f"PS00 auto-sync [{hostname}]: run metadata "
           f"({len(paths)} file(s))\n\n" + "\n".join(paths))
    repo.index.commit(msg)
    print(f"  committed {len(paths)} file(s).")
    branch = repo.active_branch.name
    try:
        repo.git.push("origin", branch)
        print(f"  pushed to origin/{branch}.")
    except git_exc.GitCommandError:
        print("  push rejected — rebasing onto origin and retrying ...")
        try:
            repo.git.pull("--rebase", "--autostash", "origin", branch)
            repo.git.push("origin", branch)
            print(f"  pushed to origin/{branch} after rebase.")
        except git_exc.GitCommandError as err:
            for d in ("rebase-merge", "rebase-apply"):
                if (pathlib.Path(repo.git_dir) / d).exists():
                    repo.git.rebase("--abort")
            warn.warn("git push failed — metadata is committed locally and "
                      f"the next scan will retry the push: {err}")


# ==================================================================================
def _run_dir_from_state(cfg: Dict[str, Any], rk: pd.Series) -> pathlib.Path:
    """Rebuild the run folder path from a state key row.

    Parameters
    ----------
    cfg : dict
        Host config.
    rk : pd.Series
        Key row with project/site/sensor/run_date/run.

    Returns
    -------
    pathlib.Path
        The run folder path.
    """
    return (cfg["node_root"] / rk["project"] / rk["site"] / rk["sensor"]
            / rk["run_date"] / rk["run"])


# ==================================================================================
# ========== Event diffing + layer-1 outputs ==========
# ==================================================================================
def diff_events(prev: Optional[pd.DataFrame], new: pd.DataFrame,
                cfg: Dict[str, Any]) -> pd.DataFrame:
    """Diff a project's new state against its previous state -> events.

    Parameters
    ----------
    prev : pd.DataFrame or None
        Previous ``state_<SYS>.parquet`` contents (None = first scan).
    new : pd.DataFrame
        This scan's state rows for the project.
    cfg : dict
        Host config.

    Returns
    -------
    pd.DataFrame
        Append-only event rows (one per observed transition). On the
        first scan every row emits a baseline event (``old_status`` "")
        so state is fully regenerable from the ledger.
    """
    keys = ["project", "site", "sensor", "run_date", "run", "stage", "payload"]
    run_keys = ["project", "site", "sensor", "run_date", "run"]
    ev: List[Dict[str, Any]] = []
    as_of = new["as_of"].iloc[0]

    def _event(row: pd.Series, etype: str, stage: str, payload: str,
               old: str, newv: str, message: str = "") -> None:
        ev.append({"date": as_of, "system": cfg["system"], "node": cfg["node"],
                   "project": row["project"], "site": row["site"],
                   "sensor": row["sensor"], "run_date": row["run_date"],
                   "run": row["run"], "event_type": etype, "stage": stage,
                   "payload": payload, "old_status": old, "new_status": newv,
                   "reason": "", "message": message})

    prev_idx = (prev.set_index(keys) if prev is not None and not prev.empty
                else None)
    prev_runs = (set(map(tuple, prev[run_keys].drop_duplicates().values))
                 if prev is not None and not prev.empty else set())
    seen_runs: Set[Tuple[Any, ...]] = set()
    for _, row in new.iterrows():
        rk = tuple(row[k] for k in run_keys)
        if rk not in prev_runs and rk not in seen_runs:
            _event(row, "run", "", "", "", "observed",
                   f"run first observed (run_status={row['run_status']})")
            seen_runs.add(rk)
        old_status = ""
        old_row = None
        if prev_idx is not None:
            k = tuple(row[c] for c in keys)
            if k in prev_idx.index:
                old_row = prev_idx.loc[k]
                old_status = str(old_row["stage_status"])
        if old_status != str(row["stage_status"]):
            etype = "ticket" if row["stage"] == "ticket" else "stage"
            _event(row, etype, row["stage"], row["payload"], old_status,
                   str(row["stage_status"]), str(row["message"]))
    # +++++ run-level transitions (triggers / yaml_state / run_status) +++++
    new_runs = new.drop_duplicates(subset=run_keys)
    prev_run_idx = (prev.drop_duplicates(subset=run_keys).set_index(run_keys)
                    if prev is not None and not prev.empty else None)
    watched = [("deviations", "Deviations", "run"),
               ("issues", "Issues", "run"),
               ("run_failed", "RunFailed", "run"),
               ("yaml_state", "yaml_state", "ticket"),
               ("run_status", "run_status", "run"),
               ("completion", "completion", "run")]
    for _, row in new_runs.iterrows():
        old_row = None
        if prev_run_idx is not None:
            k = tuple(row[c] for c in run_keys)
            if k in prev_run_idx.index:
                old_row = prev_run_idx.loc[k]
        for col, label, etype in watched:
            old_val = "" if old_row is None else str(old_row.get(col, ""))
            new_val = str(row[col])
            if old_val != new_val:
                # Skip noisy False baselines on first observation.
                if old_row is None and new_val in ("False",):
                    continue
                _event(row, etype, label, "", old_val, new_val)
    return pd.DataFrame(ev)


# ==================================================================================
def write_layer1(state: pd.DataFrame,
                 tickets_by_run: Dict[str, List[Dict[str, Any]]],
                 cfg: Dict[str, Any], args: argparse.Namespace) -> pd.DataFrame:
    """Write the per-project layer-1 outputs (plan §5.2).

    Parameters
    ----------
    state : pd.DataFrame
        Full tidy state table for this scan.
    tickets_by_run : dict
        Run key -> ticket summaries.
    cfg : dict
        Host config.
    args : argparse.Namespace
        ``--dryrun`` blocks all writes.

    Returns
    -------
    pd.DataFrame
        All event rows emitted this scan (across projects).
    """
    print("Writing layer-1 per-project outputs ...")
    sys_tag = cfg["system"]
    all_events: List[pd.DataFrame] = []
    for project, proj_state in state.groupby("project", sort=True):
        out_dir = cfg["node_root"] / str(project) / "Documentation" / "ProcessingStatus"
        state_f = out_dir / f"state_{sys_tag}.parquet"
        events_f = out_dir / f"events_{sys_tag}.parquet"
        prev = pd.read_parquet(state_f) if state_f.is_file() else None
        events = diff_events(prev, proj_state.reset_index(drop=True), cfg)
        all_events.append(events)
        if args.dryrun:
            print(f"  [dryrun] {project}: {len(proj_state)} state rows, "
                  f"{len(events)} event(s) -> {out_dir}")
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        if not events.empty:
            if events_f.is_file():
                events = pd.concat([pd.read_parquet(events_f), events],
                                   ignore_index=True)
            events.to_parquet(events_f, index=False)
        proj_state.to_parquet(state_f, index=False)
        # +++++ human reports +++++
        if cfg["reports"]:
            write_json_report(proj_state, tickets_by_run, cfg,
                              out_dir / f"{project}_Status.json")
            write_xlsx_report(proj_state, out_dir / f"{project}_Status.xlsx")
    events_df = (pd.concat(all_events, ignore_index=True)
                 if all_events else pd.DataFrame())
    return events_df


# ==================================================================================
def write_layer2(state: pd.DataFrame,
                 tickets_by_run: Dict[str, List[Dict[str, Any]]],
                 cfg: Dict[str, Any], args: argparse.Namespace) -> None:
    """Write the disposable node/sensor aggregates (plan §5.3).

    Parameters
    ----------
    state : pd.DataFrame
        Full tidy state table for this scan.
    tickets_by_run : dict
        Run key -> ticket summaries.
    cfg : dict
        Host config.
    args : argparse.Namespace
        ``--dryrun`` blocks all writes.

    Returns
    -------
    None
    """
    out_dir = (cfg["node_root"] / "Documents" / "StorageReports"
               / "ProcessingStatus")
    sys_tag = cfg["system"]
    if args.dryrun:
        print(f"  [dryrun] layer-2 aggregates -> {out_dir}")
        return
    print(f"Writing layer-2 aggregates to {out_dir} ...")
    out_dir.mkdir(parents=True, exist_ok=True)
    state.to_parquet(out_dir / f"Node_Status_{sys_tag}.parquet", index=False)
    if cfg["reports"]:
        write_json_report(state, tickets_by_run, cfg, out_dir / "Node_Status.json")
        write_xlsx_report(state, out_dir / "Node_Status.xlsx")
        sensor_dir = out_dir / "Sensor"
        sensor_dir.mkdir(exist_ok=True)
        for sensor, sens_state in state.groupby("sensor", sort=True):
            write_json_report(sens_state, tickets_by_run, cfg,
                              sensor_dir / f"{sensor}_Status.json")
            write_xlsx_report(sens_state, sensor_dir / f"{sensor}_Status.xlsx")


# ==================================================================================
# ========== Report rendering ==========
# ==================================================================================
def write_json_report(state: pd.DataFrame,
                      tickets_by_run: Dict[str, List[Dict[str, Any]]],
                      cfg: Dict[str, Any], fpath: pathlib.Path) -> None:
    """Render a state slice as the §5.4 JSON report.

    Parameters
    ----------
    state : pd.DataFrame
        State rows to render (node-wide, per-project, or per-sensor
        slice - the record shape is identical).
    tickets_by_run : dict
        Run key -> ticket summaries.
    cfg : dict
        Host config.
    fpath : pathlib.Path
        Output file.

    Returns
    -------
    None
    """
    report: Dict[str, Any] = {"metadata": {
        "node": cfg["node"], "system": cfg["system"],
        "report_generated_on": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": cfg["hostname"], "script": os.path.basename(__file__),
        "version": __version__,
    }}
    run_keys = ["project", "site", "sensor", "run_date", "run"]
    for rk, sub in state.groupby(run_keys, sort=True):
        key = "_".join(str(k) for k in rk)
        first = sub.iloc[0]
        stages: Dict[str, Any] = {}
        for _, row in sub[(sub["payload"] == "") & (sub["stage"] != "ticket")].iterrows():
            stages[str(row["stage"]) or "_none"] = {
                "stage_status": row["stage_status"],
                "applicable": bool(row["applicable"]),
                "artefact_count": int(row["artefact_count"]),
                "newest_mtime": (None if pd.isna(row["newest_mtime"])
                                 else str(row["newest_mtime"])),
                "message": row["message"],
            }
        payloads: Dict[str, Any] = {}
        prows = sub[(sub["payload"] != "") & (sub["stage"] != "ticket")]
        for _, row in prows.iterrows():
            payloads.setdefault(str(row["payload"]), {})[str(row["stage"])] = {
                "stage_status": row["stage_status"],
                "artefact_count": int(row["artefact_count"]),
            }
        report[key] = {
            "project": rk[0], "site": rk[1], "sensor": rk[2],
            "date": rk[3], "run": rk[4],
            "completion": first["completion"],
            "run_status": first["run_status"],
            "triggers": {"Deviations": bool(first["deviations"]),
                         "Issues": bool(first["issues"]),
                         "RunFailed": bool(first["run_failed"])},
            "yaml_state": first["yaml_state"],
            "stages": stages,
            "payloads": payloads,
            "tickets": tickets_by_run.get(key, []),
        }
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)


# ==================================================================================
def write_xlsx_report(state: pd.DataFrame, fpath: pathlib.Path) -> None:
    """Render a state slice as a coloured run x stage xlsx matrix (+ csv).

    Parameters
    ----------
    state : pd.DataFrame
        State rows to render.
    fpath : pathlib.Path
        Output xlsx file; the same matrix is also written next to it as
        a plain ``.csv`` (text-editor/grep-friendly twin).

    Returns
    -------
    None
    """
    stage_fills = {
        "done":           "C6EFCE",  # green
        "missing":        "FFC7CE",  # red-pink
        "empty":          "FFEB9C",  # amber
        "failed_check":   "FF6B6B",  # red
        "not_applicable": "D9D9D9",  # grey
        "no_pipeline":    "F2F2F2",  # light grey
    }
    status_fills = {
        "ok": "C6EFCE", "ok_fixed": "A9D08E", "ok_deviation": "9BC2E6",
        "degraded": "FFE699", "under_investigation": "BDD7EE",
        "failed_partial": "F4B084", "failed_total": "000000",
    }
    completion_fills = {
        "complete": "C6EFCE",     # green
        "pending": "FFEB9C",      # amber
        "stalled": "F4B084",      # orange: no progress > stall_days
        "blocked": "FF6B6B",      # red
        "failed": "000000",       # black
        "no_pipeline": "F2F2F2",  # light grey
    }
    run_keys = ["project", "site", "sensor", "run_date", "run"]
    base = state[(state["payload"] == "") & (state["stage"] != "ticket")]
    stage_order: List[str] = []
    for s in base["stage"]:
        if s and s not in stage_order:
            stage_order.append(s)
    rows = []
    for rk, sub in base.groupby(run_keys, sort=True):
        row: Dict[str, Any] = dict(zip(run_keys, rk))
        row["completion"] = sub["completion"].iloc[0]
        row["run_status"] = sub["run_status"].iloc[0]
        for _, r in sub.iterrows():
            if r["stage"]:
                row[str(r["stage"])] = r["stage_status"]
        rows.append(row)
    cols = run_keys + ["completion", "run_status"] + stage_order
    matrix = pd.DataFrame(rows, columns=cols)
    # +++++ plain-text twin for quick text-editor/grep checks +++++
    matrix.to_csv(fpath.with_suffix(".csv"), index=False)
    with pd.ExcelWriter(fpath, engine="openpyxl") as writer:
        matrix.to_excel(writer, sheet_name="Status", index=False)
        ws = writer.sheets["Status"]
        ws.freeze_panes = "A2"
        for cell in ws[1]:
            cell.font = Font(bold=True)
        completion_col = cols.index("completion") + 1
        status_col = cols.index("run_status") + 1
        for r_i in range(2, ws.max_row + 1):
            ccell = ws.cell(row=r_i, column=completion_col)
            cfill = completion_fills.get(str(ccell.value))
            if cfill is not None:
                ccell.fill = PatternFill("solid", fgColor=cfill)
                if cfill == "000000":
                    ccell.font = Font(color="FFFFFF", bold=True)
            scell = ws.cell(row=r_i, column=status_col)
            sval = str(scell.value)
            fill = status_fills.get(sval)
            if fill is None and sval.endswith("_undocumented"):
                fill = "CC99FF"  # purple: documentation pending
            if fill is not None:
                scell.fill = PatternFill("solid", fgColor=fill)
                if fill == "000000":
                    scell.font = Font(color="FFFFFF", bold=True)
            for c_i in range(status_col + 1, len(cols) + 1):
                cell = ws.cell(row=r_i, column=c_i)
                if cell.value is None:
                    continue
                cval = str(cell.value)
                cfill = stage_fills.get(cval)
                if cfill is not None:
                    cell.fill = PatternFill("solid", fgColor=cfill)
        for col_cells in ws.columns:
            width = max(len(str(c.value)) for c in col_cells if c.value) + 2
            ws.column_dimensions[col_cells[0].column_letter].width = min(width, 32)


# ==================================================================================
def print_summary(state: pd.DataFrame, events: pd.DataFrame) -> None:
    """Print an end-of-scan summary.

    Parameters
    ----------
    state : pd.DataFrame
        Full state table.
    events : pd.DataFrame
        Events emitted this scan.

    Returns
    -------
    None
    """
    run_keys = ["project", "site", "sensor", "run_date", "run"]
    runs = state.drop_duplicates(subset=run_keys)
    print("\n========== PS00 scan summary ==========")
    print(f"Runs scanned: {len(runs)}  |  state rows: {len(state)}  |  "
          f"events this scan: {len(events)}")
    print("completion counts:")
    print(runs["completion"].value_counts().to_string())
    print("run_status counts:")
    print(runs["run_status"].value_counts().to_string())
    piped = state[(state["payload"] == "") & (state["stage"] != "ticket")
                  & (state["stage"] != "")]
    if not piped.empty:
        print("stage_status counts (whole-stage rows):")
        print(piped["stage_status"].value_counts().to_string())


# ==================================================================================
if __name__ == "__main__":
    # ========== chdir to git root (resolved at module top) ==========
    os.chdir(_git_root)

    # ========== Parse args ==========
    parser = argparse.ArgumentParser(
        description="PS00 processing-status collector (P9). Scans the node "
                    "Tier1 tree, evaluates each sensor's pipeline registry, "
                    "folds operator issue-YAML tickets, and writes the "
                    "layer-1/layer-2 tracking outputs.")
    parser.add_argument("--dryrun", action="store_true",
                        help="Scan + report to stdout only; write nothing.")
    parser.add_argument("--projects", type=str, nargs="+", default=None,
                        help="Only scan these project folder names.")
    parser.add_argument("--dataroot", type=str, default=None,
                        help="Folder containing the node dir "
                             "(default: the git repo root).")
    parser.add_argument("--node", type=str, default=None,
                        help="Node folder name (default: the single node "
                             "in NodeSummary.yaml).")
    parser.add_argument("--system", type=str, default=None,
                        help="System tag for output file names, WS or NAS "
                             "(default WS).")
    parser.add_argument("--no-gitsync", action="store_true",
                        help="Skip the automatic commit/push of changed "
                             "run metadata (*_Issues.yaml, RunOverview.csv).")
    cf.check_environment(_git_root)
    main(parser.parse_args())
