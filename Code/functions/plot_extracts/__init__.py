"""Shared helpers for the DS03 ``PlotExtracts`` output tree.

The DS03 extraction scripts (PE00/PE01/PE02) write into a common
per-run folder layout under ``<run>/T1_proc/PlotExtracts/``:

- ``PixelLevel/`` — heavy point/pixel-level parquet *datasets*
  (directories of per-plot or per-chunk part files) plus their YAML
  provenance sidecars. A dataset's sidecar is written **last**, after
  all part files, so it doubles as the completion marker and the mtime
  anchor for ``outputs_up_to_date`` caching.
- ``PlotLevel/`` — light, analyst-facing per-plot metric tables
  (single parquet files + sidecars).
- ``Reports/`` — markdown QC reports and their ``PE_figures/``.

Part files are written atomically (``.tmp`` then ``os.replace``) so a
crash never leaves a corrupt part that looks fresh to the caching
checks.

The per-group statistics helpers (:func:`group_value_stats`,
:func:`group_value_percentiles`) implement the shared PlotLevel metric
set so PE00/PE01/PE02 all emit identical column schemas.
"""

__version__ = "1.1.0"
__author__ = "Arden Burrell"

import os
import pathlib
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from scipy import stats as sps


# ==================================================================================
def plotextract_dirs(t1_dir: pathlib.Path) -> Dict[str, pathlib.Path]:
    """Return the canonical PlotExtracts sub-folder paths for one run.

    Parameters
    ----------
    t1_dir : pathlib.Path
        The run's ``T1_proc`` directory.

    Returns
    -------
    dict of str to pathlib.Path
        Keys ``extracts`` (``PlotExtracts/``), ``pixel``
        (``PixelLevel/``), ``plot`` (``PlotLevel/``) and ``reports``
        (``Reports/``). Paths are returned, not created.
    """
    extracts = t1_dir / "PlotExtracts"
    return {
        "extracts": extracts,
        "pixel": extracts / "PixelLevel",
        "plot": extracts / "PlotLevel",
        "reports": extracts / "Reports",
    }


# ==================================================================================
def write_dataset_part(
        df: pd.DataFrame,
        part_file: pathlib.Path,
        compression: str = "zstd",
    ) -> None:
    """Atomically write one part file of a parquet dataset directory.

    Object-dtype columns (``plot_id``, ``index``, run-metadata strings)
    are dictionary-encoded, and the write goes to a ``.tmp`` sibling
    first then ``os.replace``-d into place, so readers and the mtime
    caching never see a partial part.

    Parameters
    ----------
    df : pandas.DataFrame
        Rows for this part (typically one plot or one scan chunk).
    part_file : pathlib.Path
        Destination ``.parquet`` path inside the dataset directory
        (parent created if missing).
    compression : str, optional
        Parquet compression codec. Default ``"zstd"``.

    Returns
    -------
    None
    """
    df = df.copy(deep=False)
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype("category")
    table = pa.Table.from_pandas(df, preserve_index=False)
    part_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = part_file.with_suffix(part_file.suffix + ".tmp")
    pq.write_table(table, tmp, compression=compression)
    os.replace(tmp, part_file)


# ==================================================================================
def group_value_stats(
        df: pd.DataFrame,
        group_cols: Sequence[str],
        value_col: str = "value",
    ) -> pd.DataFrame:
    """Compute the shared PlotLevel statistic set per group.

    One row per unique combination of *group_cols* with the DS03
    standard metric columns: ``count``, ``mean``, ``std``, ``var``,
    ``min``, ``max``, ``median``, ``skew``, ``kurtosis``, ``l_cv``,
    ``l_skew``, ``l_kurt``, ``normality_k2``, ``normality_p`` and the
    short percentile set (``p01``, ``p05``, ``p10``, ``p25``, ``p50``,
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
      Strong bimodality (e.g. half-soil/half-canopy plots, lodged
      crops) shows up as low ``l_kurt``. NaN when n < 4 or variance is
      zero; ``l_cv`` is only meaningful for positive-valued data and is
      NaN when the mean is 0.
    - ``normality_k2``/``normality_p`` are D'Agostino-Pearson K²
      (``scipy.stats.normaltest``), NaN when n < 20 (the scipy validity
      floor for the kurtosis test). With thousands of pixels per plot
      the p-value rejects for trivial deviations — prefer the statistic
      (and skew/kurtosis) as effect sizes.
    """
    group_cols = list(group_cols)
    rows: List[Dict[str, Any]] = []
    for keys, vals in df.groupby(group_cols, sort=True, observed=True)[value_col]:
        if not isinstance(keys, tuple):
            keys = (keys,)
        arr = vals.to_numpy(dtype=np.float64)
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            continue
        rows.append({**dict(zip(group_cols, keys)), **_value_stats(arr)})
    return pd.DataFrame(rows)


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


# ==================================================================================
def dataset_parts(dataset_dir: pathlib.Path) -> List[pathlib.Path]:
    """List the part files of a parquet dataset directory.

    Parameters
    ----------
    dataset_dir : pathlib.Path
        The dataset directory.

    Returns
    -------
    list of pathlib.Path
        Sorted ``*.parquet`` part paths; empty when the directory does
        not exist. ``.tmp`` leftovers from interrupted writes are
        excluded by the suffix match.
    """
    if not dataset_dir.is_dir():
        return []
    return sorted(dataset_dir.glob("*.parquet"))


# ==================================================================================
def dataset_row_count(dataset_dir: pathlib.Path) -> Optional[int]:
    """Sum the row counts of a dataset directory from the part footers.

    Parameters
    ----------
    dataset_dir : pathlib.Path
        The dataset directory.

    Returns
    -------
    int or None
        Total rows across all parts (footer metadata only — no data is
        read), or None when the directory is empty or a footer cannot
        be read.
    """
    parts = dataset_parts(dataset_dir)
    if not parts:
        return None
    try:
        return sum(pq.read_metadata(p).num_rows for p in parts)
    except (OSError, pa.ArrowInvalid):
        return None
