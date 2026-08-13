"""Shared statistics for the DS02 GCP accuracy scripts (QA01 / QA03).

Distance summary statistics and the bias decomposition (systematic mean
offset vs random scatter) used both when building per-run accuracy
reports (QA01) and when recomputing stats from saved distance tables in
the cross-run comparison (QA03).
"""

__version__ = "1.0.0"
__author__ = "Arden Burrell"

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


# ==================================================================================
def classify_bias_fraction(frac: Optional[float]) -> str:
    """Label a bias fraction as random / mixed / biased.

    ``frac`` is ``|bias| / rmse`` in [0, 1]. Cut-offs follow the
    convention: <=0.3 dominantly random scatter, >=0.7 dominantly
    systematic offset, anything in between is a mixture.

    Parameters
    ----------
    frac : float or None
        Bias fraction, or None/NaN when undefined.

    Returns
    -------
    str
        ``"random"``, ``"mixed"``, ``"biased"`` or ``"unknown"``.
    """
    if frac is None or not np.isfinite(frac):
        return "unknown"
    if frac <= 0.3:
        return "random"
    if frac >= 0.7:
        return "biased"
    return "mixed"


# ==================================================================================
def axis_bias(series: pd.Series) -> Dict[str, Any]:
    """Decompose a signed-error series into bias + random scatter.

    For a residual series ``r``, the mean is the systematic bias, the
    population standard deviation is the random scatter, and they
    satisfy ``rmse**2 = mean**2 + std**2``. ``bias_fraction`` is
    ``|mean| / rmse`` in [0, 1] (0 = purely random, 1 = purely
    systematic).

    Parameters
    ----------
    series : pd.Series
        Signed error values (metres); NaNs are dropped.

    Returns
    -------
    dict
        ``n, mean, std, rmse, bias_fraction, classification``.
        Empty input yields ``{"n": 0}``.
    """
    series = series.dropna()
    if series.empty:
        return {"n": 0}
    arr = series.to_numpy(dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=0))
    rmse = float(np.sqrt(np.mean(arr ** 2)))
    frac: Optional[float] = float(abs(mean) / rmse) if rmse > 0 else None
    return {
        "n": int(arr.size),
        "mean": mean,
        "std": std,
        "rmse": rmse,
        "bias_fraction": frac,
        "classification": classify_bias_fraction(frac),
    }


# ==================================================================================
def bias_analysis(matched: pd.DataFrame) -> Dict[str, Any]:
    """Quantify whether matched-point errors are random or systematic.

    Reports per-axis bias for easting, northing and height, plus a
    combined 2D bias vector (magnitude and bearing clockwise from
    grid north). The 2D summary uses the planar RMSE so its
    ``bias_fraction`` is directly comparable to ``distance_2d``'s
    ``rmse``.

    Parameters
    ----------
    matched : pd.DataFrame
        Matched-point distance table with ``delta_easting_m``,
        ``delta_northing_m``, ``delta_height_m`` and ``distance_2d_m``
        columns (the QA01 output schema).

    Returns
    -------
    dict
        ``rule, planar_2d, easting, northing, height`` payload
        (JSON-serialisable).
    """
    east = axis_bias(matched["delta_easting_m"])
    north = axis_bias(matched["delta_northing_m"])
    height = axis_bias(matched["delta_height_m"])

    d2d = matched["distance_2d_m"].dropna().to_numpy(dtype=float)
    if d2d.size == 0:
        planar: Dict[str, Any] = {"n": 0}
    else:
        mean_e = float(matched["delta_easting_m"].dropna().mean())
        mean_n = float(matched["delta_northing_m"].dropna().mean())
        bias_mag = float(np.hypot(mean_e, mean_n))
        rmse_2d = float(np.sqrt(np.mean(d2d ** 2)))
        if bias_mag > 0:
            bearing = float(np.degrees(np.arctan2(mean_e, mean_n)) % 360.0)
        else:
            bearing = None
        frac: Optional[float] = float(bias_mag / rmse_2d) if rmse_2d > 0 else None
        planar = {
            "n": int(d2d.size),
            "mean_delta_easting_m": mean_e,
            "mean_delta_northing_m": mean_n,
            "bias_magnitude_m": bias_mag,
            "bias_bearing_deg": bearing,
            "rmse_2d_m": rmse_2d,
            "bias_fraction": frac,
            "classification": classify_bias_fraction(frac),
        }

    return {
        "rule": (
            "bias_fraction = |mean(error)| / rmse, in [0, 1]. "
            "<=0.3 random, >=0.7 biased, otherwise mixed."
        ),
        "planar_2d": planar,
        "easting": east,
        "northing": north,
        "height": height,
    }


# ==================================================================================
def distance_stats(series: pd.Series) -> Dict[str, Any]:
    """Summary statistics for a distance series, in metres.

    Parameters
    ----------
    series : pd.Series
        Distance values (metres); NaNs are dropped.

    Returns
    -------
    dict
        ``n, mean, median, min, max, std, rmse`` (JSON-friendly;
        counts are integers, all other numbers floats). Empty input
        yields ``{"n": 0}``.
    """
    series = series.dropna()
    if series.empty:
        return {"n": 0}
    arr = series.to_numpy(dtype=float)
    return {
        "n": int(arr.size),
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "min": float(arr.min()),
        "max": float(arr.max()),
        "std": float(arr.std(ddof=0)),
        "rmse": float(np.sqrt(np.mean(arr ** 2))),
    }
