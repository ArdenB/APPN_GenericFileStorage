"""Shared per-group statistics for pixel/point value distributions.

One implementation of the standard metric set consumed by the DS03
PlotLevel tables (PE00/PE01/PE02, re-exported via
``Code.functions.plot_extracts``) and the DS02 panel-homogeneity
statistics (``Code.functions.spectral_qc``): moments, L-moment ratios,
normality and percentile profiles per group.
"""

__version__ = "1.0.0"
__author__ = "Arden Burrell"

from typing import Dict, List, Sequence

import numpy as np
import pandas as pd
from scipy import stats as sps


# ==================================================================================
def group_value_stats(
        df: pd.DataFrame,
        group_cols: Sequence[str],
        value_col: str = "value",
    ) -> pd.DataFrame:
    """Compute the shared per-group statistic set.

    One row per unique combination of *group_cols* with the standard
    metric columns: ``count``, ``mean``, ``std``, ``var``, ``min``,
    ``max``, ``median``, ``skew``, ``kurtosis``, ``l_cv``, ``l_skew``,
    ``l_kurt``, ``normality_k2``, ``normality_p`` and the short
    percentile set (``p01``, ``p05``, ``p10``, ``p25``, ``p50``,
    ``p75``, ``p90``, ``p95``, ``p99``). All percentiles come from a
    single ``np.quantile`` call per group, so the sort cost is paid
    once.

    Parameters
    ----------
    df : pandas.DataFrame
        Long-format table holding *group_cols* and *value_col*.
    group_cols : sequence of str
        Column(s) to group by (e.g. ``["band"]`` or ``["plot_id"]``).
    value_col : str, optional
        Value column name. Default ``"value"``.

    Returns
    -------
    pandas.DataFrame
        One row per group, sorted by *group_cols*.

    Notes
    -----
    - Non-finite values (NaN, ±Inf) are dropped per group before any
      statistic is computed; a group with no finite values is omitted
      from the output.
    - ``std``/``var`` use ``ddof=1``; single-value groups report 0.0
      (matching the historical PE01/PE02 convention).
    - ``skew``/``kurtosis`` are bias-corrected (pandas-compatible;
      kurtosis is Fisher excess) and NaN for groups too small
      (n < 3 / n < 4) or with zero variance.
    - ``l_cv``/``l_skew``/``l_kurt`` are L-moment ratios (tau, tau3,
      tau4 — unbiased direct estimators, validated against lmoments3):
      a robust, bounded (|t3|,|t4| < 1) distribution-shape fingerprint.
      Strong bimodality (e.g. half-soil/half-canopy plots, shadowed
      panel corners) shows up as low ``l_kurt``. NaN when n < 4 or
      variance is zero; ``l_cv`` is only meaningful for positive-valued
      data and is NaN when the mean is 0.
    - ``normality_k2``/``normality_p`` are D'Agostino-Pearson K²
      (``scipy.stats.normaltest``), NaN when n < 20 (the scipy validity
      floor for the kurtosis test). With thousands of pixels per group
      the p-value rejects for trivial deviations — prefer the statistic
      (and skew/kurtosis) as effect sizes.
    - Vectorised across groups (one lexsort + ``np.add.reduceat`` pass
      instead of per-group scipy calls; ~10x on plot x band tables).
      The per-group reference implementation (:func:`_value_stats`) is
      kept as the test oracle in ``tests/test_group_stats.py``.
    """
    group_cols = list(group_cols)
    gb = df.groupby(group_cols, sort=True, observed=True)
    codes = gb.ngroup().to_numpy(np.int64)
    vals = df[value_col].to_numpy(dtype=np.float64)

    # +++++ Drop non-finite values (and rows with NaN group keys) +++++
    keep = np.isfinite(vals) & (codes >= 0)
    codes, vals = codes[keep], vals[keep]
    if codes.size == 0:
        return pd.DataFrame()

    # +++++ One sort: by group, then by value within group +++++
    order = np.lexsort((vals, codes))
    codes, x = codes[order], vals[order]
    starts = np.flatnonzero(np.r_[True, codes[1:] != codes[:-1]])
    counts = np.diff(np.r_[starts, codes.size])
    gcodes = codes[starts]
    n = counts.astype(np.float64)

    # +++++ Moments (deviations about the group mean, one reduceat each) +++++
    mean = np.add.reduceat(x, starts) / n
    d = x - np.repeat(mean, counts)
    d2 = d * d
    ss2 = np.add.reduceat(d2, starts)
    m2 = ss2 / n
    m3 = np.add.reduceat(d2 * d, starts) / n
    m4 = np.add.reduceat(d2 * d2, starts) / n
    var = np.where(counts > 1, ss2 / np.maximum(n - 1.0, 1.0), 0.0)
    std = np.sqrt(var)

    # +++++ Quantiles from the sorted values (np.quantile 'linear' rule) +++++
    q = np.array([0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99])
    pos = starts[:, None] + (counts[:, None] - 1) * q[None, :]
    lo = np.floor(pos).astype(np.int64)
    hi = np.ceil(pos).astype(np.int64)
    frac = pos - lo
    qvals = x[lo] * (1.0 - frac) + x[hi] * frac

    with np.errstate(divide="ignore", invalid="ignore"):
        # +++++ Bias-corrected skew / excess kurtosis (scipy formulas) +++++
        g1 = m3 / m2 ** 1.5
        skew = np.where((counts >= 3) & (std > 0),
                        np.sqrt((n - 1.0) * n) / (n - 2.0) * g1, np.nan)
        kurt = np.where(
            (counts >= 4) & (std > 0),
            1.0 / (n - 2.0) / (n - 3.0)
            * ((n ** 2 - 1.0) * m4 / m2 ** 2 - 3.0 * (n - 1.0) ** 2),
            np.nan)

        # +++++ L-moment ratios from within-group ranks (Hosking 1990) +++++
        r = (np.arange(x.size) - np.repeat(starts, counts)).astype(np.float64)
        w1 = r * x
        w2 = w1 * (r - 1.0)
        w3 = w2 * (r - 2.0)
        b0 = mean
        b1 = np.add.reduceat(w1, starts) / (n * (n - 1.0))
        b2 = np.add.reduceat(w2, starts) / (n * (n - 1.0) * (n - 2.0))
        b3 = np.add.reduceat(w3, starts) \
            / (n * (n - 1.0) * (n - 2.0) * (n - 3.0))
        l2 = 2.0 * b1 - b0
        l3 = 6.0 * b2 - 6.0 * b1 + b0
        l4 = 20.0 * b3 - 30.0 * b2 + 12.0 * b1 - b0
        ok4 = counts >= 4
        l_cv = np.where(ok4 & (b0 != 0), l2 / b0, np.nan)
        l_skew = np.where(ok4 & (l2 > 0), l3 / l2, np.nan)
        l_kurt = np.where(ok4 & (l2 > 0), l4 / l2, np.nan)

        # +++++ D'Agostino-Pearson K² (scipy normaltest formulas) +++++
        zs = _skewtest_z(g1, n)
        zk = _kurtosistest_z(m4 / m2 ** 2, n)
        k2 = np.where((counts >= 20) & (std > 0), zs ** 2 + zk ** 2, np.nan)
    normality_p = np.where(np.isnan(k2), np.nan, sps.chi2.sf(k2, 2))

    # +++++ Assemble (column order matches the reference implementation) +++++
    keys_df = gb.size().index.to_frame(index=False).iloc[gcodes]
    out = keys_df.reset_index(drop=True)
    out["count"] = counts
    out["mean"] = mean
    out["std"] = std
    out["var"] = var
    out["min"] = x[starts]
    out["max"] = x[starts + counts - 1]
    out["median"] = qvals[:, 4]
    out["skew"] = skew
    out["kurtosis"] = kurt
    out["l_cv"] = l_cv
    out["l_skew"] = l_skew
    out["l_kurt"] = l_kurt
    out["normality_k2"] = k2
    out["normality_p"] = normality_p
    for i, pct in enumerate([1, 5, 10, 25, 50, 75, 90, 95, 99]):
        out[f"p{pct:02d}"] = qvals[:, i]
    return out


# ==================================================================================
def _skewtest_z(g1: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Vectorised ``scipy.stats.skewtest`` Z statistic.

    Parameters
    ----------
    g1 : numpy.ndarray
        Biased skewness (m3 / m2^1.5) per group.
    n : numpy.ndarray
        Group sizes (float). Only valid for n >= 8; callers gate on
        n >= 20 so no small-sample guard is needed here.

    Returns
    -------
    numpy.ndarray
        Z score per group (D'Agostino 1970 transform).
    """
    y = g1 * np.sqrt(((n + 1.0) * (n + 3.0)) / (6.0 * (n - 2.0)))
    beta2 = (3.0 * (n ** 2 + 27.0 * n - 70.0) * (n + 1.0) * (n + 3.0)
             / ((n - 2.0) * (n + 5.0) * (n + 7.0) * (n + 9.0)))
    w2 = -1.0 + np.sqrt(2.0 * (beta2 - 1.0))
    delta = 1.0 / np.sqrt(0.5 * np.log(w2))
    alpha = np.sqrt(2.0 / (w2 - 1.0))
    y = np.where(y == 0, 1.0, y)
    return delta * np.log(y / alpha + np.sqrt((y / alpha) ** 2 + 1.0))


# ==================================================================================
def _kurtosistest_z(b2: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Vectorised ``scipy.stats.kurtosistest`` Z statistic.

    Parameters
    ----------
    b2 : numpy.ndarray
        Biased Pearson kurtosis (m4 / m2^2) per group.
    n : numpy.ndarray
        Group sizes (float). Only valid for n >= 5; callers gate on
        n >= 20 so no small-sample guard is needed here.

    Returns
    -------
    numpy.ndarray
        Z score per group (Anscombe & Glynn 1983 transform).
    """
    e = 3.0 * (n - 1.0) / (n + 1.0)
    varb2 = (24.0 * n * (n - 2.0) * (n - 3.0)
             / ((n + 1.0) ** 2 * (n + 3.0) * (n + 5.0)))
    xk = (b2 - e) / np.sqrt(varb2)
    sqrtbeta1 = (6.0 * (n * n - 5.0 * n + 2.0) / ((n + 7.0) * (n + 9.0))
                 * np.sqrt(6.0 * (n + 3.0) * (n + 5.0)
                           / (n * (n - 2.0) * (n - 3.0))))
    a = 6.0 + 8.0 / sqrtbeta1 * (2.0 / sqrtbeta1
                                 + np.sqrt(1.0 + 4.0 / sqrtbeta1 ** 2))
    term1 = 1.0 - 2.0 / (9.0 * a)
    denom = 1.0 + xk * np.sqrt(2.0 / (a - 4.0))
    term2 = np.sign(denom) * np.where(
        denom == 0, np.nan, ((1.0 - 2.0 / a) / np.abs(denom)) ** (1.0 / 3.0))
    return (term1 - term2) / np.sqrt(2.0 / (9.0 * a))


# ==================================================================================
def _value_stats(vals: np.ndarray) -> Dict[str, float]:
    """Compute the standard metric set for one group's values.

    Parameters
    ----------
    vals : numpy.ndarray
        Finite float values of one group (non-finite values are
        filtered by the group helpers before this is called).

    Returns
    -------
    dict of str to float
        Metric name to value (see :func:`group_value_stats`).
    """
    n = int(vals.size)
    q = np.quantile(vals, [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99])
    std = float(np.std(vals, ddof=1)) if n > 1 else 0.0
    out: Dict[str, float] = {
        "count": n,
        "mean": float(np.mean(vals)),
        "std": std,
        "var": float(np.var(vals, ddof=1)) if n > 1 else 0.0,
        "min": float(np.min(vals)),
        "max": float(np.max(vals)),
        "median": float(q[4]),
        "skew": (float(sps.skew(vals, bias=False))
                 if n >= 3 and std > 0 else np.nan),
        "kurtosis": (float(sps.kurtosis(vals, fisher=True, bias=False))
                     if n >= 4 and std > 0 else np.nan),
    }
    out.update(_lmoment_ratios(vals))
    if n >= 20 and std > 0:
        k2, p = sps.normaltest(vals)
        out["normality_k2"], out["normality_p"] = float(k2), float(p)
    else:
        out["normality_k2"], out["normality_p"] = np.nan, np.nan
    for pct, val in zip([1, 5, 10, 25, 50, 75, 90, 95, 99], q):
        out[f"p{pct:02d}"] = float(val)
    return out


# ==================================================================================
def _lmoment_ratios(vals: np.ndarray) -> Dict[str, float]:
    """Compute the first L-moment ratios of one group's values.

    Direct unbiased estimator from sorted order statistics (Hosking
    1990 probability-weighted moments); benchmarked ~0.2 ms per 11k-px
    group and numerically identical to ``lmoments3.lmom_ratios``.

    Parameters
    ----------
    vals : numpy.ndarray
        Finite float values of one group.

    Returns
    -------
    dict of str to float
        ``l_cv`` (tau = l2/l1), ``l_skew`` (tau3 = l3/l2) and ``l_kurt``
        (tau4 = l4/l2). All NaN when n < 4; ratios with a zero
        denominator are NaN.
    """
    n = vals.size
    if n < 4:
        return {"l_cv": np.nan, "l_skew": np.nan, "l_kurt": np.nan}
    x = np.sort(vals)
    i = np.arange(1, n + 1, dtype=np.float64)
    b0 = x.mean()
    b1 = np.sum((i - 1) * x) / (n * (n - 1))
    b2 = np.sum((i - 1) * (i - 2) * x) / (n * (n - 1) * (n - 2))
    b3 = np.sum((i - 1) * (i - 2) * (i - 3) * x) / (n * (n - 1) * (n - 2) * (n - 3))
    l1 = b0
    l2 = 2.0 * b1 - b0
    l3 = 6.0 * b2 - 6.0 * b1 + b0
    l4 = 20.0 * b3 - 30.0 * b2 + 12.0 * b1 - b0
    return {
        "l_cv": float(l2 / l1) if l1 != 0 else np.nan,
        "l_skew": float(l3 / l2) if l2 > 0 else np.nan,
        "l_kurt": float(l4 / l2) if l2 > 0 else np.nan,
    }


# ==================================================================================
def group_value_percentiles(
        df: pd.DataFrame,
        group_cols: Sequence[str],
        value_col: str = "value",
    ) -> pd.DataFrame:
    """Compute the full 0-100 percentile profile per group (long format).

    Parameters
    ----------
    df : pandas.DataFrame
        Long-format table holding *group_cols* and *value_col*.
    group_cols : sequence of str
        Column(s) to group by.
    value_col : str, optional
        Value column name. Default ``"value"``.

    Returns
    -------
    pandas.DataFrame
        101 rows per group: *group_cols* + ``percentile`` (int16,
        0-100 where 0 = min and 100 = max) + ``value`` (float32).
        Non-finite values are dropped per group first; a group with no
        finite values is omitted.
    """
    group_cols = list(group_cols)
    quantiles = np.linspace(0.0, 1.0, 101)
    pct_levels = np.arange(101, dtype=np.int16)
    frames: List[pd.DataFrame] = []
    for keys, vals in df.groupby(group_cols, sort=True, observed=True)[value_col]:
        if not isinstance(keys, tuple):
            keys = (keys,)
        arr = vals.to_numpy(dtype=np.float64)
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            continue
        frame = pd.DataFrame({
            "percentile": pct_levels,
            "value": np.quantile(arr, quantiles).astype(np.float32),
        })
        for col, key in zip(reversed(group_cols), reversed(keys)):
            frame.insert(0, col, key)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)
