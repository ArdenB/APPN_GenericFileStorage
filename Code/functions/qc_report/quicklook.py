"""Product quicklook images for the QC report's "At a glance" grid.

QC report plan v1.4 §4.2: each quicklook is made by the script that
already reads the product (QC03: CIR + DSM; QC01: trajectory) and listed
in its ``artifacts`` as ``quicklook_*.png``; assembly only places them.
Reads are decimated (``out_shape``), so a multi-GB cube is never loaded
whole. Not imported by ``qc_report/__init__`` — keeps rasterio out of
the contract reader/writer import path.
"""

import pathlib
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

import Code.functions.core_functions as cf


# ==================================================================================
def is_quicklook(artifact: Any) -> bool:
    """Return True for a ``quicklook_*.png`` artifact path.

    Parameters
    ----------
    artifact : Any
        A ``QC_data``-relative artifact path.

    Returns
    -------
    bool
        Whether the artifact is an At-a-glance quicklook.
    """
    name = pathlib.PurePosixPath(str(artifact).replace("\\", "/")).name
    return name.startswith("quicklook_") and name.lower().endswith(".png")


# ==================================================================================
def find_dsm(products_dir: pathlib.Path) -> Optional[pathlib.Path]:
    """Pick the highest-resolution LiDAR DSM TIF from a products folder.

    Looks for ``*_LiDAR_DSM_<cm>cm.tif`` and returns the smallest
    ``<cm>`` (highest resolution). Shared by QC00 (GCP heights) and QC03
    (DSM quicklook).

    Parameters
    ----------
    products_dir : pathlib.Path
        The ``<NAME>.gpro/products`` folder for a run.

    Returns
    -------
    pathlib.Path or None
        Selected DSM TIF, or None if no candidate is present.
    """
    if not products_dir.is_dir():
        return None
    candidates: List[Tuple[int, pathlib.Path]] = []
    for tif in products_dir.glob("*_LiDAR_DSM_*cm.tif"):
        match = re.search(r"_LiDAR_DSM_(\d+)cm\.tif$", tif.name, re.IGNORECASE)
        if match:
            candidates.append((int(match.group(1)), tif))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


# ==================================================================================
def render_cir(
        bin_path: pathlib.Path,
        out_png: pathlib.Path,
        max_px: int = 1200,
        stretch_pct: Tuple[float, float] = (2.0, 98.0),
    ) -> Dict[str, Any]:
    """Write a false-colour (NIR/red/green) quicklook of a VNIR cube.

    Bands nearest 800/660/560 nm are read decimated to ``max_px`` on the
    long side; each is stretched to its own percentiles over non-zero
    pixels (zeros are no-data and render white).

    Parameters
    ----------
    bin_path : pathlib.Path
        ENVI ``.bin`` reflectance cube (``.hdr`` alongside).
    out_png : pathlib.Path
        Output PNG path (parent created).
    max_px : int
        Long-side size of the quicklook in pixels.
    stretch_pct : tuple of float
        Lower/upper stretch percentiles.

    Returns
    -------
    dict
        Reproducibility record: ``bands`` (1-based), ``wavelengths_nm``,
        ``stretch_pct`` and per-band ``stretch`` limits.

    Raises
    ------
    ValueError
        When the cube has no wavelength metadata (no CIR possible).
    """
    import rasterio

    waves, _ = cf.band_wavelengths(bin_path)
    known = {b: w for b, w in waves.items() if np.isfinite(w)}
    if not known:
        raise ValueError(f"No band wavelengths in {bin_path}")
    bands = [min(known, key=lambda b: abs(known[b] - target))
             for target in (800.0, 660.0, 560.0)]
    with rasterio.open(bin_path) as src:
        shape = _decimated_shape(src.height, src.width, max_px)
        cube = src.read(bands, out_shape=(3, *shape)).astype("float32")
    valid = np.all(cube > 0, axis=0) & np.all(np.isfinite(cube), axis=0)
    rgb = np.ones((*shape, 3), dtype="float32")
    limits = []
    for i in range(3):
        lo, hi = (np.percentile(cube[i][valid], stretch_pct)
                  if valid.any() else (0.0, 1.0))
        limits.append([float(lo), float(hi)])
        scaled = np.clip((cube[i] - lo) / max(hi - lo, 1e-9), 0, 1)
        rgb[..., i] = np.where(valid, scaled, 1.0)
    _save_image(rgb, out_png)
    return {"bands": bands,
            "wavelengths_nm": [known[b] for b in bands],
            "stretch_pct": list(stretch_pct), "stretch": limits}


# ==================================================================================
def render_dsm(
        tif_path: pathlib.Path,
        out_png: pathlib.Path,
        max_px: int = 1200,
        stretch_pct: Tuple[float, float] = (2.0, 98.0),
    ) -> Dict[str, Any]:
    """Write a colour-ramped, hillshaded DSM quicklook.

    Parameters
    ----------
    tif_path : pathlib.Path
        LiDAR DSM GeoTIFF.
    out_png : pathlib.Path
        Output PNG path (parent created).
    max_px : int
        Long-side size of the quicklook in pixels.
    stretch_pct : tuple of float
        Lower/upper elevation stretch percentiles.

    Returns
    -------
    dict
        Reproducibility record: ``stretch_pct`` and elevation ``stretch``
        limits (m).
    """
    import rasterio
    from matplotlib import colormaps
    from matplotlib.colors import LightSource

    with rasterio.open(tif_path) as src:
        shape = _decimated_shape(src.height, src.width, max_px)
        dem = src.read(1, out_shape=shape, masked=True).astype("float32")
        # cell size grows with decimation; keeps hillshade relief honest
        cell = abs(src.res[0]) * src.width / shape[1]
    elev = dem.filled(np.nan)
    valid = np.isfinite(elev)
    lo, hi = (np.percentile(elev[valid], stretch_pct)
              if valid.any() else (0.0, 1.0))
    filled = np.where(valid, elev, lo)
    shaded = LightSource(azdeg=315, altdeg=45).shade(
        filled, cmap=colormaps["viridis"], vmin=lo, vmax=hi,
        blend_mode="soft", dx=cell, dy=cell)[..., :3]
    shaded[~valid] = 1.0
    _save_image(shaded, out_png)
    return {"stretch_pct": list(stretch_pct),
            "stretch": [float(lo), float(hi)]}


# ==================================================================================
def _decimated_shape(height: int, width: int, max_px: int) -> Tuple[int, int]:
    """Return a (rows, cols) read shape with the long side ≤ ``max_px``.

    Parameters
    ----------
    height, width : int
        Native raster size.
    max_px : int
        Long-side limit.

    Returns
    -------
    tuple of int
        Decimated shape (never upsampled).
    """
    scale = min(1.0, max_px / max(height, width))
    return max(1, round(height * scale)), max(1, round(width * scale))


# ==================================================================================
def _save_image(rgb: np.ndarray, out_png: pathlib.Path) -> None:
    """Save an RGB float array (0–1) as a PNG with no axes.

    Parameters
    ----------
    rgb : np.ndarray
        ``(rows, cols, 3)`` image in 0–1.
    out_png : pathlib.Path
        Destination (parent created).

    Returns
    -------
    None
    """
    import matplotlib.pyplot as plt

    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.imsave(out_png.as_posix(), np.clip(rgb, 0, 1))
