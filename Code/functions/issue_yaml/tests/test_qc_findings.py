"""Tests for the QC findings loop (qc_findings grammar + run_decision).

Covers ``ensure_finding_tickets`` (authoring, grouping, idempotency,
machine-field refresh, auto-close, never-touch-human-closures,
re-author-after-fixed), the ``accepted`` ladder rung in ``classify_run``
and the ``run_decision`` inclusion/annotation contract (QC findings
plan, development-master repo).
"""

import pathlib
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

import Code.functions.issue_yaml as iy


# ==================================================================================
def make_report(checks: Dict[str, Dict[str, Any]],
                script: str = "QC01_FlightCheck",
                version: str = "v2.9",
                utc: str = "2026-09-04T00:00:00+00:00",
                sensor: Optional[str] = "CALVIS") -> Dict[str, Any]:
    """Build a minimal contract-shaped report dict.

    Parameters
    ----------
    checks : dict
        ``name -> check object`` mapping (``status``/``value``/...).
    script : str
        Contract script name.
    version : str
        Script version string.
    utc : str
        ``generated_utc`` value.
    sensor : str or None
        Run-identity sensor (None omits it).

    Returns
    -------
    dict
        Report dict shaped like ``qc_report.new_report`` output.
    """
    run = {"sensor": sensor} if sensor else {}
    return {"schema_version": "1.0",
            "script": {"name": script, "version": version},
            "run": run, "generated_utc": utc, "status": "not_evaluated",
            "checks": checks, "artifacts": [], "warnings": []}


# ==================================================================================
def fail(value: str = "38.1 %", **extra: Any) -> Dict[str, Any]:
    """Build a failing check object.

    Parameters
    ----------
    value : str
        Headline value.
    **extra : Any
        Extra check fields (``advisory``, ``waived``, ...).

    Returns
    -------
    dict
        Check object with ``status: fail``.
    """
    return {"status": "fail", "value": value, **extra}


# ==================================================================================
def good(value: str = "ok") -> Dict[str, Any]:
    """Build a passing check object.

    Parameters
    ----------
    value : str
        Headline value.

    Returns
    -------
    dict
        Check object with ``status: good``.
    """
    return {"status": "good", "value": value}


# ==================================================================================
def live_state(date_dir: pathlib.Path, run: str, script: str,
               finding: str) -> Optional[str]:
    """Read the live state of one finding from the persisted yaml.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder holding the yaml.
    run : str
        Run folder name.
    script : str
        Finding identity part 1.
    finding : str
        Finding identity part 2.

    Returns
    -------
    str or None
        The live entry's state, or None when absent.
    """
    data, state = iy.load_issue_yaml(date_dir, run)
    if state != "parsed":
        return None
    entry = iy._live_findings(data).get((script, finding))
    return None if entry is None else str(entry.get("state"))


# ==================================================================================
def set_state(date_dir: pathlib.Path, run: str, script: str, finding: str,
              state: str, note: str = "") -> None:
    """Flip a finding's state in the persisted yaml (operator stand-in).

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder holding the yaml.
    run : str
        Run folder name.
    script : str
        Finding identity part 1.
    finding : str
        Finding identity part 2.
    state : str
        New state value.
    note : str
        Note value to set alongside.
    """
    from ruamel.yaml import YAML
    fpath = date_dir / f"{run}_Issues.yaml"
    yaml_rt = YAML(typ="rt")
    with open(fpath, encoding="utf-8") as f:
        data = yaml_rt.load(f)
    entry = iy._live_findings(data)[(script, finding)]
    entry["state"] = state
    entry["note"] = note
    with open(fpath, "w", encoding="utf-8") as f:
        yaml_rt.dump(data, f)


# ==================================================================================
# ========== ensure_finding_tickets: authoring ==========
def test_author_creates_file(tmp_path):
    report = make_report({"graw_present": fail()})
    actions = iy.ensure_finding_tickets(tmp_path, "run_00", report)
    assert any("open finding graw_present" in a for a in actions)
    assert any("created" in a for a in actions)
    data, state = iy.load_issue_yaml(tmp_path, "run_00")
    assert state == "parsed"
    assert data["run"] == "run_00"
    assert data["sensor"] == "CALVIS"
    entry = iy._live_findings(data)[("QC01_FlightCheck",
                                     "graw_present")]
    assert entry["state"] == "TODO"
    assert entry["status"] == "fail"
    assert list(entry["checks"]) == ["graw_present"]
    assert entry["script_version"] == "v2.9"


def test_no_findings_no_file(tmp_path):
    report = make_report({"graw_present": good()})
    assert iy.ensure_finding_tickets(tmp_path, "run_00", report) == []
    assert not (tmp_path / "run_00_Issues.yaml").is_file()


def test_advisory_and_waived_never_author(tmp_path):
    report = make_report({
        "zeros_in_footprint_vnir": fail(advisory=True),
        "time_to_solar_noon": fail(waived="declared solar_window sweep"),
        "warning_check": {"status": "warning", "value": "x"},
    })
    assert iy.ensure_finding_tickets(tmp_path, "run_00", report) == []
    assert not (tmp_path / "run_00_Issues.yaml").is_file()


def test_grouping_aggregates(tmp_path):
    report = make_report({
        "dead_band_412nm": fail("99.9 %"), "dead_band_413nm": fail("99.8 %"),
        "over_range_vnir": fail("2.1 %"),
    }, script="QC02_SpectralCheck")  # mechanics only; QC03 authoring is deferred
    groups = {"dead_bands": ["dead_band_412nm", "dead_band_413nm"]}
    actions = iy.ensure_finding_tickets(tmp_path, "run_00", report,
                                        groups=groups)
    assert any("open finding dead_bands" in a for a in actions)
    data, _ = iy.load_issue_yaml(tmp_path, "run_00")
    live = iy._live_findings(data)
    grouped = live[("QC02_SpectralCheck", "dead_bands")]
    assert list(grouped["checks"]) == ["dead_band_412nm", "dead_band_413nm"]
    assert "dead_band_412nm: 99.9 %" in grouped["value"]
    # unclaimed failing check becomes a singleton finding
    assert ("QC02_SpectralCheck", "over_range_vnir") in live


def test_author_appends_to_existing_yaml(tmp_path):
    fpath = tmp_path / "run_00_Issues.yaml"
    fpath.write_text("schema_version: 1.0\nrun: run_00\n"
                     "payload_outcomes:\n  - payload: lidar\n"
                     "    state: ok\n", encoding="utf-8")
    report = make_report({"gcp_2d_vnir": fail("0.31 m")},
                         script="QC00_GCPCheck")
    actions = iy.ensure_finding_tickets(tmp_path, "run_00", report)
    assert actions == ["open finding gcp_2d_vnir"]
    data, _ = iy.load_issue_yaml(tmp_path, "run_00")
    assert data["payload_outcomes"][0]["state"] == "ok"  # untouched
    assert ("QC00_GCPCheck", "gcp_2d_vnir") in iy._live_findings(data)


def test_dry_run_writes_nothing(tmp_path):
    report = make_report({"graw_present": fail()})
    actions = iy.ensure_finding_tickets(tmp_path, "run_00", report,
                                        write=False)
    assert actions
    assert not (tmp_path / "run_00_Issues.yaml").is_file()


def test_unparseable_yaml_skipped(tmp_path):
    (tmp_path / "run_00_Issues.yaml").write_text(
        "run: [unclosed", encoding="utf-8")
    report = make_report({"graw_present": fail()})
    with pytest.warns(UserWarning, match="[Uu]nparseable"):
        assert iy.ensure_finding_tickets(tmp_path, "run_00", report) == []


# ========== ensure_finding_tickets: idempotency + refresh ==========
def test_idempotent_second_call(tmp_path):
    report = make_report({"graw_present": fail()})
    iy.ensure_finding_tickets(tmp_path, "run_00", report)
    before = (tmp_path / "run_00_Issues.yaml").read_bytes()
    assert iy.ensure_finding_tickets(tmp_path, "run_00", report) == []
    assert (tmp_path / "run_00_Issues.yaml").read_bytes() == before


def test_refresh_updates_machine_fields_only(tmp_path):
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    set_state(tmp_path, "run_00", "QC01_FlightCheck",
              "graw_present", "wip", note="looking at it")
    report2 = make_report({"graw_present": fail("35.0 %")},
                          version="v3.0", utc="2026-09-05T00:00:00+00:00")
    actions = iy.ensure_finding_tickets(tmp_path, "run_00", report2)
    assert actions == ["refresh graw_present"]
    data, _ = iy.load_issue_yaml(tmp_path, "run_00")
    entry = iy._live_findings(data)[("QC01_FlightCheck",
                                     "graw_present")]
    assert entry["value"] == "35.0 %"
    assert entry["script_version"] == "v3.0"
    assert entry["state"] == "wip"            # human field preserved
    assert entry["note"] == "looking at it"   # human field preserved


# ========== ensure_finding_tickets: auto-close ==========
@pytest.mark.parametrize("open_state", ["TODO", "wip"])
def test_autoclose_open_states(tmp_path, open_state):
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    if open_state != "TODO":
        set_state(tmp_path, "run_00", "QC01_FlightCheck",
                  "graw_present", open_state)
    report2 = make_report({"graw_present": good("55 %")},
                          utc="2026-09-05T00:00:00+00:00")
    actions = iy.ensure_finding_tickets(tmp_path, "run_00", report2)
    assert actions == ["close graw_present as fixed"]
    data, _ = iy.load_issue_yaml(tmp_path, "run_00")
    entry = iy._live_findings(data)[("QC01_FlightCheck",
                                     "graw_present")]
    assert entry["state"] == "fixed"
    assert entry["resolved_utc"] == "2026-09-05T00:00:00+00:00"


def test_autoclose_requires_all_members(tmp_path):
    report = make_report({"dead_band_412nm": fail(), "dead_band_413nm": fail()},
                         script="QC02_SpectralCheck")
    iy.ensure_finding_tickets(tmp_path, "run_00", report, groups={
        "dead_bands": ["dead_band_412nm", "dead_band_413nm"]})
    half = make_report({"dead_band_412nm": good(),
                        "dead_band_413nm": fail("99.1 %")},
                       script="QC02_SpectralCheck")
    actions = iy.ensure_finding_tickets(tmp_path, "run_00", half, groups={
        "dead_bands": ["dead_band_412nm", "dead_band_413nm"]})
    assert "close dead_bands as fixed" not in actions
    assert live_state(tmp_path, "run_00", "QC02_SpectralCheck",
                      "dead_bands") == "TODO"


@pytest.mark.parametrize("closure", ["accepted", "caution", "failed"])
def test_human_closures_never_touched(tmp_path, closure):
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    set_state(tmp_path, "run_00", "QC01_FlightCheck",
              "graw_present", closure, note="ruled")
    # passing re-run: no auto-close of a human closure
    passing = make_report({"graw_present": good()})
    assert iy.ensure_finding_tickets(tmp_path, "run_00", passing) == []
    # failing re-run: no re-author over a human closure
    failing = make_report({"graw_present": fail("30 %")})
    assert iy.ensure_finding_tickets(tmp_path, "run_00", failing) == []
    assert live_state(tmp_path, "run_00", "QC01_FlightCheck",
                      "graw_present") == closure


def test_other_scripts_findings_untouched(tmp_path):
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    other = make_report({"graw_present": good()},
                        script="QC00_GCPCheck")
    assert iy.ensure_finding_tickets(tmp_path, "run_00", other) == []
    assert live_state(tmp_path, "run_00", "QC01_FlightCheck",
                      "graw_present") == "TODO"


def test_refail_after_fixed_appends_new_entry(tmp_path):
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": good()}))
    actions = iy.ensure_finding_tickets(
        tmp_path, "run_00",
        make_report({"graw_present": fail("20 %")}))
    assert actions == ["open finding graw_present "
                       "(refailed after fixed)"]
    data, _ = iy.load_issue_yaml(tmp_path, "run_00")
    entries = [e for e in data["qc_findings"]
               if e.get("finding") == "graw_present"]
    assert len(entries) == 2                      # history kept
    assert entries[0]["state"] == "fixed"
    assert entries[1]["state"] == "TODO"          # live = last
    assert live_state(tmp_path, "run_00", "QC01_FlightCheck",
                      "graw_present") == "TODO"


# ========== classify_run: the accepted rung + findings channel ==========
def write_overview(date_dir: pathlib.Path, run: str = "run_00",
                   **flags: bool) -> None:
    """Write a one-row RunOverview.csv with the given trigger bools.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder to write into (created if needed).
    run : str
        Run folder name for the row.
    **flags : bool
        Column values; unset columns default False.
    """
    date_dir.mkdir(parents=True, exist_ok=True)
    cols = {"Deviations": False, "Issues": False, "RunFailed": False,
            "DuplicateRun": False, **flags}
    pd.DataFrame([{"Run": run, **cols}]).to_csv(
        date_dir / "RunOverview.csv", index=False)


def test_findings_classify_without_issues_bool(tmp_path):
    write_overview(tmp_path)  # all bools False
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    severity, detail = iy.classify_run(tmp_path, "run_00")
    assert severity == "untriaged"
    assert "QC01_FlightCheck/graw_present" in detail


def test_accepted_rung(tmp_path):
    write_overview(tmp_path)
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    set_state(tmp_path, "run_00", "QC01_FlightCheck",
              "graw_present", "accepted", note="edge line only")
    severity, detail = iy.classify_run(tmp_path, "run_00")
    assert severity == "accepted"
    assert "graw_present" in detail


def test_fixed_finding_is_clean(tmp_path):
    write_overview(tmp_path)
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": good()}))
    assert iy.classify_run(tmp_path, "run_00")[0] == "clean"


@pytest.mark.parametrize("closure,severity", [("caution", "degraded"),
                                              ("failed", "degraded")])
def test_condemned_finding_degrades(tmp_path, closure, severity):
    write_overview(tmp_path)
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    set_state(tmp_path, "run_00", "QC01_FlightCheck",
              "graw_present", closure, note="confirmed")
    assert iy.classify_run(tmp_path, "run_00")[0] == severity


def test_worst_wins_across_tickets_and_findings(tmp_path):
    write_overview(tmp_path, Issues=True)
    (tmp_path / "run_00_Issues.yaml").write_text(
        "schema_version: 1.0\nrun: run_00\npayload_outcomes:\n"
        "  - payload: lidar\n    state: caution\n", encoding="utf-8")
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    set_state(tmp_path, "run_00", "QC01_FlightCheck",
              "graw_present", "accepted", note="tolerable")
    # caution ticket outranks the accepted finding
    assert iy.classify_run(tmp_path, "run_00")[0] == "degraded"


# ========== run_decision ==========
def test_accepted_included_by_default_with_annotation(tmp_path):
    write_overview(tmp_path)
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    set_state(tmp_path, "run_00", "QC01_FlightCheck",
              "graw_present", "accepted", note="edge line only")
    decision = iy.run_decision(tmp_path, "run_00")
    assert decision.included
    assert decision.reason is None
    assert decision.annotations == (
        "accepted: QC01_FlightCheck/graw_present "
        "— edge line only",)


def test_exclude_accepted_flag(tmp_path):
    write_overview(tmp_path)
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    set_state(tmp_path, "run_00", "QC01_FlightCheck",
              "graw_present", "accepted", note="edge line only")
    decision = iy.run_decision(tmp_path, "run_00", exclude_accepted=True)
    assert not decision.included
    assert "--exclude-accepted" in decision.reason
    assert decision.annotations == ()


def test_open_finding_excludes_by_default(tmp_path):
    write_overview(tmp_path)
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    decision = iy.run_decision(tmp_path, "run_00")
    assert not decision.included
    assert "--include-runs untriaged" in decision.reason
    included = iy.run_decision(tmp_path, "run_00", include_runs="untriaged")
    assert included.included


def test_annotations_carried_at_higher_include_levels(tmp_path):
    write_overview(tmp_path, Issues=True)
    (tmp_path / "run_00_Issues.yaml").write_text(
        "schema_version: 1.0\nrun: run_00\npayload_outcomes:\n"
        "  - payload: lidar\n    state: caution\n", encoding="utf-8")
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    set_state(tmp_path, "run_00", "QC01_FlightCheck",
              "graw_present", "accepted", note="tolerable")
    decision = iy.run_decision(tmp_path, "run_00", include_runs="degraded")
    assert decision.included
    assert any("accepted: QC01_FlightCheck" in a
               for a in decision.annotations)


def test_empty_note_warns_but_annotates(tmp_path):
    write_overview(tmp_path)
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    set_state(tmp_path, "run_00", "QC01_FlightCheck",
              "graw_present", "accepted", note="")
    with pytest.warns(UserWarning, match="empty note"):
        decision = iy.run_decision(tmp_path, "run_00")
    assert decision.included
    assert "(no note)" in decision.annotations[0]


def test_reason_none_iff_included(tmp_path):
    write_overview(tmp_path)
    iy.ensure_finding_tickets(
        tmp_path, "run_00", make_report({"graw_present": fail()}))
    decision = iy.run_decision(tmp_path, "run_00")
    assert not decision.included and decision.reason
    set_state(tmp_path, "run_00", "QC01_FlightCheck",
              "graw_present", "accepted", note="fine")
    decision = iy.run_decision(tmp_path, "run_00")
    assert decision.included and decision.reason is None


def test_bad_include_runs_raises(tmp_path):
    write_overview(tmp_path)
    with pytest.raises(ValueError, match="include_runs must be one of"):
        iy.run_decision(tmp_path, "run_00", include_runs="accepted")


# ========== finding_groups: the canonical one-writer grouping policy ==========
def test_finding_groups_qc01_spec_family():
    report = {"script": {"name": "QC01_FlightCheck"},
              "checks": {name: {"status": "fail"} for name in [
                  "gsd_vnir", "frame_rate_vnir", "sidelap_vnir_calculator",
                  "sidelap_vnir_fieldbook", "oversampling_vnir_fieldbook",
                  "gsd_swir", "sidelap_lidar", "graw_present",
                  "time_to_solar_noon", "flightcal_spec"]}}
    groups = iy.finding_groups(report)
    assert sorted(groups) == ["flight_spec_swir", "flight_spec_vnir"]
    assert sorted(groups["flight_spec_vnir"]) == [
        "frame_rate_vnir", "gsd_vnir", "oversampling_vnir_fieldbook",
        "sidelap_vnir_calculator", "sidelap_vnir_fieldbook"]
    grouped = {m for members in groups.values() for m in members}
    # bundle integrity, the gate, solar timing and lidar stay singletons
    assert grouped.isdisjoint({"sidelap_lidar", "graw_present",
                               "time_to_solar_noon", "flightcal_spec"})


def test_finding_groups_qc03_per_product():
    report = {"script": {"name": "QC03_RasterCheck"},
              "products": {"vnir": {}, "swir": {}},
              "checks": {"zeros_in_footprint_vnir": {}, "negative_vnir": {},
                         "over_range_swir": {}}}
    groups = iy.finding_groups(report)
    assert sorted(groups["raster_vnir"]) == ["negative_vnir",
                                             "zeros_in_footprint_vnir"]
    assert groups["raster_swir"] == ["over_range_swir"]


def test_finding_groups_default_singletons(tmp_path):
    # QC00/QC02 grade singleton findings via the empty default
    assert iy.finding_groups({"script": {"name": "QC00_GCPCheck"},
                              "checks": {"gcp_2d_vnir": {}}}) == {}
    report = make_report({"gcp_2d_vnir": fail("0.31 m")},
                         script="QC00_GCPCheck")
    iy.ensure_finding_tickets(tmp_path, "run_00", report)
    data, _ = iy.load_issue_yaml(tmp_path, "run_00")
    assert ("QC00_GCPCheck", "gcp_2d_vnir") in iy._live_findings(data)


# ========== finding_exclusions: deferred classes never author ==========
def test_qc01_reflectance_product_deferred(tmp_path):
    report = make_report({"reflectance_product_vnir": fail("MISSING"),
                          "reflectance_product_swir": fail("MISSING"),
                          "gsd_vnir": fail("120 %")})
    actions = iy.ensure_finding_tickets(tmp_path, "run_00", report)
    # the spec fail still authors; the not-yet-processed class never does
    assert actions == ["created Issues.yaml", "open finding flight_spec_vnir"]
    data, _ = iy.load_issue_yaml(tmp_path, "run_00")
    live = iy._live_findings(data)
    assert not any("reflectance_product" in k for _, k in live)


def test_qc03_fully_deferred(tmp_path):
    report = make_report({"zeros_in_footprint_vnir": fail("18.7 %"),
                          "header_bin_integrity_swir": fail("bad header")},
                         script="QC03_RasterCheck")
    report["products"] = {"vnir": {}, "swir": {}}
    assert iy.ensure_finding_tickets(tmp_path, "run_00", report) == []
    assert not (tmp_path / "run_00_Issues.yaml").is_file()
