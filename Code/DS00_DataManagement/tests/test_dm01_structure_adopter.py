"""Tests for DM01_StructureAdopter (see DM01_ADOPTER_PLAN.md §9).

Synthetic ``tmp_path`` trees only -- machine-independent, same pattern as
the parse_APPN_dataset_path tests.

Run with:
    pytest Code/DS00_DataManagement/tests/test_dm01_structure_adopter.py -v
"""

import importlib.util
import pathlib
import sys

import numpy as np
import pandas as pd
import pytest
import yaml

# ---------------------------------------------------------------------------
# Ensure repo root is importable (DM01 imports Code.functions.* + ProjectBuilder)
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ==================================================================================
@pytest.fixture(scope="module")
def dm01():
    """Import DM01_StructureAdopter by file path (script, not a package).

    Returns
    -------
    module
        The loaded DM01_StructureAdopter module.
    """
    path = (_REPO_ROOT / "Code" / "DS00_DataManagement"
            / "DM01_StructureAdopter.py")
    spec = importlib.util.spec_from_file_location("dm01", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ==================================================================================
@pytest.fixture()
def nodeinfo():
    """Minimal NodeSummary structure for a synthetic node."""
    return {"nodes": [{"name": "TEST_Node",
                       "SensorPlatforms": ["GOBI", "CALVIS", "M3M"]}]}


# ==================================================================================
def _make_run(date_dir: pathlib.Path, run: str,
              tiers=("T0_raw", "T1_proc", "T2_traits")) -> None:
    for tier in tiers:
        (date_dir / run / tier).mkdir(parents=True, exist_ok=True)


def _make_tree(root: pathlib.Path) -> pathlib.Path:
    """Build a fully compliant synthetic tree; returns the node dir."""
    node = root / "TEST_Node"
    date_a = node / "2025_Wheat" / "2025Merinda_F" / "GOBI" / "20250301"
    _make_run(date_a, "run_00")
    _make_run(date_a, "run_01")
    date_b = node / "2025_Wheat" / "2025Merinda_F" / "CALVIS" / "20250302"
    _make_run(date_b, "run_00")
    date_c = node / "2026_Barley" / "2026Glasshouse_C" / "M3M" / "20260110"
    _make_run(date_c, "run_00")
    return node


# ==================================================================================
# ========== Site-name inversion ==========
# ==================================================================================
@pytest.mark.parametrize("folder,name,year,ce", [
    ("2025Merinda_F", "Merinda", 2025, False),
    ("2026Glasshouse_C", "Glasshouse", 2026, True),
    ("2025IAWatson", "IAWatson", 2025, None),
])
def test_invert_site_roundtrip(dm01, folder, name, year, ce):
    site = dm01.invert_site_folder(folder)
    assert site == {"name": name, "year": year, "ControlledEnvironment": ce}


@pytest.mark.parametrize("folder", ["Merinda", "25Merinda", "2025", "2025_F"])
def test_invert_site_rejects(dm01, folder):
    assert dm01.invert_site_folder(folder) is None


# ==================================================================================
# ========== Audit: compliant tree ==========
# ==================================================================================
def test_compliant_tree_no_fails(dm01, nodeinfo, tmp_path):
    _make_tree(tmp_path)
    findings, models = dm01.audit_store(tmp_path, nodeinfo)
    df = dm01.findings_frame(findings)
    assert df.empty or not (df["severity"] == "fail").any()
    model = models["TEST_Node"]
    assert set(model) == {"2025_Wheat", "2026_Barley"}
    wheat = model["2025_Wheat"]["sites"]["2025Merinda_F"]
    assert wheat["site_meta"]["name"] == "Merinda"
    assert wheat["sensors"]["GOBI"]["20250301"]["runs"] == [0, 1]
    assert wheat["sensors"]["CALVIS"]["20250302"]["runs"] == [0]


def test_field_rows(dm01, nodeinfo, tmp_path):
    _make_tree(tmp_path)
    _, models = dm01.audit_store(tmp_path, nodeinfo)
    rows = dm01.build_field_rows(models["TEST_Node"]["2025_Wheat"]["sites"])
    assert len(rows) == 2
    gobi = next(r for r in rows if r["Sensor"] == "GOBI")
    assert gobi == {"Year": 2025, "Month": 3, "Day": 1, "Sensor": "GOBI",
                    "Technician": "Unknown", "Runs": 2, "Site": "Merinda",
                    "MakeNotesFile": True, "MakeTableFile": True,
                    "CheckSum": gobi["CheckSum"]}
    assert np.isnan(gobi["CheckSum"])


# ==================================================================================
# ========== Audit: fail classes ==========
# ==================================================================================
def _codes(dm01, findings, severity=None):
    df = dm01.findings_frame(findings)
    if severity:
        df = df[df["severity"] == severity]
    return set(df["code"])


def test_bad_project_name(dm01, nodeinfo, tmp_path):
    (tmp_path / "TEST_Node" / "WheatTrial").mkdir(parents=True)
    findings, _ = dm01.audit_store(tmp_path, nodeinfo)
    assert "bad_project_name" in _codes(dm01, findings, "fail")


def test_bad_site_name(dm01, nodeinfo, tmp_path):
    (tmp_path / "TEST_Node" / "2025_Wheat" / "Merinda").mkdir(parents=True)
    findings, _ = dm01.audit_store(tmp_path, nodeinfo)
    assert "bad_site_name" in _codes(dm01, findings, "fail")


def test_unknown_sensor(dm01, nodeinfo, tmp_path):
    (tmp_path / "TEST_Node" / "2025_Wheat" / "2025Merinda_F"
     / "HIRES").mkdir(parents=True)
    findings, _ = dm01.audit_store(tmp_path, nodeinfo)
    assert "unknown_sensor" in _codes(dm01, findings, "fail")


def test_bad_date_name(dm01, nodeinfo, tmp_path):
    (tmp_path / "TEST_Node" / "2025_Wheat" / "2025Merinda_F" / "GOBI"
     / "2025-03-01x").mkdir(parents=True)
    findings, _ = dm01.audit_store(tmp_path, nodeinfo)
    assert "bad_date_name" in _codes(dm01, findings, "fail")


def test_bad_run_name(dm01, nodeinfo, tmp_path):
    date_dir = (tmp_path / "TEST_Node" / "2025_Wheat" / "2025Merinda_F"
                / "GOBI" / "20250301")
    (date_dir / "run1").mkdir(parents=True)
    findings, _ = dm01.audit_store(tmp_path, nodeinfo)
    assert "bad_run_name" in _codes(dm01, findings, "fail")


def test_project_at_root(dm01, nodeinfo, tmp_path):
    _make_tree(tmp_path)
    (tmp_path / "2025_Stray").mkdir()
    findings, _ = dm01.audit_store(tmp_path, nodeinfo)
    assert "project_at_root" in _codes(dm01, findings, "fail")


def test_unrecognised_root_folder_warns(dm01, nodeinfo, tmp_path):
    _make_tree(tmp_path)
    (tmp_path / "misc_stuff").mkdir()
    findings, _ = dm01.audit_store(tmp_path, nodeinfo)
    assert "unrecognised_root_folder" in _codes(dm01, findings, "warn")
    assert "unrecognised_root_folder" not in _codes(dm01, findings, "fail")


# ==================================================================================
# ========== Audit: warn classes ==========
# ==================================================================================
def test_run_gap_warns_and_runs_inferred_max_plus_one(dm01, nodeinfo, tmp_path):
    date_dir = (tmp_path / "TEST_Node" / "2025_Wheat" / "2025Merinda_F"
                / "GOBI" / "20250301")
    _make_run(date_dir, "run_00")
    _make_run(date_dir, "run_03")
    findings, models = dm01.audit_store(tmp_path, nodeinfo)
    assert "run_gap" in _codes(dm01, findings, "warn")
    rows = dm01.build_field_rows(models["TEST_Node"]["2025_Wheat"]["sites"])
    assert rows[0]["Runs"] == 4  # max run number + 1 back-fills the gap


def test_missing_tiers_warn(dm01, nodeinfo, tmp_path):
    date_dir = (tmp_path / "TEST_Node" / "2025_Wheat" / "2025Merinda_F"
                / "GOBI" / "20250301")
    _make_run(date_dir, "run_00", tiers=("T0_raw",))
    findings, _ = dm01.audit_store(tmp_path, nodeinfo)
    assert "missing_tiers" in _codes(dm01, findings, "warn")


def test_misplaced_file_warn(dm01, nodeinfo, tmp_path):
    date_dir = (tmp_path / "TEST_Node" / "2025_Wheat" / "2025Merinda_F"
                / "GOBI" / "20250301")
    _make_run(date_dir, "run_00")
    (date_dir / "random_notes.docx").touch()
    (date_dir / "FieldNotes.txt").touch()          # allowed
    (date_dir / "run_00_Issues.yaml").touch()      # allowed
    findings, _ = dm01.audit_store(tmp_path, nodeinfo)
    df = dm01.findings_frame(findings)
    misplaced = df[df["code"] == "misplaced_file"]
    assert len(misplaced) == 1
    assert misplaced["path"].iloc[0].endswith("random_notes.docx")


# ==================================================================================
# ========== Apply: writers ==========
# ==================================================================================
def _apply_all(dm01, root, nodeinfo, models):
    for node in nodeinfo["nodes"]:
        for plan in dm01.plan_node_writes(root, node, models[node["name"]]):
            if plan["action"] != "unchanged":
                plan["write"]()


def test_writers_create_metadata(dm01, nodeinfo, tmp_path):
    node_dir = _make_tree(tmp_path)
    _, models = dm01.audit_store(tmp_path, nodeinfo)
    _apply_all(dm01, tmp_path, nodeinfo, models)

    # ProjectsSummary: both projects, sensors flagged from tree only
    df = pd.read_csv(node_dir / "TEST_Node_ProjectsSummary.csv",
                     header=0, index_col=0)
    assert bool(df.loc["2025_Wheat", "GOBI"]) is True
    assert bool(df.loc["2025_Wheat", "CALVIS"]) is True
    assert bool(df.loc["2025_Wheat", "M3M"]) is False
    assert bool(df.loc["2026_Barley", "M3M"]) is True

    # ProjectSummary.yaml: sites inverted from folder names
    with open(node_dir / "2025_Wheat" / "ProjectSummary.yaml") as fh:
        proj = yaml.safe_load(fh)
    sites = proj["project"]["sites"]
    assert len(sites) == 1
    assert sites[0]["name"] == "Merinda"
    assert sites[0]["year"] == 2025
    assert sites[0]["ControlledEnvironment"] is False
    assert sites[0]["sensors"] == []

    # FieldLog: rows with blank CheckSum and Unknown technician
    flog = pd.read_csv(node_dir / "2025_Wheat" / "FieldLog.csv")
    assert len(flog) == 2
    assert (flog["Technician"] == "Unknown").all()
    assert flog["CheckSum"].isna().all()
    assert flog["MakeNotesFile"].all() and flog["MakeTableFile"].all()


def test_append_only_merge(dm01, nodeinfo, tmp_path):
    node_dir = _make_tree(tmp_path)
    proj_dir = node_dir / "2025_Wheat"

    # Pre-existing FieldLog row (hand-made) must survive untouched.
    hand_row = pd.DataFrame([{
        "Year": 2025, "Month": 3, "Day": 1, "Sensor": "GOBI",
        "Technician": "A. Person", "Runs": 2, "Site": "Merinda",
        "MakeNotesFile": True, "MakeTableFile": True, "CheckSum": 12345.0}])
    hand_row.to_csv(proj_dir / "FieldLog.csv", index=False)

    # Pre-existing YAML with one real site already registered.
    existing_yaml = {"project": {"ShortName": "2025_Wheat", "sites": [
        {"name": "Merinda", "year": 2025, "ControlledEnvironment": False,
         "description": "hand-entered"}]}}
    with open(proj_dir / "ProjectSummary.yaml", "w") as fh:
        yaml.dump(existing_yaml, fh, sort_keys=False)

    _, models = dm01.audit_store(tmp_path, nodeinfo)
    _apply_all(dm01, tmp_path, nodeinfo, models)

    flog = pd.read_csv(proj_dir / "FieldLog.csv")
    assert len(flog) == 2  # hand row + appended CALVIS row only
    kept = flog[(flog.Sensor == "GOBI")].iloc[0]
    assert kept["Technician"] == "A. Person"
    assert kept["CheckSum"] == 12345.0

    with open(proj_dir / "ProjectSummary.yaml") as fh:
        proj = yaml.safe_load(fh)
    sites = proj["project"]["sites"]
    assert len(sites) == 1  # no duplicate appended
    assert sites[0]["description"] == "hand-entered"


def test_yaml_placeholder_site_dropped(dm01, nodeinfo, tmp_path):
    import ProjectBuilder as pb
    node_dir = _make_tree(tmp_path)
    proj_dir = node_dir / "2025_Wheat"
    with open(proj_dir / "ProjectSummary.yaml", "w") as fh:
        yaml.dump(pb._defaultProjectYAML("2025_Wheat"), fh, sort_keys=False)

    _, models = dm01.audit_store(tmp_path, nodeinfo)
    _apply_all(dm01, tmp_path, nodeinfo, models)

    with open(proj_dir / "ProjectSummary.yaml") as fh:
        proj = yaml.safe_load(fh)
    names = [s["name"] for s in proj["project"]["sites"]]
    assert names == ["Merinda"]  # template placeholder removed


def test_unchanged_second_pass(dm01, nodeinfo, tmp_path):
    _make_tree(tmp_path)
    _, models = dm01.audit_store(tmp_path, nodeinfo)
    _apply_all(dm01, tmp_path, nodeinfo, models)
    _, models2 = dm01.audit_store(tmp_path, nodeinfo)
    plans = dm01.plan_node_writes(tmp_path, nodeinfo["nodes"][0],
                                  models2["TEST_Node"])
    assert all(p["action"] == "unchanged" for p in plans)


# ==================================================================================
# ========== ProjectBuilder hand-off: Rowchecker accepts the output ==========
# ==================================================================================
def test_projectbuilder_rowchecker_accepts(dm01, nodeinfo, tmp_path):
    import ProjectBuilder as pb
    node_dir = _make_tree(tmp_path)
    _, models = dm01.audit_store(tmp_path, nodeinfo)
    _apply_all(dm01, tmp_path, nodeinfo, models)

    proj_dir = node_dir / "2025_Wheat"
    df_proj = pd.read_csv(node_dir / "TEST_Node_ProjectsSummary.csv",
                          header=0, index_col=0)
    with open(proj_dir / "ProjectSummary.yaml") as fh:
        ProjectInfo = yaml.safe_load(fh)
    flog_fname = str(proj_dir / "FieldLog.csv")
    df_flog = pd.read_csv(flog_fname)

    for _, frow in df_flog.iterrows():
        check, site = pb.Rowchecker(flog_fname, frow,
                                    df_proj.loc["2025_Wheat"],
                                    ProjectInfo, historical=True)
        assert check is not None          # checksum computed, ready to store
        assert site["name"] == "Merinda"  # site resolved from the YAML


# ==================================================================================
# ========== Report ==========
# ==================================================================================
def test_report_written(dm01, nodeinfo, tmp_path):
    node_dir = _make_tree(tmp_path)
    (node_dir / "2025_Wheat" / "BadSite").mkdir()
    findings, models = dm01.audit_store(tmp_path, nodeinfo)
    fname = dm01.write_report(tmp_path, "TEST_Node", findings,
                              models["TEST_Node"])
    assert fname.is_file()
    text = fname.read_text()
    assert "### fail" in text
    assert "BadSite" in text
    assert "TODO after" in text
    assert "2025_Wheat" in text
