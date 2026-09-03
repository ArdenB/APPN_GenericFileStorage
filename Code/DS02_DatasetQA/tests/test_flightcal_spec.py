"""Regression tests: FlightCal.md section-5 worked example vs QC01 helpers.

Feeds the GRYFN Flight Calculator worked-example inputs (H = 60 m,
v = 5 m/s, d = 17 m) through the QC01_FlightCheck spec-check equation
helpers and asserts the workbook outputs are reproduced - catching
transcription errors in `reference/thresholds/flightcal_spec.yml`.

Run with:
    pytest Code/DS02_DatasetQA/tests/test_flightcal_spec.py -v
"""

import importlib.util
import pathlib
import sys

import pandas as pd
import pytest
import yaml

# ---------------------------------------------------------------------------
# Ensure repo root is importable (QC01 imports Code.functions.* at module top)
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]  # Code/DS02_DatasetQA/tests -> repo root
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ==================================================================================
@pytest.fixture(scope="module")
def qc01():
    """Import QC01_FlightCheck by file path (script naming, not a package).

    Returns
    -------
    module
        The loaded QC01_FlightCheck module.
    """
    path = _REPO_ROOT / "Code" / "DS02_DatasetQA" / "QC01_FlightCheck.py"
    spec = importlib.util.spec_from_file_location("qc01", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ==================================================================================
@pytest.fixture(scope="module")
def cal_spec():
    """Load flightcal_spec.yml.

    Returns
    -------
    dict
        Parsed spec (sensor hard limits, thresholds, assumptions).
    """
    with open(_REPO_ROOT / "reference" / "thresholds" / "flightcal_spec.yml",
              encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ==================================================================================
def test_linescan_gsd(qc01, cal_spec):
    """Worked example: nHP f=12.6 -> 2.790 cm; uVS SWIR 640 f=25 -> 3.600 cm."""
    nhp = cal_spec["linescan_sensors"]["nHP"]
    assert qc01.linescan_gsd_cm(
        nhp["pixel_size_um"], 60.0, 12.6) == pytest.approx(2.790, abs=5e-4)
    uvs = cal_spec["linescan_sensors"]["uVS"]
    assert qc01.linescan_gsd_cm(
        uvs["pixel_size_um"], 60.0, 25.0) == pytest.approx(3.600, abs=5e-4)


# ==================================================================================
def test_linescan_hard_limits(cal_spec):
    """Sensor frame-rate limits match the LineScanTable."""
    nhp = cal_spec["linescan_sensors"]["nHP"]
    assert nhp["max_frame_rate_hz"] == 250
    assert nhp["min_frame_period_ms"] == 4.00
    uvs = cal_spec["linescan_sensors"]["uVS"]
    assert uvs["max_frame_rate_hz"] == 236
    assert uvs["min_frame_period_ms"] == 4.24


# ==================================================================================
def test_lidar_worked_example(qc01, cal_spec):
    """Worked example: OS-1 rev7 128ch dual at H=60, v=5, d=17."""
    os1 = cal_spec["lidar_sensors"]["OS1-128"]
    m = qc01.lidar_line_metrics(os1, 60.0, 5.0, 17.0, return_mode=2)
    assert m["points_per_s"] == pytest.approx(436907, rel=1e-5)
    assert m["hfov_m"] == pytest.approx(69.28, abs=0.01)
    assert m["vfov_m"] == pytest.approx(46.54, abs=0.01)
    assert m["sidelap_pct"] == pytest.approx(75.5, abs=0.05)
    assert m["density_single_pts_m2"] == pytest.approx(122.3, abs=0.05)
    assert m["density_overlap_pts_m2"] == pytest.approx(306.99, abs=0.05)


# ==================================================================================
def test_vlp16_corrections(qc01, cal_spec):
    """VLP-16 corrections yield finite density despite the workbook gaps."""
    vlp = cal_spec["lidar_sensors"]["VLP16"]
    m = qc01.lidar_line_metrics(vlp, 60.0, 5.0, 17.0, return_mode=2)
    # corrected VFoV 30 deg -> 2*60*tan(15deg) = 32.15 m
    assert m["vfov_m"] == pytest.approx(32.15, abs=0.05)
    # pulses path: 18084 * 16 ch * (60/360) * 2 returns
    assert m["points_per_s"] == pytest.approx(96448, rel=1e-5)
    assert m["density_single_pts_m2"] > 0


# ==================================================================================
def test_status_classification(qc01, cal_spec):
    """Threshold classification reproduces the workbook's colour bands."""
    thr = cal_spec["thresholds"]

    def hi(v, t):
        return qc01.classify_high(v, t["good_min"], t["accept_min"],
                                  t.get("fail_below"))

    sc = thr["calculator"]["linescan_sidelap_pct"]
    assert hi(40.3, sc) == "good"          # worked example nHP sidelap
    assert hi(26.2, sc) == "acceptable"    # worked example uVS sidelap
    assert hi(-3.0, sc) == "fail"          # negative sidelap = coverage gap
    sw = thr["fieldbook"]["sidelap_pct"]["SWIR"]
    assert hi(26.2, sw) == "warning"       # fieldbook stricter than calculator
    vn = thr["fieldbook"]["sidelap_pct"]["VNIR"]
    assert hi(38.0, vn) == "acceptable"
    assert hi(40.3, vn) == "good"

    fr = thr["calculator"]["frame_rate_frac_of_max"]

    def lo(v):
        return qc01.classify_low(v, fr["good_max"], fr["warn_min"],
                                 fr.get("fail_above"))

    assert lo(179.2 / 250.0) == "acceptable"  # worked example nHP frame rate
    assert lo(0.4) == "good"
    assert lo(0.9) == "warning"
    assert lo(1.02) == "fail"                 # period below sensor minimum
    assert lo(float("nan")) == "not_checked"


# ==================================================================================
def test_flag_rogue_lines(qc01):
    """Take-off/landing and stub capture lines get flagged."""
    df = pd.DataFrame({
        "sensor_id": ["s"] * 3 + ["t"] * 3,
        "line": [0, 1, 2, 0, 1, 2],
        # observed UOA_APEX_1 pattern: survey ~37 m, descent capture ~11 m;
        # sensor t: Cali_trial_APEx2_Gobi pattern - 2.4 m stub at survey AGL
        "agl_m": [37.7, 36.7, 11.2, 29.5, 29.3, 29.4],
        "line_length_m": [95.0, 96.0, 90.0, 2.4, 97.3, 95.5],
    })
    out = qc01.flag_rogue_lines(df, frac=0.5, len_frac=0.2)
    assert out["rogue_line"].tolist() == [False, False, True,
                                          True, False, False]
    df2 = pd.DataFrame({"sensor_id": ["s"], "line": [0], "agl_m": [11.0],
                        "line_length_m": [2.0]})
    assert not qc01.flag_rogue_lines(df2, frac=0, len_frac=0)["rogue_line"].any()


# ==================================================================================
def test_annotate_flight_deviations(qc01):
    """Declared deviations note covered checks and waive covered fails."""
    report = {"checks": {
        "time_to_solar_noon": {"status": "fail",
                               "note": "APPN solar-window compliance"},
        "sidelap_vnir_fieldbook": {"status": "fail"},   # not covered
        "gsd_vnir": {"status": "good"},
    }}
    qc01.annotate_flight_deviations(report, ["solar_window"])
    noon = report["checks"]["time_to_solar_noon"]
    assert noon["status"] == "fail"                  # measured status kept
    assert "declared flight deviation: solar_window" in noon["waived"]
    assert "APPN solar-window compliance" in noon["note"]  # note preserved
    # an uncovered fail is never waived; a good check is never touched
    assert "waived" not in report["checks"]["sidelap_vnir_fieldbook"]
    assert "note" not in report["checks"]["gsd_vnir"]
    assert report["flight_deviations"] == ["solar_window"]

    # covered but passing: note added, no waiver
    report2 = {"checks": {"time_to_solar_noon": {"status": "good"}}}
    qc01.annotate_flight_deviations(report2, ["solar_window"])
    assert "waived" not in report2["checks"]["time_to_solar_noon"]
    assert "declared" in report2["checks"]["time_to_solar_noon"]["note"]

    # empty list still records the (empty) axis in the detail JSON
    report3 = {"checks": {"gsd_vnir": {"status": "good"}}}
    qc01.annotate_flight_deviations(report3, [])
    assert report3["flight_deviations"] == []
    assert "note" not in report3["checks"]["gsd_vnir"]


# ==================================================================================
def test_solar_noon_pass_fail_at_120(qc01, cal_spec):
    """APPN solar-window compliance: <=120 min good, >120 fail, no bands."""
    tn = cal_spec["thresholds"]["fieldbook"]["time_to_solar_noon_min"]

    def cls(v):
        return qc01.classify_low(v, tn["good_max"], tn["warn_min"],
                                 tn.get("fail_above"))

    assert cls(0.0) == "good"
    assert cls(120.0) == "good"
    assert cls(120.1) == "fail"
    assert cls(165.5) == "fail"          # GOBI 20260805 run_01 range
    assert cls(float("nan")) == "not_checked"


# ==================================================================================
def test_add_appn_compliance_check(qc01):
    """Hard spec bool: measured only, spec-family only, never waived."""
    # a waived noon fail must still read non-compliant
    report = {"checks": {
        "gsd_vnir": {"status": "good"},
        "time_to_solar_noon": {"status": "fail",
                               "waived": "declared flight deviation"},
        "graw_present": {"status": "warning"},   # integrity: excluded
    }}
    qc01.add_appn_compliance_check(report)
    chk = report["checks"]["appn_compliant"]
    assert chk["status"] == "fail"
    assert chk["value"] is False
    assert chk["advisory"] is True
    assert "waived" not in chk

    # all spec checks good/acceptable -> compliant
    report2 = {"checks": {
        "gsd_vnir": {"status": "good"},
        "sidelap_lidar": {"status": "acceptable"},
        "reflectance_product_vnir": {"status": "fail"},  # integrity: excluded
    }}
    qc01.add_appn_compliance_check(report2)
    assert report2["checks"]["appn_compliant"]["value"] is True
    assert report2["checks"]["appn_compliant"]["status"] == "good"

    # a spec warning (missed fieldbook target) is non-compliant
    report3 = {"checks": {"sidelap_vnir_fieldbook": {"status": "warning"}}}
    qc01.add_appn_compliance_check(report3)
    assert report3["checks"]["appn_compliant"]["value"] is False

    # nothing evaluated -> not_checked, no bool claimed
    report4 = {"checks": {"flightcal_spec": {"status": "not_checked"},
                          "graw_present": {"status": "good"}}}
    qc01.add_appn_compliance_check(report4)
    assert report4["checks"]["appn_compliant"]["status"] == "not_checked"
    assert "value" not in report4["checks"]["appn_compliant"]
