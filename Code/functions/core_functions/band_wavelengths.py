"""Read per-band centre wavelengths from a GDAL-readable raster."""

import pathlib
import warnings as warn
from typing import Dict, Tuple

import numpy as np


def band_wavelengths(raster_path: pathlib.Path) -> Tuple[Dict[int, float], str]:
    """Read per-band centre wavelengths and the native dtype of a raster.

    GDAL exposes the ENVI header ``wavelength`` block as per-band metadata
    tags, which is where GRYFN orthomosaics store their band centres.
    (The flattened dataset-level ``wavelength`` attribute is useless — it
    collapses to a single float.)

    Parameters
    ----------
    raster_path : pathlib.Path
        Path to the raster (ENVI ``.bin`` with ``.hdr`` sidecar, or any
        GDAL-readable format).

    Returns
    -------
    dict of int to float
        Mapping of 1-based band index to centre wavelength (nm). Bands
        without a wavelength tag map to NaN.
    str
        The native (on-disk) dtype of band 1, e.g. ``"uint16"``.
    """
    # Local import keeps `import Code.functions.core_functions` usable in
    # environments without rasterio.
    import rasterio

    wavelengths: Dict[int, float] = {}
    with rasterio.open(raster_path) as src:
        src_dtype = src.dtypes[0]
        for bidx in range(1, src.count + 1):
            tag = src.tags(bidx).get("wavelength")
            wavelengths[bidx] = float(tag) if tag is not None else np.nan
    if all(np.isnan(v) for v in wavelengths.values()):
        warn.warn(
            f"No per-band wavelength metadata found for {raster_path}. "
            "The 'wavelength' column will be NaN; check the ENVI .hdr sidecar.")
    return wavelengths, src_dtype
