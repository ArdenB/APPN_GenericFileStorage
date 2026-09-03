"""Tests for the qc_report markdown fragment + assembly renderer.

Covers the QC-report markdown design (QC-report plan, development-master
repo): fragment content per
script, assembly with all/partial/no fragments, the pre-fragment and
legacy stubs, never-raise behaviour, relative-path/figure handling and
idempotence (re-assembly byte-identical apart from timestamps).

Run with:
    pytest Code/functions/qc_report/tests/test_markdown.py -v
"""

import json
import pathlib
import re
import sys

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is importable (mirrors the core_functions test suite)
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import Code.functions.qc_report as qr  # type: ignore # noqa: E402


# ===========================================================================
# Fixtures — minimal contract reports shaped like the real detail JSONs
# ===========================================================================
def _base_report(name, version="v9.9(03.09.2026)", status="pass"):
    return {
        "schema_version": "1.0",
        "script": {"name": name, "version": version},
        "run": {"node": "USYD_Narrabri", "project": "2026_Test",
                "site": "TestSite", "sensor": "CALVIS",
                "date": "2026-08-19T00:00:00", "run": 1,
                "gpro": "test.gpro"},
        "generated_utc": "2026-09-03T00:00:00+00:00",
        "status": status,
        "checks": {},
        "artifacts": [],
        "warnings": [],
    }


@pytest.fixture
def qc00_report():
    report = _base_report("QC00_GCPCheck", status="fail")
    report["checks"] = {
        "gcp_2d": {"status": "fail", "value": "max 0.29 m (n=5)",
                   "threshold": "<= 0.1 m on all points"},
        "height_bias": {"status": "good", "advisory": True},
    }
    report["pairs"] = {
        "QC_GCP_distances": {
            "counts": {"matched": 5},
            "statistics_metres": {
                "distance_2d": {"n": 5, "mean": 0.05, "max": 0.29,
                                "rmse": 0.12}},
            "bias": {"planar_2d": {"bias_magnitude_m": 0.016}},
            "status": {"result": "fail"},
        },
    }
    report["config"] = {"path": "reference/thresholds/gcp_limits.yml",
                        "sha256": "abc123def4567890"}
    report["artifacts"] = [
        "QC00_GCPCheck/QC_GCP_distances.parquet",
        "QC00_GCPCheck/QC_plots/QC_GCP_distances_displacements.png"]
    return report


@pytest.fixture
def qc01_report():
    report = _base_report("QC01_FlightCheck", status="warn")
    report["checks"] = {
        "graw_present": {"status": "good", "value": "test.graw"},
        "sidelap_swir_fieldbook": {"status": "warning",
                                   "value": "25.9-29.9 %"},
    }
    report["acquisition_report"] = {
        "mission": {"conditions": "Sunny", "pilot": "Connor"},
        "acquisition": {"n_flight_lines": 14, "n_rogue_lines": 0,
                        "first_line_start_utc": "2026-08-19 02:33:58",
                        "last_line_end_utc": "2026-08-19 02:39:52"},
        "geometry": {"mean_agl_m": 31.6},
        "solar": {"solar_elevation_deg_range": [62.0, 64.6]},
    }
    report["staleness"] = {"gpro_path": "T1_proc/test.gpro",
                           "gpro_mtime_utc": "2026-07-14T07:04:04+00:00"}
    report["config"] = {"path": "reference/thresholds/flightcal_spec.yml",
                        "sha256": "6084207da9c4"}
    report["artifacts"] = ["QC01_FlightCheck/flight_lines.csv"]
    return report


@pytest.fixture
def qc02_report():
    report = _base_report("QC02_SpectralCheck", status="not_evaluated")
    report["checks"] = {
        "nodata_zero_swir": {"status": "warning", "advisory": True,
                             "value": "panel(s) entirely nodata: elm 30"},
        "dhr_bias_swir": {"status": "warning", "advisory": True,
                          "value": "worst |bias| 4.90 pp"},
    }
    report["spectral_report"] = {
        "targets": {
            "QC_ELM_Panels": {
                "SWIR": {
                    "n_bands": 135,
                    "panels": {
                        "11": {"nodata_zero_fraction": 0.875,
                               "all_nodata": False,
                               "median_residual_pct": -6.57},
                        "30": {"nodata_zero_fraction": 1.0,
                               "all_nodata": True},
                    },
                },
            },
        },
    }
    report["dhr_comparison"] = {
        "panel_set": {"gpro_pin": "UF200-24008", "n_elm_targets": 2},
        "references": {},
        "delta_stats": [
            {"panel_name": "QC_ELM_Panels", "EM_Region": "SWIR",
             "Panel_ref": "11", "serial": "24008-11", "region": "full",
             "n_bands": 114, "bias_pct": -1.45, "rmse_pct": 3.10,
             "mae_pct": 2.84, "max_abs_pct": 6.28},
            {"panel_name": "QC_ELM_Panels", "EM_Region": "SWIR",
             "Panel_ref": "11", "serial": "24008-11", "region": "nir",
             "n_bands": 9, "bias_pct": -9.99, "rmse_pct": 9.99,
             "mae_pct": 9.99, "max_abs_pct": 9.99},
        ],
    }
    report["config"] = {
        "table_schema_version": 2.2,
        "spectral_limits": {
            "path": "reference/thresholds/spectral_limits.yml",
            "sha256": "520b1055ea47"}}
    report["artifacts"] = [
        "QC_Spectral_Tables/QC_ELM_spectra_SWIR.parquet",
        "QC02_SpectralCheck/QC_plots/QC_ELM_Panels_SWIR_dhr_overlay.png"]
    return report


@pytest.fixture
def qc03_report():
    report = _base_report("QC03_RasterCheck", status="fail")
    report["checks"] = {
        "header_bin_integrity_vnir": {"status": "good", "value": "v.bin"},
        "zeros_in_footprint_vnir": {"status": "fail", "value": "16.222 %"},
        "dropout_in_roi_vnir": {"status": "good", "value": "0.054 %"},
    }
    report["products"] = {
        "vnir": {
            "file": "v.bin",
            "header_bin_integrity": {"ok": True},
            "shape": {"bands": 172, "height": 13102, "width": 12357,
                      "dtype": "uint16"},
            "zero_zones": {"classifier": "bbox",
                           "zero_edge_band_pct": 0.564,
                           "dropout_in_roi_pct": 0.054,
                           "interior_cc_roi_share_pct": 11.4,
                           "inset": {"line_spacing_source": "qc01"}},
            "cube": {"worst_over_range_band": {
                "band": 135, "wavelength_nm": 871.7,
                "over_range_pct": 0.021}},
            "constant_bands": [],
        },
    }
    report["staleness"] = {"vnir": {"path": "T1_proc/v.bin",
                                    "mtime_utc": "2026-08-21T04:45:41+00:00"}}
    report["config"] = {"path": "reference/thresholds/raster_validity.yml",
                        "sha256": "13519fd231ac"}
    return report


def _strip_timestamps(text):
    return re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\S*", "<utc>", text)


# ===========================================================================
# 1.  Fragment + assembly happy paths
# ===========================================================================
def test_update_writes_fragment_and_report(tmp_path, qc01_report):
    out = qr.update_qc_report(tmp_path, qc01_report)
    assert out == tmp_path / "QC_report.md"
    frag = tmp_path / "QC01_FlightCheck" / "QC01_FlightCheck_section.md"
    assert frag.is_file()
    text = frag.read_text()
    assert "## QC01 — Flight / acquisition" in text
    assert "⚠️ warn" in text                       # section status line
    assert "sidelap_swir_fieldbook" in text
    assert "Flight lines: 14 (0 rogue)" in text
    assert "Mean AGL: 31.6 m" in text
    assert "flight_lines.csv" in text
    assert "flightcal_spec.yml" in text


def test_report_contains_overview_and_stubs(tmp_path, qc01_report):
    qr.write_report(tmp_path, qc01_report)
    qr.update_qc_report(tmp_path, qc01_report)
    text = (tmp_path / "QC_report.md").read_text()
    assert text.startswith("# QC report — USYD_Narrabri/2026_Test/TestSite/"
                           "CALVIS/2026-08-19/1")
    assert "gpro: test.gpro" in text
    # overview row for the script that ran + not-yet-run rows for the rest
    assert "QC01_FlightCheck" in text
    assert text.count("— not yet run") == 3
    # stub sections for the other three, in fixed order
    assert text.index("## QC00 —") < text.index("## QC01 —") \
        < text.index("## QC02 —") < text.index("## QC03 —")
    assert text.count("_Not yet run._") == 3
    assert "triggered by QC01_FlightCheck" in text


def test_all_four_sections_render(tmp_path, qc00_report, qc01_report,
                                  qc02_report, qc03_report):
    for report in (qc00_report, qc01_report, qc02_report, qc03_report):
        qr.write_report(tmp_path, report, derive=False)
        qr.update_qc_report(tmp_path, report)
    text = (tmp_path / "QC_report.md").read_text()
    assert "_Not yet run._" not in text
    # QC00: pair table + gate callout
    assert "QC00 fail — downstream QC void" in text
    assert "QC_GCP_distances" in text
    # QC02: per-target table + DHR full-region rows only
    assert "Max nodata %" in text
    assert "24008-11" in text
    assert "-9.99" not in text                      # sub-region rows excluded
    assert "gpro pin: UF200-24008" in text
    # QC03: matrix + zone-split evidence
    assert "zeros_in_footprint" in text
    assert "❌ 16.222 %" in text
    assert "0.054 % (11.400 % interior-connected)" in text


# ===========================================================================
# 2.  Sibling ownership + idempotence
# ===========================================================================
def test_sibling_fragment_untouched(tmp_path, qc01_report, qc03_report):
    qr.write_report(tmp_path, qc01_report)
    qr.update_qc_report(tmp_path, qc01_report)
    frag = tmp_path / "QC01_FlightCheck" / "QC01_FlightCheck_section.md"
    before = frag.read_bytes()
    qr.write_report(tmp_path, qc03_report, derive=False)
    qr.update_qc_report(tmp_path, qc03_report)
    assert frag.read_bytes() == before


def test_reassembly_idempotent_modulo_timestamps(tmp_path, qc01_report):
    qr.write_report(tmp_path, qc01_report)
    qr.update_qc_report(tmp_path, qc01_report)
    first = _strip_timestamps((tmp_path / "QC_report.md").read_text())
    qr.update_qc_report(tmp_path, qc01_report)
    second = _strip_timestamps((tmp_path / "QC_report.md").read_text())
    assert first == second


# ===========================================================================
# 3.  Stub variants (plan §6)
# ===========================================================================
def test_pre_fragment_stub(tmp_path, qc00_report, qc01_report):
    # contract report on disk but no fragment (pre-fragment-era run)
    qr.write_report(tmp_path, qc00_report, derive=False)
    qr.update_qc_report(tmp_path, qc01_report)
    text = (tmp_path / "QC_report.md").read_text()
    assert "predates section fragments — re-run QC00_GCPCheck" in text
    assert "(status fail)" in text


def test_legacy_stub(tmp_path, qc01_report):
    legacy = tmp_path / "QC_spectra_report.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"status": {"result": "pass"}}))
    qr.update_qc_report(tmp_path, qc01_report)
    text = (tmp_path / "QC_report.md").read_text()
    assert "Legacy report present (QC_spectra_report.json, status pass)" \
        in text
    assert "re-run QC02_SpectralCheck" in text


# ===========================================================================
# 4.  Figures, artifacts, relative paths
# ===========================================================================
def test_qc02_figures_ordered_val2_elm_val4(tmp_path, qc02_report):
    # three targets: 4-panel ELM, 2-panel VAL (headline), 4-panel VAL
    four = {str(p): {"nodata_zero_fraction": 0.0, "all_nodata": False}
            for p in (11, 30, 56, 82)}
    two = {str(p): {"nodata_zero_fraction": 0.0, "all_nodata": False}
           for p in (20, 45)}
    qc02_report["spectral_report"]["targets"] = {
        "QC_ELM_Panels": {"SWIR": {"panels": four}},
        "QC_VAL_Gryfn_2_Panels": {"SWIR": {"panels": two}},
        "QC_VAL_Gryfn4P_Panels": {"SWIR": {"panels": four}},
    }
    plots = "QC02_SpectralCheck/QC_plots"
    qc02_report["artifacts"] = [
        f"{plots}/QC_ELM_Panels_SWIR_dhr_overlay.png",
        f"{plots}/QC_ELM_Panels_SWIR_dhr_delta.png",
        f"{plots}/QC_VAL_Gryfn4P_Panels_SWIR_dhr_overlay.png",
        f"{plots}/QC_VAL_Gryfn4P_Panels_SWIR_dhr_delta.png",
        f"{plots}/QC_VAL_Gryfn_2_Panels_SWIR_dhr_overlay.png",
        f"{plots}/QC_VAL_Gryfn_2_Panels_SWIR_dhr_delta.png",
    ]
    qr.update_qc_report(tmp_path, qc02_report)
    frag = (tmp_path / "QC02_SpectralCheck" /
            "QC02_SpectralCheck_section.md").read_text()
    val2 = frag.index("QC_VAL_Gryfn_2_Panels_SWIR_dhr_overlay.png")
    elm = frag.index("QC_ELM_Panels_SWIR_dhr_overlay.png")
    val4 = frag.index("QC_VAL_Gryfn4P_Panels_SWIR_dhr_overlay.png")
    assert val2 < elm < val4
    # overlay/delta pairing preserved within each target
    assert (frag.index("QC_VAL_Gryfn_2_Panels_SWIR_dhr_delta.png")
            < elm)


def test_missing_and_present_figures(tmp_path, qc00_report):
    png = tmp_path / "QC00_GCPCheck" / "QC_plots" / \
        "QC_GCP_distances_displacements.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(b"png")
    qr.update_qc_report(tmp_path, qc00_report)
    frag = (tmp_path / "QC00_GCPCheck" /
            "QC00_GCPCheck_section.md").read_text()
    assert ("![QC_GCP_distances_displacements]"
            "(QC00_GCPCheck/QC_plots/QC_GCP_distances_displacements.png)"
            in frag)
    # parquet artifact is linked but flagged missing (never written here)
    assert "QC_GCP_distances.parquet (missing)" in frag
    assert "\\" not in frag                          # '/' separators only


# ===========================================================================
# 5.  Never-raise behaviour (plan §3 call-site rules)
# ===========================================================================
def test_unknown_script_warns_and_returns_none(tmp_path):
    report = _base_report("QC99_Bogus")
    with pytest.warns(UserWarning, match="QC_report.md render failed"):
        out = qr.update_qc_report(tmp_path, report)
    assert out is None
    assert not (tmp_path / "QC_report.md").exists()


def test_malformed_report_never_raises(tmp_path):
    with pytest.warns(UserWarning, match="QC_report.md render failed"):
        out = qr.update_qc_report(tmp_path, {"not": "a report"})
    assert out is None
