"""Unit tests for spectral_qc.panel_homogeneity (QC02 homogeneity block)."""

import numpy as np
import pandas as pd
import pytest

from Code.functions.spectral_qc import panel_homogeneity


# ==================================================================================
def _panel_frame(pixels_by_band, panel_ref="56", wl_start=420.0):
    """Build a spectra-table-shaped frame from {band: pixel array}."""
    recs = []
    for band, vals in pixels_by_band.items():
        for v in vals:
            recs.append({"Panel_ref": panel_ref, "band": band,
                         "wavelength": wl_start + 5.0 * band,
                         "refl_pct": float(v)})
    return pd.DataFrame(recs)


def _thresholds():
    """Test-local thresholds (values arbitrary, not calibration output)."""
    return {"abs_skew_warn_above": 1.0, "l_kurt_warn_below": 0.0,
            "mean_median_divergence_warn_pp": 0.5,
            "fraction_bands_warn_above": 0.25}


# ==================================================================================
def test_clean_panel_flags_clean():
    rng = np.random.default_rng(42)
    df = _panel_frame({b: 56.0 + rng.normal(0.0, 0.4, 400) for b in range(10)})
    out = panel_homogeneity(df, thresholds=_thresholds())
    block = out["56"]
    assert block["flag"] == "clean"
    assert block["n_bands"] == 10
    assert block["n_px"] == 400
    assert block["median_abs_skew"] < 0.5
    # unimodal normal: l_kurt ~ +0.123
    assert block["median_l_kurt"] > 0.05
    assert block["mean_median_divergence_pct"] < 0.5


def test_bimodal_shadow_flags_suspect():
    # 50/50 mixture (shadowed corner): l_kurt depressed below unimodal
    rng = np.random.default_rng(7)
    df = _panel_frame({
        b: np.concatenate([rng.normal(56.0, 0.4, 200),
                           rng.normal(30.0, 0.4, 200)])
        for b in range(10)})
    out = panel_homogeneity(df, thresholds=_thresholds())
    block = out["56"]
    assert block["median_l_kurt"] < 0.0  # ~-0.185 for a 50/50 mixture
    assert block["flag"] == "suspect"
    assert block["fraction_bands_flagged"] == pytest.approx(1.0)


def test_hotspot_right_tail_flags_suspect():
    # specular hotspot: heavy right tail inflates skew + mean-median gap
    rng = np.random.default_rng(3)
    df = _panel_frame({
        b: np.concatenate([rng.normal(56.0, 0.3, 380),
                           rng.normal(75.0, 2.0, 20)])
        for b in range(10)})
    out = panel_homogeneity(df, thresholds=_thresholds())
    block = out["56"]
    assert block["median_abs_skew"] > 1.0
    assert block["flag"] == "suspect"


def test_no_thresholds_computes_metrics_only():
    rng = np.random.default_rng(0)
    df = _panel_frame({b: rng.normal(56.0, 0.4, 100) for b in range(4)})
    block = panel_homogeneity(df)["56"]
    assert block["flag"] is None
    assert block["n_bands_flagged"] is None
    assert np.isfinite(block["median_l_kurt"])


def test_bad_bands_excluded():
    rng = np.random.default_rng(1)
    clean = {b: rng.normal(56.0, 0.4, 100) for b in range(8)}
    # bands 0-3 (420-435 nm) carry a gross bimodal artefact
    for b in range(4):
        clean[b] = np.concatenate([rng.normal(56.0, 0.4, 50),
                                   rng.normal(20.0, 0.4, 50)])
    df = _panel_frame(clean)
    out = panel_homogeneity(df, bad_wavelengths=[(415.0, 436.0)],
                            thresholds=_thresholds())
    block = out["56"]
    assert block["n_bands"] == 4
    assert block["flag"] == "clean"


def test_multiple_panels_keyed_by_code():
    rng = np.random.default_rng(5)
    df = pd.concat([
        _panel_frame({b: rng.normal(56.0, 0.4, 50) for b in range(4)},
                     panel_ref="56"),
        _panel_frame({b: rng.normal(82.0, 0.4, 50) for b in range(4)},
                     panel_ref="82.0"),  # float-string refs normalise
    ], ignore_index=True)
    out = panel_homogeneity(df, thresholds=_thresholds())
    assert set(out) == {"56", "82"}


def test_missing_column_raises():
    df = _panel_frame({0: [1.0, 2.0]}).drop(columns=["refl_pct"])
    with pytest.raises(KeyError, match="refl_pct"):
        panel_homogeneity(df)
