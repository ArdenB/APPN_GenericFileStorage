"""Band→spyndex mapping helpers for the DS05 spectral index scripts.

Maps sensor band-centre wavelengths (read from raster metadata via
:func:`Code.functions.core_functions.band_wavelengths`) onto the standard
spectral band symbols used by :mod:`spyndex` (``B``, ``G``, ``R``, ``N``,
``S1`` …), reports which indices are computable from the available bands,
and resolves the ``lambdaX`` wavelength constants.

The band definitions come from :func:`spectral_band_definitions` (the
former ``HSDrone_pipe/data/SpectralTable.csv``, preserved verbatim in
``Code/DS05_SpectralIndices/legacy/``). A future "matching sensor"
feature will pass an alternative definitions table (another sensor's
band min/max/peak) through the ``definitions`` argument of
:func:`map_bands_to_spyndex` to produce sensor-matched indices.
"""

import warnings as warn
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import spyndex


# ==================================================================================
def spectral_band_definitions() -> pd.DataFrame:
    """Return the standard spectral band definitions used for mapping.

    One row per spyndex band symbol: the wavelength window over which a
    broadband sensor would integrate (``min_nm``/``max_nm``) and the
    nominal band centre (``peak_nm``). Values reproduce the legacy
    ``SpectralTable.csv`` (Sentinel-2 style band windows).

    Returns
    -------
    pandas.DataFrame
        Columns ``description``, ``standard`` (spyndex band symbol),
        ``min_nm``, ``max_nm``, ``peak_nm``.
    """
    rows = [
        # description,     standard, min_nm, max_nm, peak_nm
        ("Aerosols",       "A",        400,    455,   427.5),
        ("Blue",           "B",        450,    530,   490.0),
        ("Green 1",        "G1",       510,    550,   530.0),
        ("Green",          "G",        510,    600,   555.0),
        ("Yellow",         "Y",        585,    625,   605.0),
        ("Red",            "R",        620,    690,   655.0),
        ("Red Edge 1",     "RE1",      695,    715,   705.0),
        ("Red Edge 2",     "RE2",      730,    750,   740.0),
        ("Red Edge 3",     "RE3",      765,    795,   780.0),
        ("NIR",            "N",        760,    900,   830.0),
        ("NIR 2",          "N2",       850,    880,   865.0),
        ("Water Vapour",   "WV",       930,    960,   945.0),
        ("SWIR 1",         "S1",      1550,   1750,  1650.0),
        ("SWIR 2",         "S2",      2080,   2350,  2215.0),
        ("Thermal",        "T",      10400,  12500, 11450.0),
        ("Thermal 1",      "T1",     10600,  11190, 10895.0),
        ("Thermal 2",      "T2",     11500,  12510, 12005.0),
    ]
    return pd.DataFrame(
        rows, columns=["description", "standard", "min_nm", "max_nm", "peak_nm"])


# ==================================================================================
@dataclass(frozen=True)
class BandMapping:
    """Resolved mapping from raster bands to spyndex band symbols.

    Attributes
    ----------
    method : str
        Band aggregation method used: ``"Peak"`` (single nearest band
        per symbol) or ``"Mean"`` (average of all bands inside the
        symbol's wavelength window).
    band_map : dict of str to list of int
        Spyndex band symbol → 1-based raster band indices. ``Peak``
        entries hold exactly one index; ``Mean`` entries hold every
        band inside the window.
    constants : dict of str to float
        Spyndex constants resolved for this mapping: every constant
        with a non-None default plus the ``lambdaX`` band-centre
        wavelengths for the mapped symbols.
    wavelengths : dict of int to float
        1-based raster band index → centre wavelength (nm), as read
        from the raster (provenance).
    """
    method: str
    band_map: Dict[str, List[int]] = field(default_factory=dict)
    constants: Dict[str, float] = field(default_factory=dict)
    wavelengths: Dict[int, float] = field(default_factory=dict)


# ==================================================================================
def map_bands_to_spyndex(
        wavelengths: Dict[int, float],
        method: str = "Peak",
        definitions: Optional[pd.DataFrame] = None,
    ) -> BandMapping:
    """Map raster band wavelengths onto spyndex band symbols.

    For every band-definition row whose nominal peak falls inside the
    raster's wavelength range, resolve which raster band(s) represent
    that spyndex symbol and record the symbol's centre wavelength as a
    ``lambdaX`` constant.

    Parameters
    ----------
    wavelengths : dict of int to float
        1-based raster band index → centre wavelength (nm), e.g. from
        :func:`read_band_wavelengths`. NaN entries are ignored.
    method : str, optional
        ``"Peak"`` — the single band whose centre is nearest the
        symbol's nominal peak; ``"Mean"`` — every band whose centre
        lies inside the symbol's ``[min_nm, max_nm]`` window (falls
        back to the nearest-peak band when the window contains no band
        centre). Default ``"Peak"``.
    definitions : pandas.DataFrame, optional
        Alternative band-definition table with the columns of
        :func:`spectral_band_definitions`. Reserved for the future
        matching-sensor feature (pass another sensor's band windows to
        produce sensor-matched indices). Default None (standard table).

    Returns
    -------
    BandMapping
        The resolved symbol → band mapping plus constants.

    Raises
    ------
    ValueError
        If ``method`` is not ``"Peak"`` or ``"Mean"``, or no band has a
        valid wavelength.
    """
    if method not in ("Peak", "Mean"):
        raise ValueError(f"method={method!r} is not valid; use 'Peak' or 'Mean'.")
    if definitions is None:
        definitions = spectral_band_definitions()

    wl = pd.Series(wavelengths, dtype=float).dropna()
    if wl.empty:
        raise ValueError("No bands with a valid wavelength; cannot map to spyndex symbols.")

    # +++++ Constants: every spyndex constant with a non-None default +++++
    # Built without mutating spyndex's global Constant objects (the legacy
    # scripts set .value in place, which leaks state between scenes).
    const_objs = spyndex.constants.to_dict()
    constants: Dict[str, float] = {
        k: float(c.default) for k, c in const_objs.items() if c.default is not None}

    band_map: Dict[str, List[int]] = {}
    for _, row in definitions.iterrows():
        # +++++ Skip symbols whose nominal peak is outside the sensor range +++++
        if not (wl.min() <= row["peak_nm"] <= wl.max()):
            continue

        if method == "Peak":
            band_idxs = [int((wl - row["peak_nm"]).abs().idxmin())]
        else:  # Mean
            in_window = wl[(wl >= row["min_nm"]) & (wl <= row["max_nm"])]
            if in_window.empty:
                band_idxs = [int((wl - row["peak_nm"]).abs().idxmin())]
            else:
                band_idxs = [int(i) for i in in_window.index]
        band_map[row["standard"]] = band_idxs

        # +++++ Record the symbol's centre wavelength as a lambda constant +++++
        if f"lambda{row['standard']}" in const_objs:
            constants[f"lambda{row['standard']}"] = float(row["peak_nm"])

    return BandMapping(
        method=method, band_map=band_map, constants=constants,
        wavelengths=dict(wavelengths))


# ==================================================================================
def computable_indices(
        band_symbols: List[str],
        constants: Dict[str, float],
        restrict: Optional[List[str]] = None,
    ) -> Tuple[List[str], Dict[str, List[str]]]:
    """Report which spyndex indices are computable from the given bands.

    Parameters
    ----------
    band_symbols : list of str
        Spyndex band symbols available (keys of
        :attr:`BandMapping.band_map`).
    constants : dict of str to float
        Resolved constants (:attr:`BandMapping.constants`).
    restrict : list of str, optional
        Curated index list; only these indices are considered. Names
        not known to spyndex raise, names known but not computable are
        reported in the skipped mapping. Default None (all indices).

    Returns
    -------
    list of str
        Index names computable from the available bands + constants,
        in spyndex catalogue order.
    dict of str to list of str
        Index name → missing band symbols, for every index considered
        but not computable.

    Raises
    ------
    ValueError
        If ``restrict`` contains a name not in the spyndex catalogue.
    """
    available = set(band_symbols) | set(constants)
    catalogue = list(spyndex.indices)
    if restrict is not None:
        unknown = sorted(set(restrict) - set(catalogue))
        if unknown:
            raise ValueError(
                f"Unknown spyndex index name(s): {unknown}. "
                "See spyndex.indices for the catalogue.")
        catalogue = [i for i in catalogue if i in set(restrict)]

    computable: List[str] = []
    skipped: Dict[str, List[str]] = {}
    for ind in catalogue:
        missing = [b for b in spyndex.indices[ind].bands if b not in available]
        if missing:
            skipped[ind] = missing
        else:
            computable.append(ind)
    return computable, skipped
