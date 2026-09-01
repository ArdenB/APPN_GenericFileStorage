"""Tests for the vectorised group_value_stats against the per-group oracle.

The vectorised implementation in ``group_stats.group_value_stats`` must be
numerically indistinguishable from the retained per-group reference
(:func:`group_stats._value_stats` + scipy) that produced all PlotLevel
tables before v1.1.
"""

import numpy as np
import pandas as pd
import pytest
from scipy import stats as sps

from Code.functions.core_functions import group_stats as gs


# ==================================================================================
def _oracle(df: pd.DataFrame, group_cols, value_col: str = "value") -> pd.DataFrame:
    """Reference implementation: the original per-group loop."""
    rows = []
    for keys, vals in df.groupby(list(group_cols), sort=True, observed=True)[value_col]:
        if not isinstance(keys, tuple):
            keys = (keys,)
        arr = vals.to_numpy(dtype=np.float64)
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            continue
        rows.append({**dict(zip(group_cols, keys)), **gs._value_stats(arr)})
    return pd.DataFrame(rows)


def _assert_matches(new: pd.DataFrame, ref: pd.DataFrame, group_cols) -> None:
    """Assert the vectorised output matches the oracle column by column."""
    assert list(new.columns) == list(ref.columns)
    assert len(new) == len(ref)
    new = new.sort_values(list(group_cols)).reset_index(drop=True)
    ref = ref.sort_values(list(group_cols)).reset_index(drop=True)
    for col in ref.columns:
        if col in group_cols:
            assert (new[col].to_numpy() == ref[col].to_numpy()).all(), col
        else:
            np.testing.assert_allclose(
                new[col].to_numpy(dtype=np.float64),
                ref[col].to_numpy(dtype=np.float64),
                rtol=1e-10, atol=1e-12, equal_nan=True, err_msg=col)


# ==================================================================================
def test_matches_oracle_large_groups():
    rng = np.random.default_rng(42)
    bands = np.repeat(np.arange(25), 3000)
    vals = rng.gamma(2.0, 150.0, bands.size) + rng.normal(0, 5, bands.size)
    df = pd.DataFrame({"band": bands, "value": vals})
    _assert_matches(gs.group_value_stats(df, ["band"]), _oracle(df, ["band"]), ["band"])


def test_matches_oracle_small_and_degenerate_groups():
    # n=1 (std/var=0), n=2, n=3 (skew ok, kurt NaN), n=5, n=19 (no normality),
    # n=25 zero-variance, n=30 normal
    parts = {
        0: [5.0],
        1: [1.0, 2.0],
        2: [1.0, 2.0, 10.0],
        3: [1.0, 2.0, 3.0, 4.0, 100.0],
        4: list(np.random.default_rng(1).normal(10, 2, 19)),
        5: [7.0] * 25,
        6: list(np.random.default_rng(2).lognormal(1, 0.5, 30)),
    }
    df = pd.DataFrame(
        [(k, v) for k, vs in parts.items() for v in vs], columns=["band", "value"])
    _assert_matches(gs.group_value_stats(df, ["band"]), _oracle(df, ["band"]), ["band"])


def test_nonfinite_values_dropped_and_empty_groups_omitted():
    df = pd.DataFrame({
        "band": [0] * 4 + [1] * 3 + [2] * 2,
        "value": [1.0, np.nan, 2.0, np.inf, np.nan, np.nan, np.nan, 3.0, -np.inf],
    })
    out = gs.group_value_stats(df, ["band"])
    assert list(out["band"]) == [0, 2]  # band 1 all-NaN -> omitted
    assert list(out["count"]) == [2, 1]
    _assert_matches(out, _oracle(df, ["band"]), ["band"])


def test_multiple_group_cols_and_value_col():
    rng = np.random.default_rng(7)
    df = pd.DataFrame({
        "plot_id": np.repeat(["a", "b"], 200),
        "band": np.tile(np.repeat([1, 2], 100), 2),
        "refl": rng.normal(50, 10, 400),
    })
    new = gs.group_value_stats(df, ["plot_id", "band"], value_col="refl")
    ref = _oracle(df, ["plot_id", "band"], value_col="refl")
    _assert_matches(new, ref, ["plot_id", "band"])


def test_matches_scipy_directly():
    rng = np.random.default_rng(3)
    arr = rng.gamma(3.0, 20.0, 5000)
    out = gs.group_value_stats(
        pd.DataFrame({"g": 0, "value": arr}), ["g"]).iloc[0]
    assert out["skew"] == pytest.approx(sps.skew(arr, bias=False), rel=1e-12)
    assert out["kurtosis"] == pytest.approx(
        sps.kurtosis(arr, fisher=True, bias=False), rel=1e-12)
    k2, p = sps.normaltest(arr)
    assert out["normality_k2"] == pytest.approx(k2, rel=1e-10)
    assert out["normality_p"] == pytest.approx(p, rel=1e-8)
    q = np.quantile(arr, [0.01, 0.25, 0.5, 0.75, 0.99])
    for pct, val in zip(["p01", "p25", "p50", "p75", "p99"], q):
        assert out[pct] == pytest.approx(val, rel=1e-12)


def test_all_nonfinite_returns_empty():
    df = pd.DataFrame({"band": [0, 1], "value": [np.nan, np.inf]})
    assert gs.group_value_stats(df, ["band"]).empty
