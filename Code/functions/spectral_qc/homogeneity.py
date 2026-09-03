"""Per-panel distribution-shape homogeneity statistics (QC02).

A clean calibration panel's per-band pixel distribution is tight,
symmetric and unimodal. Shadow across a corner or mixed edge pixels
make it bimodal (``l_kurt`` depressed, |skew| inflated), specular
hotspots grow a heavy right tail (``skew`` up), and general
contamination diverges the per-band mean from the median. These
statistics detect the failure modes the mean-based residuals silently
absorb (design record: retired QC pipeline plan §7, git history).

Thresholds are injected by the caller (``spectral_limits.yml``
``homogeneity`` block) — nothing is hardcoded here.
"""

__version__ = "1.0.0"
__author__ = "Arden Burrell"

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..core_functions.group_stats import group_value_stats
from . import bad_wavelength_mask


# ==================================================================================
def panel_homogeneity(
        tdf: pd.DataFrame,
        bad_wavelengths: Optional[List[Tuple[float, float]]] = None,
        thresholds: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Dict[str, Any]]:
    """Summarise per-band distribution shape for every panel in a target.

    Per ``Panel_ref``, over good bands only, runs the shared group
    statistics per band and collapses them to a small homogeneity
    block. A band is *flagged* when any of |skew|, ``l_kurt`` (low
    tail) or the |mean - median| divergence crosses its threshold; the
    panel grades ``suspect`` when the flagged-band fraction crosses
    ``fraction_bands_warn_above``.

    Parameters
    ----------
    tdf : pd.DataFrame
        Extracted spectra rows for one target x EM region. Required
        columns: ``Panel_ref``, ``band``, ``wavelength``, ``refl_pct``.
    bad_wavelengths : list of tuple of float, optional
        ``(lo_nm, hi_nm)`` ranges excluded before the statistics
        (``spectral_qc.default_bad_wavelengths`` for the sensor/region).
    thresholds : dict, optional
        ``homogeneity`` block of ``spectral_limits.yml`` with keys
        ``abs_skew_warn_above``, ``l_kurt_warn_below``,
        ``mean_median_divergence_warn_pp`` and
        ``fraction_bands_warn_above``. None computes the statistics but
        leaves ``flag`` as None (caller grades ``not_checked``).

    Returns
    -------
    dict of str to dict
        Per stringified ``Panel_ref``: ``n_px``, ``n_bands``,
        ``median_abs_skew``, ``median_l_kurt``,
        ``mean_median_divergence_pct`` (median |mean - median| per
        band, reflectance pp), ``n_bands_flagged``,
        ``fraction_bands_flagged`` and ``flag``
        (``clean``/``suspect``/None). Flag counts are None when no
        thresholds were given.

    Raises
    ------
    KeyError
        If a required column is missing.
    """
    required = ["Panel_ref", "band", "wavelength", "refl_pct"]
    missing = [c for c in required if c not in tdf.columns]
    if missing:
        raise KeyError(f"panel_homogeneity: missing columns {missing}")
    good = tdf
    if bad_wavelengths:
        good = tdf[~bad_wavelength_mask(tdf["wavelength"], bad_wavelengths)]

    out: Dict[str, Dict[str, Any]] = {}
    for ref, pdf in good.groupby("Panel_ref"):
        code = str(int(float(ref)))  # type: ignore[arg-type]
        g = group_value_stats(pdf, ["band"], value_col="refl_pct")
        if g.empty:
            continue
        g = g.assign(abs_skew=g["skew"].abs(),
                     divergence_pp=(g["mean"] - g["median"]).abs())
        block: Dict[str, Any] = {
            "n_px": int(pdf["band"].value_counts().max()),
            "n_bands": int(len(g)),
            "median_abs_skew": _nanmedian(g["abs_skew"]),
            "median_l_kurt": _nanmedian(g["l_kurt"]),
            "mean_median_divergence_pct": _nanmedian(g["divergence_pp"]),
        }
        if thresholds is None:
            block.update({"n_bands_flagged": None,
                          "fraction_bands_flagged": None, "flag": None})
        else:
            flagged = (
                (g["abs_skew"] > float(thresholds["abs_skew_warn_above"]))
                | (g["l_kurt"] < float(thresholds["l_kurt_warn_below"]))
                | (g["divergence_pp"]
                   > float(thresholds["mean_median_divergence_warn_pp"])))
            fraction = float(flagged.mean())
            block.update({
                "n_bands_flagged": int(flagged.sum()),
                "fraction_bands_flagged": fraction,
                "flag": ("suspect"
                         if fraction
                         > float(thresholds["fraction_bands_warn_above"])
                         else "clean"),
            })
        out[code] = block
    return out


# ==================================================================================
def _nanmedian(series: pd.Series) -> float:
    """NaN-safe median of a metric column.

    Parameters
    ----------
    series : pd.Series
        Per-band metric values.

    Returns
    -------
    float
        Median over finite values, NaN when none remain.
    """
    vals = series.to_numpy(dtype=float)
    vals = vals[np.isfinite(vals)]
    return float(np.median(vals)) if vals.size else float("nan")
