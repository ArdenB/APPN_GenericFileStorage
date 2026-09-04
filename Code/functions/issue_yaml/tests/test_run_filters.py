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
def run_exclusion(*args, **kwargs):
    """Legacy-contract shim: exclusion reason via ``run_decision``.

    The historical ``run_exclusion`` API (None = include, else reason)
    was retired with the QC findings loop; these tests keep asserting
    through the same contract so the reason strings and axis ordering
    stay pinned.

    Parameters
    ----------
    *args : Any
        Positional arguments for ``iy.run_decision``.
    **kwargs : Any
        Keyword arguments for ``iy.run_decision``.

    Returns
    -------
    str or None
        None when the run is included; otherwise the exclusion reason.
    """
    decision = iy.run_decision(*args, **kwargs)
    return None if decision.included else decision.reason


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
def write_overview_rows(date_dir: pathlib.Path, rows: list) -> None:
    """Write a multi-row RunOverview.csv for duplicate-group tests.

    Parameters
    ----------
    date_dir : pathlib.Path
        Date folder to write into (created if needed).
    rows : list of dict
        One dict per run with a ``Run`` key; unset trigger/duplicate
        columns default to False (``DupOf`` to empty).

    Returns
    -------
    None
    """
    date_dir.mkdir(parents=True, exist_ok=True)
    defaults = {"Deviations": False, "Issues": False, "RunFailed": False,
                "DuplicateRun": False, "DupOf": "", "BestRun": False}
    pd.DataFrame([{**defaults, **row} for row in rows]).to_csv(
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
        assert run_exclusion(tmp_path, "run_00") is None

    @pytest.mark.parametrize("level,expected_excluded", [
        (None, True), ("untriaged", True), ("degraded", True),
        ("failed", False)])
    def test_failed_needs_top_level(self, tmp_path, level, expected_excluded):
        write_overview(tmp_path, RunFailed=True)
        reason = run_exclusion(tmp_path, "run_00", include_runs=level)
        assert (reason is not None) == expected_excluded

    @pytest.mark.parametrize("level,expected_excluded", [
        (None, True), ("untriaged", False), ("degraded", False),
        ("failed", False)])
    def test_untriaged_ladder_is_cumulative(self, tmp_path, level,
                                            expected_excluded):
        write_overview(tmp_path, Issues=True)
        reason = run_exclusion(tmp_path, "run_00", include_runs=level)
        assert (reason is not None) == expected_excluded

    @pytest.mark.parametrize("level,expected_excluded", [
        (None, True), ("untriaged", True), ("degraded", False),
        ("failed", False)])
    def test_degraded_ladder_is_cumulative(self, tmp_path, level,
                                           expected_excluded):
        write_overview(tmp_path, Issues=True)
        write_issues_yaml(tmp_path, "run_00", ["caution"])
        reason = run_exclusion(tmp_path, "run_00", include_runs=level)
        assert (reason is not None) == expected_excluded

    def test_exclusion_reason_names_the_flag(self, tmp_path):
        write_overview(tmp_path, RunFailed=True)
        reason = run_exclusion(tmp_path, "run_00")
        assert "--include-runs failed" in reason

    def test_duplicate_excluded_by_default(self, tmp_path):
        write_overview(tmp_path, DuplicateRun=True)
        reason = run_exclusion(tmp_path, "run_00")
        assert reason is not None and "--include-duplicates" in reason

    def test_duplicate_opt_in(self, tmp_path):
        write_overview(tmp_path, DuplicateRun=True)
        assert run_exclusion(tmp_path, "run_00",
                                include_duplicates=True) is None

    def test_group_original_wins_by_default(self, tmp_path):
        write_overview_rows(tmp_path, [
            {"Run": "run_00"},
            {"Run": "run_01", "DuplicateRun": True, "DupOf": 0}])
        assert run_exclusion(tmp_path, "run_00") is None
        reason = run_exclusion(tmp_path, "run_01")
        assert reason is not None
        assert "run_00" in reason and "--include-duplicates" in reason

    def test_bestrun_duplicate_demotes_original(self, tmp_path):
        write_overview_rows(tmp_path, [
            {"Run": "run_00"},
            {"Run": "run_01", "DuplicateRun": True, "DupOf": 0,
             "BestRun": True}])
        # The winning reprocess is included despite DuplicateRun=True
        assert run_exclusion(tmp_path, "run_01") is None
        reason = run_exclusion(tmp_path, "run_00")
        assert reason is not None
        assert "superseded by run_01" in reason
        assert "--include-duplicates" in reason

    def test_include_duplicates_restores_everything(self, tmp_path):
        write_overview_rows(tmp_path, [
            {"Run": "run_00"},
            {"Run": "run_01", "DuplicateRun": True, "DupOf": 0,
             "BestRun": True}])
        for run in ("run_00", "run_01"):
            assert run_exclusion(tmp_path, run,
                                    include_duplicates=True) is None

    def test_double_bestrun_raises(self, tmp_path):
        write_overview_rows(tmp_path, [
            {"Run": "run_00", "BestRun": True},
            {"Run": "run_01", "DuplicateRun": True, "DupOf": 0,
             "BestRun": True}])
        with pytest.raises(ValueError, match="BestRun"):
            run_exclusion(tmp_path, "run_01")

    def test_duplicate_axis_is_orthogonal(self, tmp_path):
        # include-runs failed alone must NOT pull in a duplicate
        write_overview(tmp_path, DuplicateRun=True, RunFailed=True)
        assert run_exclusion(tmp_path, "run_00",
                                include_runs="failed") is not None
        assert run_exclusion(tmp_path, "run_00", include_runs="failed",
                                include_duplicates=True) is None

    def test_resolved_issues_run_rejoins_default(self, tmp_path):
        write_overview(tmp_path, Issues=True)
        write_issues_yaml(tmp_path, "run_00", ["fixed", "ok"])
        assert run_exclusion(tmp_path, "run_00") is None

    def test_invalid_level_raises(self, tmp_path):
        write_overview(tmp_path)
        with pytest.raises(ValueError, match="include_runs"):
            run_exclusion(tmp_path, "run_00", include_runs="everything")


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
        reason = run_exclusion(tmp_path, "run_00")
        assert reason is not None
        assert "--include-flight-deviations" in reason
        assert "solar_window" in reason

    def test_deviation_opt_in(self, tmp_path):
        write_overview(tmp_path, Deviations=True)
        write_compliance_yaml(tmp_path, "run_00",
                              ["flight_pattern", "sensor_config"])
        assert run_exclusion(tmp_path, "run_00",
                                include_flight_deviations=True) is None

    def test_fully_compliant_run_included(self, tmp_path):
        # untouched full list: run was compliant after all
        write_overview(tmp_path, Deviations=True)
        write_compliance_yaml(tmp_path, "run_00",
                              list(iy.flight_deviation_vocab()))
        assert run_exclusion(tmp_path, "run_00") is None

    def test_deviation_axis_is_orthogonal(self, tmp_path):
        # include-runs failed alone must NOT pull in a deviation run
        write_overview(tmp_path, Deviations=True, RunFailed=True)
        write_compliance_yaml(tmp_path, "run_00",
                              ["solar_window", "sensor_config"])
        assert run_exclusion(tmp_path, "run_00",
                                include_runs="failed") is not None
        assert run_exclusion(tmp_path, "run_00",
                                include_flight_deviations=True) is not None
        assert run_exclusion(tmp_path, "run_00", include_runs="failed",
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


# ==================================================================================
class TestRunDisposition:
    """Duplicate-group resolution and winner selection."""

    def test_missing_file_is_empty(self, tmp_path):
        assert iy.run_disposition(tmp_path) == {}

    def test_no_duplicate_columns_all_winners(self, tmp_path):
        pd.DataFrame([{"Run": "run_00", "Issues": True},
                      {"Run": "run_01", "Issues": False}]).to_csv(
            tmp_path / "RunOverview.csv", index=False)
        disp = iy.run_disposition(tmp_path)
        assert all(d["is_winner"] for d in disp.values())
        assert not any(d["is_duplicate"] for d in disp.values())

    def test_flat_duplicate_never_wins(self, tmp_path):
        # Legacy table: DuplicateRun set, no DupOf/BestRun columns
        write_overview(tmp_path, DuplicateRun=True)
        disp = iy.run_disposition(tmp_path)["run_00"]
        assert disp["is_duplicate"] is True
        assert disp["dup_of"] is None
        assert disp["winner"] is None
        assert disp["is_winner"] is False

    def test_flat_duplicate_self_promotes_via_bestrun(self, tmp_path):
        write_overview_rows(tmp_path, [
            {"Run": "run_00", "DuplicateRun": True, "BestRun": True}])
        assert iy.run_disposition(tmp_path)["run_00"]["is_winner"] is True

    def test_group_default_winner_is_original(self, tmp_path):
        write_overview_rows(tmp_path, [
            {"Run": "run_00"},
            {"Run": "run_01", "DuplicateRun": True, "DupOf": 0}])
        disp = iy.run_disposition(tmp_path)
        assert disp["run_00"]["is_winner"] is True
        assert disp["run_01"]["is_winner"] is False
        assert disp["run_01"]["dup_of"] == "run_00"
        assert disp["run_01"]["winner"] == "run_00"

    def test_bestrun_overrides_original(self, tmp_path):
        write_overview_rows(tmp_path, [
            {"Run": "run_00"},
            {"Run": "run_01", "DuplicateRun": True, "DupOf": 0},
            {"Run": "run_02", "DuplicateRun": True, "DupOf": 0,
             "BestRun": True}])
        disp = iy.run_disposition(tmp_path)
        assert disp["run_00"]["is_winner"] is False
        assert disp["run_01"]["is_winner"] is False
        assert disp["run_02"]["is_winner"] is True
        assert all(d["winner"] == "run_02" for d in disp.values())

    @pytest.mark.parametrize("dupof", [0, 0.0, "0", "0.0", "run_00"])
    def test_dupof_value_formats(self, tmp_path, dupof):
        write_overview_rows(tmp_path, [
            {"Run": "run_00"},
            {"Run": "run_01", "DuplicateRun": True, "DupOf": dupof}])
        assert iy.run_disposition(tmp_path)["run_01"]["dup_of"] == "run_00"

    def test_chain_resolves_to_root(self, tmp_path):
        write_overview_rows(tmp_path, [
            {"Run": "run_00"},
            {"Run": "run_01", "DuplicateRun": True, "DupOf": 0},
            {"Run": "run_02", "DuplicateRun": True, "DupOf": 1}])
        disp = iy.run_disposition(tmp_path)
        assert disp["run_02"]["dup_of"] == "run_00"
        assert disp["run_02"]["winner"] == "run_00"

    def test_dangling_dupof_warns_and_degrades_to_flat(self, tmp_path):
        write_overview_rows(tmp_path, [
            {"Run": "run_00", "DuplicateRun": True, "DupOf": 7}])
        with pytest.warns(UserWarning, match="DupOf"):
            disp = iy.run_disposition(tmp_path)
        assert disp["run_00"]["dup_of"] is None
        assert disp["run_00"]["is_winner"] is False

    def test_dupof_without_flag_warns_and_is_duplicate(self, tmp_path):
        write_overview_rows(tmp_path, [
            {"Run": "run_00"},
            {"Run": "run_01", "DupOf": 0}])
        with pytest.warns(UserWarning, match="DuplicateRun is not set"):
            disp = iy.run_disposition(tmp_path)
        assert disp["run_01"]["is_duplicate"] is True
        assert disp["run_01"]["winner"] == "run_00"

    def test_circular_dupof_warns(self, tmp_path):
        write_overview_rows(tmp_path, [
            {"Run": "run_00", "DuplicateRun": True, "DupOf": 1},
            {"Run": "run_01", "DuplicateRun": True, "DupOf": 0}])
        with pytest.warns(UserWarning, match="circular"):
            disp = iy.run_disposition(tmp_path)
        assert all(d["is_winner"] is False for d in disp.values())

    def test_double_bestrun_in_group_raises(self, tmp_path):
        write_overview_rows(tmp_path, [
            {"Run": "run_00", "BestRun": True},
            {"Run": "run_01", "DuplicateRun": True, "DupOf": 0,
             "BestRun": True}])
        with pytest.raises(ValueError, match="at most one winner"):
            iy.run_disposition(tmp_path)
