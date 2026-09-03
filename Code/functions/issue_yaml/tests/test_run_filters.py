"""Tests for the QA run-severity classifier and exclusion helper.

Covers ``read_triggers`` edge cases only where they feed
``classify_run`` / ``run_exclusion``; the template generator has its
own coverage via PS00 usage.
"""

import pathlib

import pandas as pd
import pytest

import Code.functions.issue_yaml as iy


# ==================================================================================
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
        Column values (``Deviations``, ``Issues``, ``RunFailed``,
        ``DuplicateRun``); unset columns default False.

    Returns
    -------
    None
    """
    date_dir.mkdir(parents=True, exist_ok=True)
    cols = {"Deviations": False, "Issues": False, "RunFailed": False,
            "DuplicateRun": False, **flags}
    pd.DataFrame([{"Run": run, **cols}]).to_csv(
        date_dir / "RunOverview.csv", index=False)


# ==================================================================================
def write_issues_yaml(date_dir: pathlib.Path, run: str,
                      states: list) -> None:
    """Write a minimal parseable issue YAML with the given ticket states.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder to write into.
    run : str
        Run folder name (file becomes ``<run>_Issues.yaml``).
    states : list of str
        One ``payload_outcomes`` ticket state per entry.

    Returns
    -------
    None
    """
    lines = [f"run: {run}", "triggers: [Issues]", "payload_outcomes:"]
    for i, state in enumerate(states):
        lines += [f"  - payload: payload_{i}", f"    state: {state}"]
    if not states:
        lines[-1] = "payload_outcomes: []"
    (date_dir / f"{run}_Issues.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


# ==================================================================================
def write_compliance_yaml(date_dir: pathlib.Path, run: str,
                          compliant: list, note: str = "") -> None:
    """Write a minimal parseable issue YAML with a flight_compliance list.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder to write into.
    run : str
        Run folder name (file becomes ``<run>_Issues.yaml``).
    compliant : list of str
        Kept ``flight_compliance`` entries (deleted entries = declared
        deviations).
    note : str
        The ``design_note`` value.

    Returns
    -------
    None
    """
    lines = [f"run: {run}", "triggers: [Deviations]",
             f"flight_compliance: [{', '.join(compliant)}]",
             f'design_note: "{note}"']
    (date_dir / f"{run}_Issues.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


# ==================================================================================
class TestClassifyRun:
    """Severity ladder classification from flags + ticket states."""

    def test_no_runoverview_is_clean(self, tmp_path):
        severity, _ = iy.classify_run(tmp_path, "run_00")
        assert severity == "clean"

    def test_deviations_only_is_clean(self, tmp_path):
        write_overview(tmp_path, Deviations=True)
        assert iy.classify_run(tmp_path, "run_00")[0] == "clean"

    def test_runfailed_is_failed_without_reading_yaml(self, tmp_path):
        write_overview(tmp_path, RunFailed=True, Issues=True)
        write_issues_yaml(tmp_path, "run_00", ["ok"])
        severity, detail = iy.classify_run(tmp_path, "run_00")
        assert severity == "failed"
        assert "RunFailed" in detail

    def test_issues_without_yaml_is_untriaged(self, tmp_path):
        write_overview(tmp_path, Issues=True)
        severity, detail = iy.classify_run(tmp_path, "run_00")
        assert severity == "untriaged"
        assert "no Issues.yaml" in detail

    def test_issues_with_open_tickets_is_untriaged(self, tmp_path):
        write_overview(tmp_path, Issues=True)
        write_issues_yaml(tmp_path, "run_00", ["TODO", "ok"])
        assert iy.classify_run(tmp_path, "run_00")[0] == "untriaged"

    def test_wip_ticket_is_untriaged(self, tmp_path):
        write_overview(tmp_path, Issues=True)
        write_issues_yaml(tmp_path, "run_00", ["wip"])
        assert iy.classify_run(tmp_path, "run_00")[0] == "untriaged"

    def test_empty_ticket_list_is_untriaged(self, tmp_path):
        write_overview(tmp_path, Issues=True)
        write_issues_yaml(tmp_path, "run_00", [])
        assert iy.classify_run(tmp_path, "run_00")[0] == "untriaged"

    def test_caution_ticket_is_degraded(self, tmp_path):
        write_overview(tmp_path, Issues=True)
        write_issues_yaml(tmp_path, "run_00", ["ok", "caution"])
        severity, detail = iy.classify_run(tmp_path, "run_00")
        assert severity == "degraded"
        assert "payload_1" in detail

    def test_failed_ticket_beats_open_ticket(self, tmp_path):
        write_overview(tmp_path, Issues=True)
        write_issues_yaml(tmp_path, "run_00", ["TODO", "failed"])
        assert iy.classify_run(tmp_path, "run_00")[0] == "degraded"

    def test_all_tickets_resolved_is_clean(self, tmp_path):
        write_overview(tmp_path, Issues=True)
        write_issues_yaml(tmp_path, "run_00", ["ok", "fixed"])
        severity, detail = iy.classify_run(tmp_path, "run_00")
        assert severity == "clean"
        assert "resolved" in detail

    def test_unparseable_yaml_is_degraded(self, tmp_path):
        write_overview(tmp_path, Issues=True)
        (tmp_path / "run_00_Issues.yaml").write_text(
            "run: [unclosed\n", encoding="utf-8")
        with pytest.warns(UserWarning, match="Unparseable"):
            severity, detail = iy.classify_run(tmp_path, "run_00")
        assert severity == "degraded"
        assert "unparseable" in detail


# ==================================================================================
class TestRunExclusion:
    """Cumulative --include-runs ladder + orthogonal duplicate toggle."""

    def test_clean_run_always_included(self, tmp_path):
        write_overview(tmp_path)
        assert iy.run_exclusion(tmp_path, "run_00") is None

    @pytest.mark.parametrize("level,expected_excluded", [
        (None, True), ("untriaged", True), ("degraded", True),
        ("failed", False)])
    def test_failed_needs_top_level(self, tmp_path, level, expected_excluded):
        write_overview(tmp_path, RunFailed=True)
        reason = iy.run_exclusion(tmp_path, "run_00", include_runs=level)
        assert (reason is not None) == expected_excluded

    @pytest.mark.parametrize("level,expected_excluded", [
        (None, True), ("untriaged", False), ("degraded", False),
        ("failed", False)])
    def test_untriaged_ladder_is_cumulative(self, tmp_path, level,
                                            expected_excluded):
        write_overview(tmp_path, Issues=True)
        reason = iy.run_exclusion(tmp_path, "run_00", include_runs=level)
        assert (reason is not None) == expected_excluded

    @pytest.mark.parametrize("level,expected_excluded", [
        (None, True), ("untriaged", True), ("degraded", False),
        ("failed", False)])
    def test_degraded_ladder_is_cumulative(self, tmp_path, level,
                                           expected_excluded):
        write_overview(tmp_path, Issues=True)
        write_issues_yaml(tmp_path, "run_00", ["caution"])
        reason = iy.run_exclusion(tmp_path, "run_00", include_runs=level)
        assert (reason is not None) == expected_excluded

    def test_exclusion_reason_names_the_flag(self, tmp_path):
        write_overview(tmp_path, RunFailed=True)
        reason = iy.run_exclusion(tmp_path, "run_00")
        assert "--include-runs failed" in reason

    def test_duplicate_excluded_by_default(self, tmp_path):
        write_overview(tmp_path, DuplicateRun=True)
        reason = iy.run_exclusion(tmp_path, "run_00")
        assert reason is not None and "--include-duplicates" in reason

    def test_duplicate_opt_in(self, tmp_path):
        write_overview(tmp_path, DuplicateRun=True)
        assert iy.run_exclusion(tmp_path, "run_00",
                                include_duplicates=True) is None

    def test_duplicate_axis_is_orthogonal(self, tmp_path):
        # include-runs failed alone must NOT pull in a duplicate
        write_overview(tmp_path, DuplicateRun=True, RunFailed=True)
        assert iy.run_exclusion(tmp_path, "run_00",
                                include_runs="failed") is not None
        assert iy.run_exclusion(tmp_path, "run_00", include_runs="failed",
                                include_duplicates=True) is None

    def test_resolved_issues_run_rejoins_default(self, tmp_path):
        write_overview(tmp_path, Issues=True)
        write_issues_yaml(tmp_path, "run_00", ["fixed", "ok"])
        assert iy.run_exclusion(tmp_path, "run_00") is None

    def test_invalid_level_raises(self, tmp_path):
        write_overview(tmp_path)
        with pytest.raises(ValueError, match="include_runs"):
            iy.run_exclusion(tmp_path, "run_00", include_runs="everything")


# ==================================================================================
class TestFlightDeviations:
    """flight_compliance delete-down list, exclusion axis, template + patcher."""

    def test_no_yaml_reads_compliant(self, tmp_path):
        assert iy.run_flight_deviations(tmp_path, "run_00") == []

    def test_missing_key_reads_compliant(self, tmp_path):
        write_issues_yaml(tmp_path, "run_00", ["ok"])
        assert iy.run_flight_deviations(tmp_path, "run_00") == []

    def test_deleted_entry_is_the_declared_deviation(self, tmp_path):
        write_compliance_yaml(tmp_path, "run_00",
                              ["flight_pattern", "sensor_config"])
        assert iy.run_flight_deviations(tmp_path, "run_00") == \
            ["solar_window"]

    def test_full_list_reads_compliant(self, tmp_path):
        # untouched template = fully compliant, declares nothing
        write_compliance_yaml(tmp_path, "run_00",
                              list(iy.flight_deviation_vocab()))
        assert iy.run_flight_deviations(tmp_path, "run_00") == []

    def test_empty_list_is_all_deviations(self, tmp_path):
        write_compliance_yaml(tmp_path, "run_00", [])
        assert iy.run_flight_deviations(tmp_path, "run_00") == \
            list(iy.flight_deviation_vocab())

    def test_unknown_entry_warns_and_is_ignored(self, tmp_path):
        write_compliance_yaml(tmp_path, "run_00",
                              ["solar_window", "flight_pattern",
                               "sensor_config", "night_flight"])
        with pytest.warns(UserWarning, match="unknown"):
            devs = iy.run_flight_deviations(tmp_path, "run_00")
        assert devs == []          # typo never subtracts from compliance

    def test_deviation_excluded_by_default(self, tmp_path):
        write_overview(tmp_path, Deviations=True)
        write_compliance_yaml(tmp_path, "run_00",
                              ["flight_pattern", "sensor_config"])
        reason = iy.run_exclusion(tmp_path, "run_00")
        assert reason is not None
        assert "--include-flight-deviations" in reason
        assert "solar_window" in reason

    def test_deviation_opt_in(self, tmp_path):
        write_overview(tmp_path, Deviations=True)
        write_compliance_yaml(tmp_path, "run_00",
                              ["flight_pattern", "sensor_config"])
        assert iy.run_exclusion(tmp_path, "run_00",
                                include_flight_deviations=True) is None

    def test_fully_compliant_run_included(self, tmp_path):
        # untouched full list: run was compliant after all
        write_overview(tmp_path, Deviations=True)
        write_compliance_yaml(tmp_path, "run_00",
                              list(iy.flight_deviation_vocab()))
        assert iy.run_exclusion(tmp_path, "run_00") is None

    def test_deviation_axis_is_orthogonal(self, tmp_path):
        # include-runs failed alone must NOT pull in a deviation run
        write_overview(tmp_path, Deviations=True, RunFailed=True)
        write_compliance_yaml(tmp_path, "run_00",
                              ["solar_window", "sensor_config"])
        assert iy.run_exclusion(tmp_path, "run_00",
                                include_runs="failed") is not None
        assert iy.run_exclusion(tmp_path, "run_00",
                                include_flight_deviations=True) is not None
        assert iy.run_exclusion(tmp_path, "run_00", include_runs="failed",
                                include_flight_deviations=True) is None

    def test_template_deviations_emit_block(self, tmp_path):
        text = iy.render_issue_template(
            "run_00", "GOBI",
            {"Deviations": True, "Issues": False, "RunFailed": False},
            pipeline=None)
        assert ("flight_compliance: "
                "[solar_window, flight_pattern, sensor_config]") in text
        assert 'design_note: ""' in text
        assert "payload_outcomes" not in text

    def test_template_no_deviations_no_block(self, tmp_path):
        text = iy.render_issue_template(
            "run_00", "GOBI",
            {"Deviations": False, "Issues": True, "RunFailed": False},
            pipeline=None)
        assert "flight_compliance" not in text

    def test_patch_appends_block_once(self, tmp_path):
        write_issues_yaml(tmp_path, "run_00", ["ok"])
        fpath = tmp_path / "run_00_Issues.yaml"
        triggers = {"Deviations": True, "Issues": True, "RunFailed": False}
        actions = iy.patch_issue_yaml(fpath, triggers, pipeline=None,
                                      write_enabled=True)
        assert any("flight_compliance" in a for a in actions)
        # freshly patched full list = compliant, declares nothing
        assert iy.run_flight_deviations(tmp_path, "run_00") == []
        # second pass is a no-op for the block
        actions = iy.patch_issue_yaml(fpath, triggers, pipeline=None,
                                      write_enabled=True)
        assert not any("flight_compliance" in a for a in actions)

    def test_patch_never_rewrites_operator_edits(self, tmp_path):
        write_overview(tmp_path, Deviations=True)
        write_compliance_yaml(tmp_path, "run_00",
                              ["flight_pattern", "sensor_config"], "sweep")
        fpath = tmp_path / "run_00_Issues.yaml"
        triggers = {"Deviations": True, "Issues": False, "RunFailed": False}
        iy.patch_issue_yaml(fpath, triggers, pipeline=None,
                            write_enabled=True)
        assert iy.run_flight_deviations(tmp_path, "run_00") == \
            ["solar_window"]


# ==================================================================================
class TestReadDuplicate:
    """RunOverview DuplicateRun parsing (moved here from PS00)."""

    def test_missing_file_is_false(self, tmp_path):
        assert iy.read_duplicate(tmp_path, "run_00") is False

    def test_missing_column_is_false(self, tmp_path):
        tmp_path.mkdir(exist_ok=True)
        pd.DataFrame([{"Run": "run_00", "Issues": True}]).to_csv(
            tmp_path / "RunOverview.csv", index=False)
        assert iy.read_duplicate(tmp_path, "run_00") is False

    @pytest.mark.parametrize("val,expected", [
        ("TRUE", True), ("yes", True), ("1", True),
        ("false", False), ("", False)])
    def test_truthy_strings(self, tmp_path, val, expected):
        pd.DataFrame([{"Run": "run_00", "DuplicateRun": val}]).to_csv(
            tmp_path / "RunOverview.csv", index=False)
        assert iy.read_duplicate(tmp_path, "run_00") is expected
