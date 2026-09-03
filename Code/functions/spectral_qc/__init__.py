"""Shared helpers for the DS02 spectral QC scripts (QA00 / QA02).

Ports the relevant APEx_SensorCalibration conventions: physical (nm)
bad-band definitions, the tiered run-palette strategy, and the
cross-sensor wavelength reference-grid snap.
"""

__version__ = "1.0.0"
__author__ = "Arden Burrell"

import warnings as warn
from typing import Dict, List, Tuple, Any

import numpy as np
import pandas as pd

# +++++ Palette tiers live in core_functions; re-exported for back-compat +++++
from ..core_functions.run_palette import (run_sort_key, resolve_run_palette,
                                          resolve_node_run_palette)
from .panel_library import (panels_root, node_library_dir, gpro_panel_set,
                            resolve_panel_set, load_panel_dhr)
from .dhr_drift import within_day_drift


# ==================================================================================
def default_bad_wavelengths() -> Dict[str, Dict[str, List[Tuple[float, float]]]]:
    """Return known-bad wavelength ranges (nm) per sensor and EM region.

    Ranges are defined in wavelength rather than band index because band
    centres differ slightly between sensor units; a physical (nm)
    definition masks the equivalent region on every unit without
    band/wavelength alignment.

    The CALVIS SWIR ranges are the two atmospheric water-vapour
    absorption features (~1400 and ~1900 nm) — the only operator-approved
    bad bands (2026-08-26; the 890-950 edge and 2440-2600 detector-tail
    ranges were removed the same day). VNIR bad regions are TBD pending
    characterisation of the sensor limitations and are currently not
    masked (the DT01 candidate ranges were reverted — VNIR has no
    approved bad bands). Bad-band changes require explicit operator
    sign-off.

    Returns
    -------
    dict of str to dict of str to list of tuple of float
        ``{sensor: {EM_Region: [(lo_nm, hi_nm), ...]}}`` (inclusive).
    """
    return ({
        "CALVIS": {
            "SWIR": [
                (1345.0, 1435.0),  # 1400 nm water vapour
                (1790.0, 1960.0),  # 1900 nm water vapour
            ],
        },
    })


# ==================================================================================
def bad_wavelength_mask(
        wavelengths: pd.Series,
        ranges: List[Tuple[float, float]],
    ) -> pd.Series:
    """Boolean mask of rows whose wavelength falls in a known-bad range.

    Parameters
    ----------
    wavelengths : pd.Series
        Wavelength column (nm). NaN wavelengths are never masked.
    ranges : list of tuple of float
        Inclusive ``(lo_nm, hi_nm)`` ranges.

    Returns
    -------
    pd.Series
        True where the wavelength is inside any bad range.
    """
    mask = pd.Series(False, index=wavelengths.index)
    for lo, hi in ranges:
        mask |= wavelengths.between(lo, hi)
    return mask


# ==================================================================================
def reflectance_pct(values: pd.Series) -> pd.Series:
    """Convert a reflectance column to percent (0-100).

    Integer tables are 0-10000 scaled; float tables are 0-1 scaled.

    Parameters
    ----------
    values : pd.Series
        The ``value`` column of an extracted spectra table.

    Returns
    -------
    pd.Series
        Reflectance in percent.
    """
    if pd.api.types.is_integer_dtype(values):
        return values / 100.0
    return values * 100.0


# ==================================================================================
def zero_nodata_mask(values: pd.Series) -> pd.Series:
    """True where a spectra-table value is the 0 = nodata sentinel.

    In the extracted panel tables a value of exactly 0 is missing data
    (e.g. SWIR gaps over a panel), not a real reflectance/radiance.
    NaN values (raster-declared nodata that survived extraction) count
    as nodata too.

    Parameters
    ----------
    values : pd.Series
        The ``value`` column of an extracted spectra table.

    Returns
    -------
    pd.Series
        Boolean mask, True on nodata samples.
    """
    return values.isna() | (values == 0)


# ==================================================================================
def known_panel_sets() -> Dict[str, frozenset]:
    """Return the Panel_ref signatures of the standard APPN panel sets.

    The GeoJSON filename encodes how a set was *used* in a flight
    (``QC_VAL_north``, ``QC_VAL_blue``, ...), not what hardware it is.
    The nominal reflectance values identify the physical set instead:
    the same Gryfn 4-panel set is comparable across flights regardless
    of what it was called.

    Returns
    -------
    dict of str to frozenset
        ``{set name: frozenset of nominal Panel_ref percents}``.
    """
    return ({
        "Gryfn4P": frozenset({11, 30, 56, 82}),
        "Gryfn2P": frozenset({20, 45}),
    })


# ==================================================================================
def identify_panel_set(panel_refs) -> str:
    """Classify a panel vector file by its ``Panel_ref`` signature.

    Compares the unique nominal reflectance values against the standard
    set signatures from :func:`known_panel_sets`. Duplicated polygons
    (e.g. two Gryfn4P sets digitised in one ELM file) still match, since
    only the unique values are compared.

    Parameters
    ----------
    panel_refs : iterable
        ``Panel_ref`` values, one per polygon.

    Returns
    -------
    str
        The matching set name (e.g. ``"Gryfn4P"``), or ``"unknown"``.
    """
    refs = frozenset(int(round(float(r))) for r in panel_refs if r is not None)
    for name, signature in known_panel_sets().items():
        if refs == signature:
            return name
    return "unknown"


# ==================================================================================
def snap_wavelengths(
        df: pd.DataFrame,
        unit_col: str = "node",
        sensor_col: str = "sensor",
        verbose: bool = False,
    ) -> pd.DataFrame:
    """Snap wavelengths onto a shared reference grid per sensor/EM region.

    Each sensor unit reports its own per-band wavelength axis (offsets up
    to ~6 nm in SWIR between nominally identical Headwall units).
    Grouping on the raw ``wavelength`` would place every unit in its own
    cell and destroy cross-unit comparisons. Following the
    APEx_SensorCalibration ``ReferenceGrid`` approach, this picks one
    unit's axis as the reference (the one closest to the per-band median
    across units, by mean absolute deviation in nm) and relabels every
    row's ``wavelength`` to the reference wavelength of its band. Snapping
    is by band index when the band exists in the reference, and by
    nearest reference wavelength otherwise.

    The original axis is preserved in a ``raw_wavelength`` column.
    Regions observed by a single unit are returned unchanged (their
    ``raw_wavelength`` still gets populated).

    Parameters
    ----------
    df : pd.DataFrame
        Long spectra table with ``sensor``, ``EM_Region``, ``band``,
        ``wavelength`` and *unit_col* columns.
    unit_col : str, optional
        Column identifying the sensor unit. Default ``"node"`` (each
        node operates one unit per sensor platform).
    sensor_col : str, optional
        Column defining the sensor grouping. Default ``"sensor"``; pass
        a pooled label (e.g. ``"sensor_group"``) to snap platforms that
        share a physical sub-sensor onto one grid.
    verbose : bool, optional
        Print the chosen reference unit per region. Default False.

    Returns
    -------
    pd.DataFrame
        Copy of *df* with snapped ``wavelength`` and added
        ``raw_wavelength``.
    """
    out = df.copy()
    out["raw_wavelength"] = out["wavelength"]

    for (sensor, region), grp in out.groupby([sensor_col, "EM_Region"]):
        axis = (grp[[unit_col, "band", "wavelength"]]
                .dropna()
                .drop_duplicates([unit_col, "band"]))
        units = sorted(axis[unit_col].astype(str).unique())
        if len(units) < 2:
            continue

        # +++++ Score units against the per-band median; pick the reference +++++
        per_band_med = axis.groupby("band")["wavelength"].median()
        dev = (axis["wavelength"] - axis["band"].map(per_band_med)).abs()
        scores = dev.groupby(axis[unit_col].astype(str)).mean()
        ref_unit = scores.idxmin()
        ref = (axis[axis[unit_col].astype(str) == ref_unit]
               .set_index("band")["wavelength"])
        ref_wls = np.sort(ref.to_numpy(dtype=float))

        # +++++ (unit, band) -> reference wavelength (nearest when band missing) +++++
        ref_wl_vals = []
        for b, w in zip(axis["band"], axis["wavelength"]):
            if int(b) in ref.index:
                ref_wl_vals.append(float(ref.loc[int(b)]))
            else:
                ref_wl_vals.append(
                    float(ref_wls[int(np.argmin(np.abs(ref_wls - float(w))))]))
        axis = axis.assign(_ref_wl=ref_wl_vals)
        snap = axis.set_index([axis[unit_col].astype(str), "band"])["_ref_wl"]

        idx = grp.index
        keys = pd.MultiIndex.from_arrays(
            [grp[unit_col].astype(str), grp["band"]])
        out.loc[idx, "wavelength"] = snap.reindex(keys).to_numpy()

        if verbose:
            score_str = ", ".join(f"{u}={scores[u]:.3f}" for u in units)
            print(f"[{sensor}/{region}] wavelength reference unit = {ref_unit} "
                  f"({len(ref_wls)} ref bands; mean |unit-median| nm: {score_str})")
    return out


# ========== Late import: homogeneity needs bad_wavelength_mask (above) ==========
from .homogeneity import panel_homogeneity  # noqa: E402  pylint: disable=wrong-import-position
