"""Shared helpers for the DS05 spectral index scripts (SI00).

Band→spyndex symbol mapping and index-applicability filtering. Raster
band-wavelength reading lives in
:func:`Code.functions.core_functions.band_wavelengths`.
"""

__version__ = "1.0.0"
__author__ = "Arden Burrell"

from .band_mapping import (
    BandMapping,
    computable_indices,
    map_bands_to_spyndex,
    spectral_band_definitions,
)

__all__ = [
    "BandMapping",
    "computable_indices",
    "map_bands_to_spyndex",
    "spectral_band_definitions",
]
