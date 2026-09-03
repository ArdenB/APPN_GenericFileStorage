"""Tests for the qc_report shared helper (DS02 reporting contract).

Covers the section-3 status vocabulary + worst-wins collapse, the
section-2/4 dual-file writer + summary projection, the legacy-tolerant
reader (section 6) and the threshold-config loader (section 5).

Run with:
    pytest Code/functions/qc_report/tests/test_qc_report.py -v
"""

import json
import pathlib
import sys

import pytest
import yaml

# ---------------------------------------------------------------------------
# Ensure repo root is importable (mirrors the core_functions test suite)
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]  # Code/functions/qc_report/tests -> repo root
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import Code.functions.qc_report as qr  # type: ignore # noqa: E402


# ===========================================================================
# 1.  Status vocabulary + collapse
# ===========================================================================
@pytest.mark.parametrize("check_status, script_status", [
    ("good", "pass"),
    ("acceptable", "pass"),
    ("warning", "warn"),
    ("fail", "fail"),
    ("not_checked", "not_evaluated"),
    # script-level statuses are idempotent
    ("pass", "pass"),
    ("warn", "warn"),
    ("not_evaluated", "not_evaluated"),
])
def test_collapse(check_status, script_status):
    assert qr.collapse(check_status) == script_status


def test_collapse_rejects_unknown_status():
    with pytest.raises(ValueError, match="Unknown status"):
        qr.collapse("great")


@pytest.mark.parametrize("statuses, expected", [
    ([], "not_evaluated"),
    (["not_checked", "not_checked"], "not_evaluated"),
    (["good", "acceptable"], "pass"),
    (["good", "warning"], "warn"),
    (["warning", "fail", "good"], "fail"),
    (["not_checked", "good"], "pass"),          # not_checked ignored
    (["not_checked", "warning"], "warn"),
    (["pass", "warn"], "warn"),                 # script-level aggregation
])
def test_worst(statuses, expected):
    assert qr.worst(statuses) == expected


def test_derive_status_excludes_advisory_checks():
    checks = {
        "gate": {"status": "good"},
        "homogeneity": {"status": "warning", "advisory": True},
    }
    assert qr.derive_status(checks) == "pass"


def test_derive_status_all_advisory_is_not_evaluated():
    checks = {"homogeneity": {"status": "fail", "advisory": True}}
    assert qr.derive_status(checks) == "not_evaluated"


def test_derive_status_waived_fail_caps_at_warn():
    checks = {
        "gate": {"status": "good"},
        "time_to_solar_noon": {
            "status": "fail",
            "waived": "declared flight deviation: solar_window"},
    }
    assert qr.derive_status(checks) == "warn"


def test_derive_status_unwaived_fail_still_fails():
    checks = {
        "waived_ok": {"status": "fail", "waived": "declared"},
        "broken": {"status": "fail"},
    }
    assert qr.derive_status(checks) == "fail"


def test_derive_status_falsy_waived_ignored():
    checks = {"noon": {"status": "fail", "waived": ""}}
    assert qr.derive_status(checks) == "fail"


# ===========================================================================
# 2.  Report build + write + summary projection
# ===========================================================================
def _example_report():
    report = qr.new_report(
        "QC01_FlightCheck", "v1.0(25.08.2026)",
        run={"node": "AU", "project": "2026_APEx", "site": "2026Rosedale",
             "sensor": "CALVIS", "date": "20260624", "run_number": "run01"})
    qr.add_check(report, "sidelap_vnir_fieldbook", "good",
                 value="46.1-47.7 %")
    qr.add_check(report, "sidelap_swir_fieldbook", "warning",
                 value="29.6-31.8 %", note="target > 30 %",
                 threshold="> 30 %", evidence=[29.6, 31.8])
    report["artifacts"].append("QC01_FlightCheck/flight_lines.csv")
    return report


def test_new_report_skeleton_shape():
    report = _example_report()
    assert report["schema_version"] == qr.schema_version()
    assert report["script"] == {"name": "QC01_FlightCheck",
                                "version": "v1.0(25.08.2026)"}
    assert report["run"]["sensor"] == "CALVIS"
    assert report["status"] == "not_evaluated"


def test_add_check_rejects_script_level_status():
    report = _example_report()
    with pytest.raises(ValueError, match="not in"):
        qr.add_check(report, "bad", "pass")


def test_waived_check_round_trips_to_summary(tmp_path):
    report = _example_report()
    qr.add_check(report, "time_to_solar_noon", "fail", value="150-160 min",
                 waived="declared flight deviation: solar_window")
    summary_path, _ = qr.write_report(tmp_path, report)
    summary = yaml.safe_load(summary_path.read_text(encoding="utf-8"))
    noon = summary["checks"]["time_to_solar_noon"]
    assert noon["status"] == "fail"                 # measured status kept
    assert noon["waived"].startswith("declared")
    assert summary["status"] == "warn"              # waived fail = warn


def test_write_report_layout_and_derived_status(tmp_path):
    qc_data = tmp_path / "QC_data"
    summary_path, detail_path = qr.write_report(qc_data, _example_report())

    # §4 layout: summary at top level, detail in the per-script subfolder
    assert summary_path == qc_data / "QC01_FlightCheck_summary.yaml"
    assert detail_path == (qc_data / "QC01_FlightCheck"
                           / "QC01_FlightCheck_detail.json")
    assert summary_path.is_file() and detail_path.is_file()

    detail = json.loads(detail_path.read_text())
    assert detail["status"] == "warn"          # worst-wins: good + warning

    summary = yaml.safe_load(summary_path.read_text())
    # summary is a pure projection of the JSON
    assert summary["status"] == detail["status"]
    assert summary["schema_version"] == detail["schema_version"]
    assert summary["run"] == detail["run"]
    assert summary["detail"] == "QC01_FlightCheck/QC01_FlightCheck_detail.json"
    assert summary["artifacts"] == ["QC01_FlightCheck/flight_lines.csv"]
    # summary check lines keep status/value/note, drop detail-only fields
    line = summary["checks"]["sidelap_swir_fieldbook"]
    assert line == {"status": "warning", "value": "29.6-31.8 %",
                    "note": "target > 30 %"}


def test_write_report_derive_false_keeps_caller_status(tmp_path):
    report = _example_report()
    report["status"] = "not_evaluated"          # advisory-only script
    _, detail_path = qr.write_report(tmp_path / "QC_data", report,
                                     derive=False)
    assert json.loads(detail_path.read_text())["status"] == "not_evaluated"


def test_write_report_derive_false_rejects_bad_status(tmp_path):
    report = _example_report()
    report["status"] = "warning"                # check-level, not script-level
    with pytest.raises(ValueError, match="Run status"):
        qr.write_report(tmp_path / "QC_data", report, derive=False)


def test_write_report_validates_check_status(tmp_path):
    report = _example_report()
    report["checks"]["broken"] = {"status": "bogus"}
    with pytest.raises(ValueError, match="broken"):
        qr.write_report(tmp_path / "QC_data", report)


def test_write_report_validates_required_keys(tmp_path):
    report = _example_report()
    del report["generated_utc"]
    with pytest.raises(ValueError, match="generated_utc"):
        qr.write_report(tmp_path / "QC_data", report)


def test_write_report_serialises_paths_and_numpy(tmp_path):
    np = pytest.importorskip("numpy")
    report = _example_report()
    qr.add_check(report, "typed", "good", value=np.float64(1.5),
                 evidence=[pathlib.Path("/tmp/x.bin")])
    _, detail_path = qr.write_report(tmp_path / "QC_data", report)
    detail = json.loads(detail_path.read_text())
    assert detail["checks"]["typed"]["value"] == 1.5
    assert detail["checks"]["typed"]["evidence"] == ["/tmp/x.bin"]


def test_write_and_read_scoped_report(tmp_path):
    # §4 QA convention: filenames carry the scope so crawls never clobber
    qa_dir = tmp_path / "QAReports"
    report = qr.new_report("QA01_FlightComparison", "v1.0")
    report["scope"] = "AU-2026Rosedale-CALVIS"
    qr.add_check(report, "solar_window", "good")
    summary_path, detail_path = qr.write_report(qa_dir, report)
    stem = "QA01_FlightComparison_AU-2026Rosedale-CALVIS"
    assert summary_path == qa_dir / f"{stem}_summary.yaml"
    assert detail_path == qa_dir / stem / f"{stem}_detail.json"
    summary = yaml.safe_load(summary_path.read_text())
    assert summary["scope"] == "AU-2026Rosedale-CALVIS"
    assert summary["detail"] == f"{stem}/{stem}_detail.json"
    result = qr.read_report(qa_dir, "QA01_FlightComparison",
                            scope="AU-2026Rosedale-CALVISXX")
    assert result is None                      # different scope, no clobber
    result = qr.read_report(qa_dir, "QA01_FlightComparison",
                            scope="AU-2026Rosedale-CALVIS")
    assert result is not None and result["status"] == "pass"


# ===========================================================================
# 3.  Reader — contract + legacy schemas, both locations
# ===========================================================================
def test_read_report_contract(tmp_path):
    qc_data = tmp_path / "QC_data"
    qr.write_report(qc_data, _example_report())
    result = qr.read_report(qc_data, "QC01_FlightCheck")
    assert result is not None
    assert result["legacy"] is False
    assert result["status"] == "warn"
    assert result["schema_version"] == qr.schema_version()
    assert result["report"]["script"]["name"] == "QC01_FlightCheck"


def test_read_report_missing_returns_none(tmp_path):
    assert qr.read_report(tmp_path / "QC_data", "QC03_RasterCheck") is None
    assert qr.read_report(tmp_path / "nonexistent", "QC00_GCPCheck") is None


def _write_legacy(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"status": {"result": result, "threshold_m": 0.10,
                    "metric": "max_m", "n_failing": 0}}))


@pytest.mark.parametrize("legacy_result, expected", [
    ("pass", "pass"),
    ("fail", "fail"),
    ("not_evaluated", "not_evaluated"),
    ("unknown", "not_evaluated"),
    ("skipped", "not_evaluated"),
])
def test_read_report_legacy_gcp_status_normalised(tmp_path, legacy_result,
                                                  expected):
    qc_data = tmp_path / "QC_data"
    _write_legacy(qc_data / "QC_GCP_distances_report.json", legacy_result)
    result = qr.read_report(qc_data, "QC00_GCPCheck")
    assert result is not None
    assert result["legacy"] is True
    assert result["status"] == expected


def test_read_report_legacy_spectra(tmp_path):
    qc_data = tmp_path / "QC_data"
    _write_legacy(qc_data / "QC_spectra_report.json", "not_evaluated")
    result = qr.read_report(qc_data, "QC02_SpectralCheck")
    assert result is not None
    assert result["legacy"] is True
    assert result["status"] == "not_evaluated"


def test_read_report_legacy_found_in_migrated_subfolder(tmp_path):
    # §4 transition rule: legacy files may already live in the subfolder
    qc_data = tmp_path / "QC_data"
    _write_legacy(qc_data / "QC00_GCPCheck" / "QC_GCP_distances_report.json",
                  "pass")
    result = qr.read_report(qc_data, "QC00_GCPCheck")
    assert result is not None
    assert result["legacy"] is True
    assert result["status"] == "pass"


def test_read_report_contract_wins_over_legacy(tmp_path):
    qc_data = tmp_path / "QC_data"
    _write_legacy(qc_data / "QC_GCP_distances_report.json", "fail")
    report = qr.new_report("QC00_GCPCheck", "v1.0")
    qr.add_check(report, "gcp_2d", "good", value="0.03 m")
    qr.write_report(qc_data, report)
    result = qr.read_report(qc_data, "QC00_GCPCheck")
    assert result["legacy"] is False
    assert result["status"] == "pass"


def test_read_report_newest_legacy_wins(tmp_path):
    import os
    qc_data = tmp_path / "QC_data"
    old = qc_data / "QC_GCP_old_distances_report.json"
    new = qc_data / "QC_GCP_new_distances_report.json"
    _write_legacy(old, "fail")
    _write_legacy(new, "pass")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    result = qr.read_report(qc_data, "QC00_GCPCheck")
    assert result["path"] == new
    assert result["status"] == "pass"


def test_legacy_report_globs_unknown_script_is_empty():
    assert qr.legacy_report_globs("QC03_RasterCheck") == []


# ===========================================================================
# 4.  Threshold loader
# ===========================================================================
def test_load_thresholds_spec_and_snapshot(tmp_path):
    spec_file = tmp_path / "gcp_limits.yml"
    spec_file.write_text("max_2d_m: 0.10\nmax_bias_m: 0.04\n")
    result = qr.load_thresholds("gcp_limits", thresholds_dir=tmp_path)
    assert result["spec"] == {"max_2d_m": 0.10, "max_bias_m": 0.04}
    assert result["path"] == spec_file.as_posix()
    import hashlib
    assert result["sha256"] == hashlib.sha256(spec_file.read_bytes()).hexdigest()


def test_load_thresholds_explicit_extension(tmp_path):
    (tmp_path / "spec.yaml").write_text("a: 1\n")
    result = qr.load_thresholds("spec.yaml", thresholds_dir=tmp_path)
    assert result["spec"] == {"a": 1}


def test_load_thresholds_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="nope"):
        qr.load_thresholds("nope", thresholds_dir=tmp_path)


def test_default_thresholds_dir_points_into_repo():
    folder = qr.default_thresholds_dir()
    assert folder.parts[-2:] == ("reference", "thresholds")
    assert pathlib.Path(_REPO_ROOT) in folder.parents


# ===========================================================================
# 5.  Version-aware cache invalidation
# ===========================================================================
@pytest.mark.parametrize("version, key", [
    ("v2.2(03.09.2026)", (2, 2)),
    ("v10.0(01.01.2026)", (10, 0)),
    ("3.14", (3, 14)),
    ("no digits here", None),
    (None, None),
])
def test_version_key(version, key):
    assert qr.version_key(version) == key


def _write_versioned_report(tmp_path, version):
    report = qr.new_report("QC01_FlightCheck", version)
    qr.add_check(report, "graw_present", "good")
    qr.write_report(tmp_path, report)


def test_report_is_current_matches_numeric_version(tmp_path):
    _write_versioned_report(tmp_path, "v2.3(03.09.2026)")
    # same numbers, different date -> still current (doc-only touch)
    current, reason = qr.report_is_current(
        tmp_path, "QC01_FlightCheck", "v2.3(31.12.2026)")
    assert current and reason is None


def test_report_is_current_stale_on_version_bump(tmp_path):
    _write_versioned_report(tmp_path, "v2.2(03.09.2026)")
    current, reason = qr.report_is_current(
        tmp_path, "QC01_FlightCheck", "v2.3(03.09.2026)")
    assert not current
    assert "written by v2.2(03.09.2026)" in reason


def test_report_is_current_missing_report(tmp_path):
    current, reason = qr.report_is_current(
        tmp_path, "QC01_FlightCheck", "v2.3(03.09.2026)")
    assert not current
    assert reason == "no contract report"


def test_report_is_current_unparseable_recorded_version(tmp_path):
    _write_versioned_report(tmp_path, "dev-build")
    current, reason = qr.report_is_current(
        tmp_path, "QC01_FlightCheck", "v2.3(03.09.2026)")
    assert not current
    assert "no parseable script version" in reason
