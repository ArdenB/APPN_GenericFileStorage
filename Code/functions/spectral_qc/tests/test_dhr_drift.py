"""Unit tests for spectral_qc.within_day_drift (QA02 drift metrics)."""

import numpy as np
import pandas as pd
import pytest

from Code.functions.spectral_qc import within_day_drift


# ==================================================================================
def _stats_frame(rows, project="2026_APEx"):
    """Build a delta-stats-shaped frame from (run, panel, bias[, solar]) rows."""
    recs = []
    for row in rows:
        run, ref, bias = row[:3]
        rec = {
            "node": "USYD_Narrabri", "project": project,
            "site": "I.A.Watson", "sensor": "GOBI",
            "date": "2026-08-05", "run_number": run,
            "panel_name": "QC_VAL_Gryfn4P_Panels", "EM_Region": "VNIR",
            "Panel_ref": ref, "serial": f"UF200-24009-{ref}",
            "region": "full", "bias_pct": bias,
        }
        if len(row) > 3:
            rec["solar_elevation_deg"] = row[3]
        recs.append(rec)
    return pd.DataFrame(recs)


# ==================================================================================
def test_monotonic_drift_detected():
    # Narrabri-class walk: monotonic decrease across nine runs
    biases = np.linspace(9.0, -13.0, 9)
    df = _stats_frame([(i + 1, "82", b) for i, b in enumerate(biases)])
    out = within_day_drift(df)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["n_runs"] == 9
    assert row["range_pp"] == pytest.approx(22.0)
    assert row["bias_first_pct"] == pytest.approx(9.0)
    assert row["bias_last_pct"] == pytest.approx(-13.0)
    assert row["spearman_run_rho"] == pytest.approx(-1.0)


def test_stable_group_small_range():
    df = _stats_frame([(1, "56", 0.4), (2, "56", 0.5), (3, "56", 0.3)])
    out = within_day_drift(df)
    assert len(out) == 1
    assert out.iloc[0]["range_pp"] == pytest.approx(0.2)


def test_min_runs_excludes_short_groups():
    df = _stats_frame([(1, "82", 1.0), (2, "82", 5.0)])
    assert within_day_drift(df, min_runs=3).empty


def test_groups_are_independent():
    df = _stats_frame(
        [(r, "82", b) for r, b in [(1, 0.0), (2, 4.0), (3, 8.0)]]
        + [(r, "30", 0.1) for r in (1, 2, 3)])
    out = within_day_drift(df).set_index("Panel_ref")
    assert out.loc["82", "range_pp"] == pytest.approx(8.0)
    assert out.loc["30", "range_pp"] == pytest.approx(0.0)


def test_projects_never_merge():
    # same node/site/sensor/date/run identifiers under two projects
    # (I.A.Watson class): two flat projects must not read as one drift
    df = pd.concat([
        _stats_frame([(r, "82", 2.0) for r in (1, 2, 3)],
                     project="2026_APEx"),
        _stats_frame([(r, "82", -8.0) for r in (1, 2, 3)],
                     project="2026_TomsCoverCrop"),
    ], ignore_index=True)
    out = within_day_drift(df)
    assert len(out) == 2
    assert (out["range_pp"] == 0.0).all()


def test_solar_correlation_used_when_present():
    # bias tracks solar elevation, not run order
    rows = [(1, "82", 2.0, 30.0), (2, "82", 6.0, 55.0),
            (3, "82", 4.0, 45.0), (4, "82", 1.0, 25.0)]
    out = within_day_drift(_stats_frame(rows))
    assert out.iloc[0]["spearman_solar_rho"] == pytest.approx(1.0)


def test_solar_rho_nan_when_column_missing():
    df = _stats_frame([(1, "82", 1.0), (2, "82", 2.0), (3, "82", 3.0)])
    assert np.isnan(within_day_drift(df).iloc[0]["spearman_solar_rho"])


def test_non_full_regions_ignored():
    df = _stats_frame([(r, "82", 1.0) for r in (1, 2, 3)])
    df.loc[df["run_number"] == 3, "region"] = "nir"
    assert within_day_drift(df).empty  # only 2 'full' runs remain


def test_missing_column_raises():
    df = _stats_frame([(1, "82", 1.0)]).drop(columns=["serial"])
    with pytest.raises(KeyError, match="serial"):
        within_day_drift(df)
