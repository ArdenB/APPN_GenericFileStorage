"""Within-day DHR panel-bias drift metrics (QA02 cross-run check).

Implements the pipeline-plan Phase 3 drift statistic: for every
day x target x panel group with enough runs, how far the full-region
DHR bias walked across the runs and how monotonic that walk was
(Narrabri 20260805: panel 82 walked +9 -> -13 % over runs 01->09,
brightness-dependent). The solar-elevation correlation uses QC01's
per-run solar geometry where the caller has attached it.
"""

__version__ = "1.0.0"
__author__ = "Arden Burrell"

from typing import List, Dict, Any

import numpy as np
import pandas as pd
from scipy import stats as sps


# ==================================================================================
def within_day_drift(
        stats: pd.DataFrame,
        min_runs: int = 3,
    ) -> pd.DataFrame:
    """Compute within-day drift metrics of the full-region DHR bias.

    Groups the concatenated QC02 delta-stats rows by day x target x
    panel and, for groups spanning at least *min_runs* runs, measures
    the bias walk across the day: total range, first/last values, and
    Spearman rank correlations of bias against run order and (when a
    ``solar_elevation_deg`` column is present) against solar elevation.

    Parameters
    ----------
    stats : pd.DataFrame
        Concatenated per-run DHR delta statistics. Required columns:
        ``node, project, site, sensor, date, run_number, panel_name,
        EM_Region, Panel_ref, serial, bias_pct`` and, when present,
        ``region`` (filtered to ``"full"`` rows) and
        ``solar_elevation_deg``.
    min_runs : int, optional
        Minimum distinct runs a group needs to be evaluated. Default 3.

    Returns
    -------
    pd.DataFrame
        One row per evaluated group: the group keys plus ``serial,
        n_runs, bias_first_pct, bias_last_pct, bias_min_pct,
        bias_max_pct, range_pp, spearman_run_rho, spearman_solar_rho``.
        Empty when no group reaches *min_runs*.

    Raises
    ------
    KeyError
        If a required column is missing.
    """
    required = ["node", "project", "site", "sensor", "date", "run_number",
                "panel_name", "EM_Region", "Panel_ref", "serial", "bias_pct"]
    missing = [c for c in required if c not in stats.columns]
    if missing:
        raise KeyError(f"within_day_drift: missing columns {missing}")
    df = stats
    if "region" in df.columns:
        df = df[df["region"] == "full"]

    # project is a group key: same-named sites recur across projects on
    # one node (e.g. I.A.Watson), so date/run identifiers alone collide.
    keys = ["node", "project", "site", "sensor", "date", "panel_name",
            "EM_Region", "Panel_ref"]
    rows: List[Dict[str, Any]] = []
    for group, gdf in df.groupby(keys):
        gdf = gdf.sort_values("run_number")
        if gdf["run_number"].nunique() < min_runs:
            continue
        bias = gdf["bias_pct"].to_numpy(dtype=float)
        row: Dict[str, Any] = dict(zip(keys, group))
        row.update({
            "serial": str(gdf["serial"].iloc[0]),
            "n_runs": int(gdf["run_number"].nunique()),
            "bias_first_pct": float(bias[0]),
            "bias_last_pct": float(bias[-1]),
            "bias_min_pct": float(bias.min()),
            "bias_max_pct": float(bias.max()),
            "range_pp": float(bias.max() - bias.min()),
            "spearman_run_rho": _spearman(
                gdf["run_number"].to_numpy(dtype=float), bias),
            "spearman_solar_rho": _spearman(
                gdf["solar_elevation_deg"].to_numpy(dtype=float), bias)
                if "solar_elevation_deg" in gdf.columns else np.nan,
        })
        rows.append(row)
    return pd.DataFrame(rows)


# ==================================================================================
def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman rho of *y* against *x*, NaN-safe.

    Parameters
    ----------
    x, y : np.ndarray
        Paired samples; NaN pairs are dropped.

    Returns
    -------
    float
        Spearman rank correlation, or NaN when fewer than three
        complete pairs remain or either side is constant.
    """
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3 or len(np.unique(x[ok])) < 2 or len(np.unique(y[ok])) < 2:
        return float("nan")
    return float(sps.spearmanr(x[ok], y[ok]).statistic)
