"""Adopt an existing APPN-format folder tree into ProjectBuilder management.

``ProjectBuilder.py`` builds an APPN folder structure *from* metadata
(NodeSummary.yaml -> ProjectsSummary.csv -> ProjectSummary.yaml ->
FieldLog.csv). This script is the inverse, for trees that already exist in
the APPN on-disk format but were assembled by hand: it audits the tree
against the naming convention (reporting non-compliance), and with
``--apply`` reconstructs the metadata files ProjectBuilder consumes so a
subsequent ``python ProjectBuilder.py --historical --enable-sensors`` run
adopts the tree as its own and creates every derived artefact
(RunOverview.csv, FieldNotes.txt, issue YAMLs, Documentation READMEs,
tier folders, checksums).

Design rule: this script reconstructs ProjectBuilder *inputs*, never its
outputs -- there is exactly one implementation of every writer. Existing
metadata files are merged append-only and never overwritten; tree/metadata
disagreements become report findings.

See ``DM01_ADOPTER_PLAN.md`` (this folder) for the full design.

Command-line Arguments
----------------------
--path : str
    Root of the tree to adopt (default: the git repo root). Anything else
    raises a UserWarning -- off the supported fork-the-generic-repo workflow.
--apply : bool
    Write the reconstructed metadata (default: audit only). Prints the
    planned writes and asks y/N before writing. Refused while any
    fail-class finding exists.
--projectsYAML : str
    The node YAML file with the sensors (default ./NodeSummary.yaml, same
    flag name as ProjectBuilder).
"""

# ==============================================================================

__title__ = "DM01 Structure Adopter"
__author__ = "Arden Burrell"
__version__ = "v1.0(01.09.2026)"
__email__ = "arden.burrell@sydney.edu.au"

# ==============================================================================
# ========== Import core packages ==========
import os
import re
import sys
import argparse
import pathlib
from typing import Any, Dict, List, Optional, Tuple

# ========== Import other packages ==========
import git
from git import exc as git_exc
import numpy as np
import pandas as pd
import yaml
from tqdm import tqdm
import warnings as warn

# ========== Resolve git root (must happen before importing Code.functions.*) ==========
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
import Code.functions.core_functions as cf  # pyright: ignore[reportMissingImports]
import ProjectBuilder as pb  # pyright: ignore[reportMissingImports]


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
        One row per finding (severity, code, path, message). Empty when the
        tree is fully compliant.
    """
    # ========== Step 1: resolve the root and load NodeSummary.yaml ==========
    root = pathlib.Path(args.path).resolve()
    if root != pathlib.Path(_git_root).resolve():
        warn.warn(
            f"--path {root} is not the git repo root ({_git_root}). The "
            f"supported workflow puts your fork of the generic repo at the "
            f"root of the data tree; ProjectBuilder hand-off advice does "
            f"not apply here.", UserWarning)
    print(f"Loading node summary {args.projectsYAML} ...")
    nodeinfo = load_node_summary(pathlib.Path(args.projectsYAML))

    # ========== Step 2: crawl + audit ==========
    print(f"Auditing tree at {root} ...")
    findings, models = audit_store(root, nodeinfo)

    # ========== Step 3: write per-node compliance reports ==========
    for node_name, model in models.items():
        report_fn = write_report(root, node_name, findings, model)
        print(f"Compliance report written: {report_fn}")

    # ========== Step 4: print the findings summary ==========
    df_findings = findings_frame(findings)
    print_summary(df_findings)

    # ========== Step 5: optionally apply ==========
    if args.apply:
        apply_adoption(root, nodeinfo, models, df_findings)

    return df_findings


# ==================================================================================
def load_node_summary(path: pathlib.Path) -> Dict[str, Any]:
    """Load and sanity-check NodeSummary.yaml.

    Parameters
    ----------
    path : pathlib.Path
        Path to the node summary YAML.

    Returns
    -------
    dict
        Parsed node summary with a ``nodes`` list.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If the file has no usable ``nodes`` list.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"Node summary not found: {path}. Edit NodeSummary.yaml so the "
            f"node name matches your existing node folder before adopting.")
    with open(path, "r") as fh:
        nodeinfo = yaml.safe_load(fh)
    nodes = nodeinfo.get("nodes") if isinstance(nodeinfo, dict) else None
    if not isinstance(nodes, list) or not nodes:
        raise ValueError(f"{path} contains no 'nodes' list.")
    for node in nodes:
        if not isinstance(node, dict) or not node.get("name"):
            raise ValueError(f"{path}: every node needs a 'name' key.")
        if not node.get("SensorPlatforms"):
            raise ValueError(
                f"{path}: node {node.get('name')} has no SensorPlatforms "
                f"list -- required to validate sensor folders.")
    return nodeinfo


# ==================================================================================
def audit_store(root: pathlib.Path,
                nodeinfo: Dict[str, Any]) -> Tuple[List[Dict[str, Any]],
                                                   Dict[str, Dict[str, Any]]]:
    """Audit the whole tree and build the inferred per-node models.

    Parameters
    ----------
    root : pathlib.Path
        Root of the tree (normally the repo root).
    nodeinfo : dict
        Parsed NodeSummary.yaml.

    Returns
    -------
    findings : list of dict
        All findings (severity, code, path, message).
    models : dict
        ``{node_name: model}`` for every node folder found; see
        :func:`crawl_node` for the model shape.
    """
    findings: List[Dict[str, Any]] = []
    node_names = [node["name"] for node in nodeinfo["nodes"]]

    audit_root_level(root, node_names, findings)

    models: Dict[str, Dict[str, Any]] = {}
    for node in nodeinfo["nodes"]:
        node_dir = root / node["name"]
        if not node_dir.is_dir():
            _add(findings, "info", "node_absent", node_dir,
                 f"Node folder {node['name']} not found under {root} -- "
                 f"nothing to adopt for this node.")
            continue
        models[node["name"]] = crawl_node(node_dir, node, findings)
    return findings, models


# ==================================================================================
def audit_root_level(root: pathlib.Path, node_names: List[str],
                     findings: List[Dict[str, Any]]) -> None:
    """Grade the folders sitting at the tree root.

    Parameters
    ----------
    root : pathlib.Path
        Root of the tree.
    node_names : list of str
        Node names declared in NodeSummary.yaml.
    findings : list of dict
        Findings list (appended to in place).

    Returns
    -------
    None
    """
    repo_dirs = {"Code", "reference", ".git", ".github", "__pycache__",
                 ".pytest_cache", ".vscode"}
    project_regex = re.compile(r"^\d{4}_.+$")
    for child in sorted(root.iterdir()):
        if not child.is_dir() or _ignored(child.name):
            continue
        if child.name in repo_dirs or child.name in node_names:
            continue
        if project_regex.match(child.name):
            _add(findings, "fail", "project_at_root", child,
                 f"Project-shaped folder {child.name} sits at the root with "
                 f"no node folder. Move it under ./{node_names[0]}/ (or the "
                 f"correct node).")
        else:
            _add(findings, "warn", "unrecognised_root_folder", child,
                 f"Folder {child.name} at the root matches no node in "
                 f"NodeSummary.yaml ({node_names}) and is ignored. If it is "
                 f"a node, add it to NodeSummary.yaml; if it is a "
                 f"misspelled node folder, rename it.")


# ==================================================================================
def crawl_node(node_dir: pathlib.Path, node: Dict[str, Any],
               findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Walk one node folder, grading every level and building the model.

    Parameters
    ----------
    node_dir : pathlib.Path
        The node folder.
    node : dict
        Node entry from NodeSummary.yaml (name, SensorPlatforms).
    findings : list of dict
        Findings list (appended to in place).

    Returns
    -------
    dict
        ``{project: {"sites": {site_folder: {"site_meta": dict|None,
        "sensors": {sensor: {date: {"runs": [int], "run_folders": [str]}}}}}}}``
    """
    project_regex = re.compile(r"^\d{4}_.+$")
    allowed = {"Documents", "Documentation", "Code", "code"}
    model: Dict[str, Any] = {}
    projects = [c for c in sorted(node_dir.iterdir()) if c.is_dir()
                and not _ignored(c.name)]
    for child in tqdm(projects, desc=f"Projects in {node_dir.name}"):
        if child.name in allowed:
            continue
        if not project_regex.match(child.name):
            _add(findings, "fail", "bad_project_name", child,
                 f"Folder {child.name} does not match the project pattern "
                 f"YYYY_ProjectDesc... Rename it (see "
                 f"FolderStructureInfo.txt).")
            continue
        model[child.name] = {"sites": crawl_project(child, node, findings)}
    return model


# ==================================================================================
def crawl_project(project_dir: pathlib.Path, node: Dict[str, Any],
                  findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Walk one project folder: site level.

    Parameters
    ----------
    project_dir : pathlib.Path
        The project folder.
    node : dict
        Node entry from NodeSummary.yaml.
    findings : list of dict
        Findings list (appended to in place).

    Returns
    -------
    dict
        ``{site_folder: {"site_meta": dict|None, "sensors": {...}}}``.
    """
    allowed = {"Documentation", "Documents", "Code", "code"}
    sites: Dict[str, Any] = {}
    for child in sorted(project_dir.iterdir()):
        if not child.is_dir() or _ignored(child.name):
            continue
        if child.name in allowed:
            continue
        site_meta = invert_site_folder(child.name)
        if site_meta is None:
            _add(findings, "fail", "bad_site_name", child,
                 f"Folder {child.name} does not match the site pattern "
                 f"{{YYYY}}{{SiteName}}[_F|_C] (year prefix required). "
                 f"Rename it.")
            continue
        sites[child.name] = {
            "site_meta": site_meta,
            "sensors": crawl_site(child, node, findings),
        }
    return sites


# ==================================================================================
def crawl_site(site_dir: pathlib.Path, node: Dict[str, Any],
               findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Walk one site folder: sensor level.

    Parameters
    ----------
    site_dir : pathlib.Path
        The site folder.
    node : dict
        Node entry from NodeSummary.yaml (uses SensorPlatforms).
    findings : list of dict
        Findings list (appended to in place).

    Returns
    -------
    dict
        ``{sensor: {date: {"runs": [int], "run_folders": [str]}}}``.
    """
    allowed = {"Documentation", "Documents", "Code", "code"}
    sensors: Dict[str, Any] = {}
    for child in sorted(site_dir.iterdir()):
        if not child.is_dir() or _ignored(child.name):
            continue
        if child.name in allowed:
            continue
        if child.name not in node["SensorPlatforms"]:
            _add(findings, "fail", "unknown_sensor", child,
                 f"Sensor folder {child.name} is not in NodeSummary.yaml "
                 f"SensorPlatforms for node {node['name']} "
                 f"({node['SensorPlatforms']}). Add it there if genuine, "
                 f"or rename the folder.")
            continue
        sensors[child.name] = crawl_sensor(child, findings)
    return sensors


# ==================================================================================
def crawl_sensor(sensor_dir: pathlib.Path,
                 findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Walk one sensor folder: date level (+ canonical parser validation).

    Parameters
    ----------
    sensor_dir : pathlib.Path
        The sensor folder.
    findings : list of dict
        Findings list (appended to in place).

    Returns
    -------
    dict
        ``{date_str: {"runs": [int], "run_folders": [str]}}``.
    """
    date_regex = re.compile(r"^\d{8}$")
    doc_dirs = {"Documentation", "Documents", "Code", "code"}
    dates: Dict[str, Any] = {}
    for child in sorted(sensor_dir.iterdir()):
        if not child.is_dir() or _ignored(child.name):
            continue
        if child.name in doc_dirs:
            # Not adoptable, but not a naming failure -- spec puts these at
            # site/project level, so flag as misplaced rather than blocking.
            _add(findings, "warn", "misplaced_folder", child,
                 f"Folder {child.name} at date level -- the spec puts "
                 f"Documentation/Code folders at the site or project "
                 f"level. Move it.")
            continue
        if (not date_regex.match(child.name)
                or pd.isna(pd.to_datetime(child.name, format="%Y%m%d",
                                          errors="coerce"))):
            _add(findings, "fail", "bad_date_name", child,
                 f"Folder {child.name} is not a valid YYYYMMDD date folder. "
                 f"Rename it.")
            continue
        # +++++ Canonical validation via the shared parser (R8) +++++
        parsed = cf.parse_APPN_dataset_path(child, path_level="date")
        if not parsed["valid"]:
            for err in parsed["errors"]:
                _add(findings, "fail", "parser_error", child, err)
        dates[child.name] = crawl_date(child, findings)
    return dates


# ==================================================================================
def crawl_date(date_dir: pathlib.Path,
               findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Walk one date folder: run level, run-number gaps, misplaced items.

    Parameters
    ----------
    date_dir : pathlib.Path
        The YYYYMMDD folder.
    findings : list of dict
        Findings list (appended to in place).

    Returns
    -------
    dict
        ``{"runs": [int], "run_folders": [str]}`` (run numbers sorted).
    """
    run_regex = re.compile(r"^run_(\d{2,})$")
    loose_run_regex = re.compile(r"^[Rr]un_?(\d+)$")
    allowed_files = {"FieldNotes.txt", "RunOverview.csv"}
    runs: List[int] = []
    run_folders: List[str] = []
    for child in sorted(date_dir.iterdir()):
        if _ignored(child.name):
            continue
        if child.is_file():
            if (child.name not in allowed_files
                    and not child.name.endswith("_Issues.yaml")):
                _add(findings, "warn", "misplaced_file", child,
                     f"Unexpected file {child.name} at date level (spec "
                     f"allows FieldNotes.txt, RunOverview.csv and "
                     f"*_Issues.yaml).")
            continue
        match = run_regex.match(child.name)
        if match:
            runs.append(int(match.group(1)))
            run_folders.append(child.name)
            crawl_run(child, findings)
        elif loose_run_regex.match(child.name):
            _add(findings, "fail", "bad_run_name", child,
                 f"Run folder {child.name} must be zero-padded run_XX "
                 f"(e.g. run_01). Rename it.")
        else:
            _add(findings, "warn", "misplaced_folder", child,
                 f"Unexpected folder {child.name} at date level (only "
                 f"run_XX folders belong here).")
    runs.sort()
    if runs and set(runs) != set(range(max(runs) + 1)):
        _add(findings, "warn", "run_gap", date_dir,
             f"Non-contiguous run numbers {run_folders} -- ProjectBuilder "
             f"assumes run_00..run_{max(runs):02d}. FieldLog Runs will be "
             f"inferred as {max(runs) + 1} so the gap is back-filled with "
             f"empty skeletons; renumber manually if that is wrong.")
    return {"runs": runs, "run_folders": run_folders}


# ==================================================================================
def crawl_run(run_dir: pathlib.Path,
              findings: List[Dict[str, Any]]) -> None:
    """Grade one run folder: tier presence and misplaced items.

    Parameters
    ----------
    run_dir : pathlib.Path
        The run_XX folder.
    findings : list of dict
        Findings list (appended to in place).

    Returns
    -------
    None
    """
    tiers = {"T0_raw", "T1_proc", "T2_traits"}
    present = set()
    for child in sorted(run_dir.iterdir()):
        if _ignored(child.name):
            continue
        if child.is_file():
            _add(findings, "warn", "misplaced_file", child,
                 f"Loose file {child.name} at run level (files belong "
                 f"inside a tier folder).")
        elif child.name in tiers:
            present.add(child.name)
        else:
            _add(findings, "warn", "misplaced_folder", child,
                 f"Unexpected folder {child.name} at run level (expected "
                 f"{sorted(tiers)}).")
    missing = tiers - present
    if missing:
        _add(findings, "warn", "missing_tiers", run_dir,
             f"Missing tier folder(s) {sorted(missing)} -- ProjectBuilder "
             f"will create them on the adoption pass.")


# ==================================================================================
def invert_site_folder(site_folder: str) -> Optional[Dict[str, Any]]:
    """Invert a site folder name into ProjectSummary.yaml site fields.

    Parameters
    ----------
    site_folder : str
        Folder name of the form ``{YYYY}{SiteName}[_F|_C]``.

    Returns
    -------
    dict or None
        ``{"name", "year", "ControlledEnvironment"}`` when the name parses
        and round-trips through ``pb._sitenamemaker``; None otherwise.
    """
    match = re.match(r"^(\d{4})(.+)$", site_folder)
    if match is None:
        return None
    year, rest = int(match.group(1)), match.group(2)
    controlled: Optional[bool] = None
    name = rest
    if rest.endswith("_C"):
        controlled, name = True, rest[:-2]
    elif rest.endswith("_F"):
        controlled, name = False, rest[:-2]
    if not name:
        return None
    site = {"name": name, "year": year, "ControlledEnvironment": controlled}
    # Round-trip through the one canonical name builder (R8).
    if pb._sitenamemaker(site) != site_folder:
        return None
    return site


# ==================================================================================
def build_field_rows(model_sites: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Derive FieldLog rows from a project's inferred site model.

    Parameters
    ----------
    model_sites : dict
        The ``sites`` mapping of one project model entry.

    Returns
    -------
    list of dict
        One FieldLog row dict per (site, sensor, date) with run folders.
        ``Runs`` is max run number + 1 so gaps get back-filled;
        ``CheckSum`` is left NaN for ProjectBuilder's Rowchecker to fill.
    """
    rows: List[Dict[str, Any]] = []
    for site_folder, site_entry in model_sites.items():
        site_meta = site_entry["site_meta"]
        for sensor, dates in site_entry["sensors"].items():
            for date_str, date_entry in dates.items():
                if not date_entry["runs"]:
                    continue  # no run folders -> nothing to log
                rows.append({
                    "Year": int(date_str[:4]),
                    "Month": int(date_str[4:6]),
                    "Day": int(date_str[6:8]),
                    "Sensor": sensor,
                    "Technician": "Unknown",
                    "Runs": max(date_entry["runs"]) + 1,
                    "Site": site_meta["name"],
                    "MakeNotesFile": True,
                    "MakeTableFile": True,
                    "CheckSum": np.nan,
                })
    rows.sort(key=lambda r: (r["Site"], r["Sensor"],
                             r["Year"], r["Month"], r["Day"]))
    return rows


# ==================================================================================
def apply_adoption(root: pathlib.Path, nodeinfo: Dict[str, Any],
                   models: Dict[str, Dict[str, Any]],
                   df_findings: pd.DataFrame) -> None:
    """Plan, confirm and execute the metadata writes.

    Parameters
    ----------
    root : pathlib.Path
        Root of the tree.
    nodeinfo : dict
        Parsed NodeSummary.yaml.
    models : dict
        Per-node inferred models from :func:`audit_store`.
    df_findings : pd.DataFrame
        Findings frame; any ``fail`` row blocks the apply.

    Returns
    -------
    None
    """
    # ========== Refuse while fails exist ==========
    if not df_findings.empty and (df_findings["severity"] == "fail").any():
        n_fail = int((df_findings["severity"] == "fail").sum())
        print(f"\n--apply REFUSED: {n_fail} fail-class finding(s) present. "
              f"Fix them (see the compliance report) and re-run.")
        return

    # ========== Dry-run listing ==========
    plans = []
    for node in nodeinfo["nodes"]:
        if node["name"] in models:
            plans.extend(plan_node_writes(root, node, models[node["name"]]))
    actionable = [p for p in plans if p["action"] != "unchanged"]
    if not actionable:
        print("\nNothing to write -- metadata already matches the tree.")
        return
    print("\nPlanned metadata writes:")
    for plan in actionable:
        print(f"  [{plan['action']:>6}] {plan['path']}  ({plan['note']})")

    # ========== Confirm ==========
    answer = input("\nProceed with these writes? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted -- nothing written.")
        return

    # ========== Execute ==========
    for plan in actionable:
        plan["write"]()
        print(f"Wrote {plan['path']}")
    print("\nAdoption metadata written. Next step:\n"
          "    python ProjectBuilder.py --historical --enable-sensors\n"
          "(add --no-git to skip commits) then fill in the TODO "
          "placeholders listed in the compliance report.")


# ==================================================================================
def plan_node_writes(root: pathlib.Path, node: Dict[str, Any],
                     model: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the write plan (action + deferred writer) for one node.

    Parameters
    ----------
    root : pathlib.Path
        Root of the tree.
    node : dict
        Node entry from NodeSummary.yaml.
    model : dict
        Inferred model for the node.

    Returns
    -------
    list of dict
        ``{"action": create|update|unchanged, "path": str, "note": str,
        "write": callable}`` per target file.
    """
    plans: List[Dict[str, Any]] = []
    node_dir = root / node["name"]
    plans.append(plan_projects_summary(node_dir, node, model))
    for project, entry in model.items():
        project_dir = node_dir / project
        sites = [s["site_meta"] for s in entry["sites"].values()]
        plans.append(plan_project_yaml(project_dir, project, sites))
        plans.append(plan_field_log(project_dir,
                                    build_field_rows(entry["sites"])))
    return plans


# ==================================================================================
def plan_projects_summary(node_dir: pathlib.Path, node: Dict[str, Any],
                          model: Dict[str, Any]) -> Dict[str, Any]:
    """Plan the append-only merge of ``{Node}_ProjectsSummary.csv``.

    Parameters
    ----------
    node_dir : pathlib.Path
        The node folder.
    node : dict
        Node entry from NodeSummary.yaml.
    model : dict
        Inferred model for the node.

    Returns
    -------
    dict
        Write-plan entry (see :func:`plan_node_writes`).
    """
    fname = node_dir / f"{node['name']}_ProjectsSummary.csv"
    sensors = list(node["SensorPlatforms"])
    if fname.is_file():
        df = pd.read_csv(fname, header=0, index_col=0)
        action = "update"
    else:
        df = pd.DataFrame(columns=sensors)
        df.index.name = "Project"
        action = "create"
    changed = not fname.is_file()
    for col in sensors:
        if col not in df.columns:
            df[col] = False
            changed = True
    for project, entry in model.items():
        if project not in df.index:
            df.loc[project, :] = False
            changed = True
        seen = {sensor for site in entry["sites"].values()
                for sensor in site["sensors"]}
        for sensor in seen:
            if df.loc[project, sensor] != True:  # noqa: E712 -- only ever flips False->True
                df.loc[project, sensor] = True
                changed = True
    df.index.name = "Project"

    def _write() -> None:
        df.sort_index().to_csv(fname)

    n_projects = len(model)
    return {"action": action if changed else "unchanged",
            "path": str(fname),
            "note": f"{n_projects} project(s), sensors flagged from tree",
            "write": _write}


# ==================================================================================
def plan_project_yaml(project_dir: pathlib.Path, project: str,
                      sites: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Plan the create/append of a project's ``ProjectSummary.yaml``.

    Parameters
    ----------
    project_dir : pathlib.Path
        The project folder.
    project : str
        Project name.
    sites : list of dict
        Inferred site metadata (name, year, ControlledEnvironment).

    Returns
    -------
    dict
        Write-plan entry (see :func:`plan_node_writes`).
    """
    fname = project_dir / "ProjectSummary.yaml"
    template_site = pb._defaultProjectYAML(project)["project"]["sites"][0]

    def _full_site(meta: Dict[str, Any]) -> Dict[str, Any]:
        site = dict(template_site)
        site["sensors"] = []  # fresh list per site or yaml.dump emits aliases
        site.update(meta)
        return site

    if fname.is_file():
        with open(fname, "r") as fh:
            data = yaml.safe_load(fh)
        existing = data.get("project", {}).get("sites", []) or []
        known = {(s.get("name"), s.get("year"))
                 for s in existing if isinstance(s, dict)}
        new = [_full_site(m) for m in sites
               if (m["name"], m["year"]) not in known]
        # Drop the untouched template placeholder once real sites exist.
        kept = [s for s in existing
                if not (isinstance(s, dict) and s.get("name") == ""
                        and s.get("year") == -9999)] if new else existing
        changed = bool(new) or (len(kept) != len(existing))
        data.setdefault("project", {})["sites"] = kept + new
        action, note = "update", f"append {len(new)} site(s)"
    else:
        data = pb._defaultProjectYAML(project)
        data["project"]["sites"] = [_full_site(m) for m in sites]
        changed = True
        action, note = "create", f"{len(sites)} site(s) from tree"

    def _write() -> None:
        with open(fname, "w") as fh:
            yaml.dump(data, fh, sort_keys=False)

    return {"action": action if changed else "unchanged",
            "path": str(fname), "note": note, "write": _write}


# ==================================================================================
def plan_field_log(project_dir: pathlib.Path,
                   rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Plan the append-only merge of a project's ``FieldLog.csv``.

    Parameters
    ----------
    project_dir : pathlib.Path
        The project folder.
    rows : list of dict
        Inferred FieldLog rows from :func:`build_field_rows`.

    Returns
    -------
    dict
        Write-plan entry (see :func:`plan_node_writes`).

    Notes
    -----
    Existing rows are matched on (Year, Month, Day, Sensor, Site) and never
    modified; a Runs disagreement is printed rather than corrected --
    curation stays with the operator.
    """
    colnames = ["Year", "Month", "Day", "Sensor", "Technician", "Runs",
                "Site", "MakeNotesFile", "MakeTableFile", "CheckSum"]
    fname = project_dir / "FieldLog.csv"
    if fname.is_file():
        df = pd.read_csv(fname)
        for col in colnames:
            if col not in df.columns:
                df[col] = np.nan
        keys = {(int(r.Year), int(r.Month), int(r.Day), str(r.Sensor),
                 str(r.Site))
                for r in df.itertuples()
                if not pd.isna(r.Year)}
        action = "update"
    else:
        df = pd.DataFrame(columns=colnames)
        keys = set()
        action = "create"
    new_rows = [r for r in rows
                if (r["Year"], r["Month"], r["Day"], r["Sensor"], r["Site"])
                not in keys]
    changed = bool(new_rows) or not fname.is_file()
    if new_rows:
        df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
    df = df[colnames + [c for c in df.columns if c not in colnames]]

    def _write() -> None:
        df.to_csv(fname, index=False)

    return {"action": action if changed else "unchanged",
            "path": str(fname),
            "note": f"append {len(new_rows)} field-day row(s)",
            "write": _write}


# ==================================================================================
def write_report(root: pathlib.Path, node_name: str,
                 findings: List[Dict[str, Any]],
                 model: Dict[str, Any]) -> pathlib.Path:
    """Render the per-node compliance report markdown.

    Parameters
    ----------
    root : pathlib.Path
        Root of the tree.
    node_name : str
        Node folder name.
    findings : list of dict
        All findings (filtered to this node + root-level here).
    model : dict
        Inferred model for the node.

    Returns
    -------
    pathlib.Path
        Path of the written report.
    """
    node_dir = root / node_name
    fname = node_dir / "DM01_AdoptionReport.md"
    # This node's findings plus root-level strays (which affect every node).
    node_findings = [f for f in findings
                     if f["path"].startswith(str(node_dir) + os.sep)
                     or f["path"] == str(node_dir)
                     or pathlib.Path(f["path"]).parent == root]
    lines = [f"# DM01 adoption report -- {node_name}", "",
             f"Generated: {pd.Timestamp.now():%Y-%m-%d %H:%M} by "
             f"{__title__} {__version__}", "",
             "Overwritten on every DM01 run; git history keeps priors.", ""]

    # ========== Findings ==========
    df = findings_frame(node_findings)
    lines.append("## Findings")
    lines.append("")
    if df.empty:
        lines.append("No findings -- tree is compliant.")
        lines.append("")
    else:
        for severity in ("fail", "warn", "info"):
            sub = df[df["severity"] == severity]
            if sub.empty:
                continue
            lines.append(f"### {severity} ({len(sub)})")
            lines.append("")
            show = sub[["code", "path", "message"]].copy()
            show["path"] = show["path"].map(
                lambda p: os.path.relpath(p, root))
            lines.append(cf.markdown_table(show))
            lines.append("")

    # ========== Inferred model summary ==========
    lines.append("## Inferred structure")
    lines.append("")
    summary_rows = []
    for project, entry in sorted(model.items()):
        rows = build_field_rows(entry["sites"])
        sensors = sorted({s for site in entry["sites"].values()
                          for s in site["sensors"]})
        summary_rows.append({"project": project,
                             "sites": len(entry["sites"]),
                             "sensors": ", ".join(sensors),
                             "field_day_rows": len(rows)})
    if summary_rows:
        lines.append(cf.markdown_table(pd.DataFrame(summary_rows)))
    else:
        lines.append("No adoptable projects found.")
    lines.append("")

    # ========== TODO checklist ==========
    lines.append("## TODO after `--apply` + ProjectBuilder pass")
    lines.append("")
    for project in sorted(model):
        lines.append(f"- [ ] `{project}/ProjectSummary.yaml`: fill "
                     f"FullName, description, dates, funding, researchers, "
                     f"and per-site season/SubLocation/lat/long/description.")
        lines.append(f"- [ ] `{project}/FieldLog.csv`: replace every "
                     f"`Technician = Unknown` placeholder with the real "
                     f"operator.")
    lines.append("- [ ] Run `python ProjectBuilder.py --historical "
                 "--enable-sensors` to create derived files and checksums.")
    lines.append("")

    with open(fname, "w") as fh:
        fh.write("\n".join(lines))
    return fname


# ==================================================================================
def findings_frame(findings: List[Dict[str, Any]]) -> pd.DataFrame:
    """Convert the findings list into a DataFrame.

    Parameters
    ----------
    findings : list of dict
        Findings (severity, code, path, message).

    Returns
    -------
    pd.DataFrame
        Findings frame (may be empty) with those four columns.
    """
    if not findings:
        return pd.DataFrame(columns=["severity", "code", "path", "message"])
    return pd.DataFrame(findings)[["severity", "code", "path", "message"]]


# ==================================================================================
def print_summary(df_findings: pd.DataFrame) -> None:
    """Print the end-of-run findings summary table.

    Parameters
    ----------
    df_findings : pd.DataFrame
        Findings frame from :func:`findings_frame`.

    Returns
    -------
    None
    """
    print("\n========== DM01 audit summary ==========")
    if df_findings.empty:
        print("Tree is fully compliant. No findings.")
        return
    counts = (df_findings.groupby(["severity", "code"]).size()
              .rename("count").reset_index())
    print(counts.to_string(index=False))
    n_fail = int((df_findings["severity"] == "fail").sum())
    if n_fail:
        print(f"\n{n_fail} fail-class finding(s) -- fix these before "
              f"--apply will run.")


# ==================================================================================
def _add(findings: List[Dict[str, Any]], severity: str, code: str,
         path: pathlib.Path, message: str) -> None:
    """Append one finding.

    Parameters
    ----------
    findings : list of dict
        Findings list (appended to in place).
    severity : {'fail', 'warn', 'info'}
        Finding severity class.
    code : str
        Machine-readable finding code.
    path : pathlib.Path
        Offending path.
    message : str
        Human-readable explanation with the fix.

    Returns
    -------
    None
    """
    findings.append({"severity": severity, "code": code,
                     "path": str(path), "message": message})


# ==================================================================================
def _ignored(name: str) -> bool:
    """Return True for filesystem noise that no level should grade.

    Parameters
    ----------
    name : str
        File or folder name.

    Returns
    -------
    bool
        True when the entry is hidden or a known OS/NAS/sync artefact.
    """
    return (name.startswith(".")
            or name in {"__pycache__", "sync.ffs_db", "Thumbs.db",
                        "desktop.ini", "@eaDir", "#recycle",
                        "$RECYCLE.BIN", "System Volume Information"})


# ==================================================================================
if __name__ == "__main__":
    # ========== chdir to git root (resolved at module top) ==========
    os.chdir(_git_root)

    # ========== Parse args ==========
    parser = argparse.ArgumentParser(
        description=("Audit an existing APPN-format tree and reconstruct "
                     "the ProjectBuilder metadata files so the tree can be "
                     "adopted. Audit-only by default; --apply writes."))
    parser.add_argument("--path", type=str, default=".",
                        help="Root of the tree to adopt (default: repo "
                             "root). Non-root paths raise a UserWarning.")
    parser.add_argument("--apply", action="store_true",
                        help="Write the reconstructed metadata (dry-run "
                             "listing + y/N confirm; refused while any "
                             "fail-class finding exists).")
    parser.add_argument("--projectsYAML", type=str,
                        default="./NodeSummary.yaml",
                        help="The node yaml file with the sensors.")
    cli_args = parser.parse_args()

    result = main(cli_args)
    if not result.empty and (result["severity"] == "fail").any():
        sys.exit(1)
