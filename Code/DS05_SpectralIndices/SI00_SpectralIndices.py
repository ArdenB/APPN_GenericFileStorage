"""Spectral index calculation from hyperspectral orthomosaics (DS05).

This script automatically crawls the dataset file structure for GRYFN
``.bin`` orthomosaics (``<run>/T1_proc/*.gpro/products/*_{VNIR|SWIR}_
Orthomosaic.bin``) and computes every spyndex spectral index the sensor's
bands support, writing per-run index maps into
``<run>/T1_proc/SpectralIndices/``.

SI00 is **raster only** (locked 2026-08-13): it reads the orthomosaics and
their band metadata and writes index maps. It never opens plot geojson or
parquet — all plot-level interaction lives in DS03 (PE02 extracts per-plot
values from these index maps).

Sensor differences are data, not code paths: per-band centre wavelengths
are read from the raster's GDAL band tags and snapped to the spyndex band
symbols via ``Code.functions.spectral_indices``. GOBI runs produce a VNIR
product; CALVIS runs produce a VNIR product plus a combined VNIR+SWIR
product (VNIR resampled onto the SWIR grid) holding only the indices that
need a SWIR band.

Outputs per run x EM region x band-aggregation method:

- ``SI_{region}_{method}[_gproN].nc`` — one variable per index
  (compressed NetCDF; split into ``_partNNofMM.nc`` files when the index
  stack exceeds the memory budget). ``--format geotiff`` writes one
  single-band tiled GTiff per index instead.
- ``SI_{region}_{method}[_gproN]_report.json`` — machine-readable
  manifest: produced files, band mapping, per-index stats, skipped
  indices. PE02 and the caching check consume this.
- ``SI_{region}_{method}[_gproN]_overview.md`` — human overview report
  with per-index raster stats and embedded figures.
- ``SI_figures/`` — headline-index histograms + map thumbnail.

Computation is cached: a product is only regenerated when its report or
any file the report lists is missing or older than the source
orthomosaic(s). Use ``--force`` to override.

Command-line Arguments
----------------------
--path : str, optional
    Folder to crawl for orthomosaics. Defaults to the git repo root.
--method : str, optional
    Band aggregation: ``Mean`` (average over the symbol's wavelength
    window, default), ``Peak`` (single nearest band per symbol), or
    ``both``.
--indices : str [str ...], optional
    Restrict to a curated list of spyndex index names. Default is every
    index computable from the sensor's bands.
--format : str, optional
    ``netcdf`` (default) or ``geotiff`` (one single-band file per index).

Notes
-----
A future matching-sensor feature (pass another sensor's band widths to
produce sensor-matched indices) is planned; the ``definitions`` argument
of :func:`Code.functions.spectral_indices.map_bands_to_spyndex` is the
hook for it. Not in scope for v1.0.
"""

# ==============================================================================

__title__ = "Spectral index calculation"
__author__ = "Arden Burrell"
__version__ = "v1.0(13.08.2026)"
__email__ = "arden.burrell@sydney.edu.au"


# ==============================================================================
# ========== Import core packages ==========
import gc
import os
import sys
import json
import argparse
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# ========== Import other packages ==========
import git
from git import exc as git_exc
import numpy as np
import pandas as pd
import psutil
import spyndex
import xarray as xr
import rioxarray
from dask.diagnostics.progress import ProgressBar
from tqdm import tqdm
import warnings as warn
import matplotlib
import matplotlib.pyplot as plt

# ========== Resolve git root (must happen before importing Code.functions.*) ==========
# Resolved from this file's location so the Code/ package is importable even
# when the search --path points at a non-git dataset root.
try:
    _git_root = git.Repo(
        os.path.dirname(os.path.abspath(__file__)),
        search_parent_directories=True,
    ).git.rev_parse("--show-toplevel")
except git_exc.InvalidGitRepositoryError as err:
    raise git_exc.InvalidGitRepositoryError(
        f"Could not resolve the git repo containing this script ({__file__})."
    ) from err
if _git_root not in sys.path:
    sys.path.insert(0, _git_root)

# ========== Import custom packages ==========
import Code.functions.core_functions as cf
import Code.functions.spectral_indices as si


# ==================================================================================
@dataclass(frozen=True)
class SIConfig:
    """Tunable settings for one spectral-index invocation.

    All fixed facts and thresholds live on this object so callers pass a
    single ``cfg`` argument through the pipeline rather than relying on
    module-level constants.

    Attributes
    ----------
    schema_version : float
        Report/manifest schema version. Bump when the output layout
        changes in a non-backwards-compatible way.
    valid_sensors : tuple of str
        Sensor platform folder names handled by this script.
    output_dirname : str
        Name of the per-run output folder inside ``T1_proc/``.
    figures_dirname : str
        Name of the figure folder inside the output folder.
    int_scale : float
        Reflectance scale factor applied to integer rasters (GRYFN
        orthos are 0-10000 scaled).
    mem_headroom_bytes : float
        Memory kept free when sizing index-computation chunks.
    headline_indices : tuple of str
        Preferred indices for the overview figures (first computable
        one becomes the map thumbnail).
    hist_sample_size : int
        Max finite pixels sampled per index for the histogram figure.
    thumbnail_max_px : int
        Max thumbnail dimension (pixels); maps are strided down to it.
    """
    schema_version: float = 1.0
    valid_sensors: Tuple[str, ...] = ("GOBI", "CALVIS")
    output_dirname: str = "SpectralIndices"
    figures_dirname: str = "SI_figures"
    int_scale: float = 10000.0
    mem_headroom_bytes: float = 8e9
    headline_indices: Tuple[str, ...] = (
        "NDVI", "GNDVI", "NDREI", "EVI", "SAVI", "NDWI", "NDMI", "NBR")
    hist_sample_size: int = 200_000
    thumbnail_max_px: int = 1024

    def ortho_types(self) -> Dict[str, Tuple[str, ...]]:
        """Return the EM-region orthomosaics expected per sensor.

        Returns
        -------
        dict of str to tuple of str
            ``{sensor: (region, ...)}`` where each region matches a
            ``*_{region}_Orthomosaic.bin`` product file.
        """
        return {"GOBI": ("VNIR",), "CALVIS": ("VNIR", "SWIR")}


def default_config() -> SIConfig:
    """Return the default :class:`SIConfig` for this tool."""
    return SIConfig()


# ==================================================================================
def main(
        args: argparse.Namespace,
        path: pathlib.Path,
    ) -> pd.DataFrame:
    """Run the spectral index calculation pipeline.

    Crawls the provided path for run orthomosaics, maps each sensor's
    bands to spyndex symbols, computes the index maps per run x EM
    region x method, and writes the maps, reports, and figures into
    each run's ``T1_proc/SpectralIndices/`` folder.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments.
    path : pathlib.Path
        Root directory to search for orthomosaics.

    Returns
    -------
    pandas.DataFrame
        One row per run/region/method summarising the outcome.
    """
    cfg = default_config()

    # ========== Find the orthomosaics and group them by run ==========
    jobs = locate_orthomosaics(
        path, cfg, exclude_dirs=args.exclude_dir, verbose=args.verbose,
        allow_multi_gpro=args.allow_multi_gpro)

    # ========== Compute the index maps for every run ==========
    summary_rows: List[Dict[str, Any]] = []
    for job in tqdm(jobs, total=len(jobs), desc="Processing runs"):
        summary_rows.extend(process_run(job, args, cfg))

    # ========== Print the end-of-run summary ==========
    summary = _print_run_summary(summary_rows)
    return summary


# ==================================================================================
def locate_orthomosaics(
        path: pathlib.Path,
        cfg: SIConfig,
        exclude_dirs: Optional[List[str]] = None,
        verbose: bool = False,
        allow_multi_gpro: bool = False,
    ) -> List[Dict[str, Any]]:
    """Find run orthomosaics in the given directory tree.

    Recursively searches ``path`` for GRYFN orthomosaic products
    (``<run>/T1_proc/<x>.gpro/products/*_{region}_Orthomosaic.bin``)
    and returns one job dictionary per run (per ``.gpro`` when
    ``allow_multi_gpro`` is set).

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively.
    cfg : SIConfig
        Active configuration (valid sensors, expected regions).
    exclude_dirs : list of str, optional
        Directory names to exclude; any ortho whose path contains one
        of these names is skipped. Default None.
    verbose : bool, optional
        Print extra diagnostic messages. Default False.
    allow_multi_gpro : bool, optional
        Process runs that contain more than one ``.gpro`` folder
        instead of skipping them (outputs get ``_gproN`` suffixes).
        Debugging only: the product set is ambiguous. Default False.

    Returns
    -------
    list of dict
        Each dictionary contains:

        - **run_dir** (*pathlib.Path*) -- The run folder.
        - **node**, **project**, **site**, **sensor**, **run** (*str*)
          and **date** (*pd.Timestamp*) -- Parsed APPN metadata.
        - **gpro_nu** (*int or None*) -- ``.gpro`` index when a run
          holds several (multi-gpro debugging), else None.
        - **orthos** (*dict*) -- ``{region: pathlib.Path}`` of the
          orthomosaics found for this job.
        - **outdir** (*pathlib.Path*) -- ``T1_proc/SpectralIndices/``.

    Raises
    ------
    ValueError
        If no orthomosaics are found in ``path``.
    """
    print(f"Scanning directory for orthomosaics. {pd.Timestamp.now()}")
    orthos = [
        f for f in sorted(path.rglob("*_Orthomosaic.bin"))
        if f.parent.name == "products"
        and f.parents[1].suffix == ".gpro"
        and f.parents[2].name == "T1_proc"]
    if len(orthos) == 0:
        raise ValueError(
            f"No orthomosaics found in {path}. Expected files matching "
            "<run>/T1_proc/*.gpro/products/*_Orthomosaic.bin.")

    # ========== Filter out excluded directories ==========
    if exclude_dirs:
        exclude_set = set(exclude_dirs)
        before = len(orthos)
        orthos = [f for f in orthos
                  if not (set(p.name for p in f.parents) & exclude_set)]
        if verbose and len(orthos) < before:
            print(f"Excluded {before - len(orthos)} orthomosaic(s) matching "
                  f"--exclude-dir {exclude_dirs}")

    # ========== Group the orthos by run and .gpro ==========
    runs: Dict[pathlib.Path, Dict[pathlib.Path, List[pathlib.Path]]] = {}
    for ortho in orthos:
        runs.setdefault(ortho.parents[3], {}).setdefault(ortho.parents[1], []).append(ortho)

    jobs: List[Dict[str, Any]] = []
    for run_dir, gpros in runs.items():
        # ========== Require exactly one .gpro per run ==========
        # Multiple .gpro folders usually mean the run is being actively
        # debugged/reprocessed; index maps from an ambiguous product set
        # could be misleading.
        if len(gpros) > 1 and not allow_multi_gpro:
            warn.warn(
                f"Found {len(gpros)} .gpro folders in {run_dir / 'T1_proc'} "
                f"({[g.name for g in gpros]}). Skipping this run until it is "
                "resolved to one .gpro (or use --allow-multi-gpro).")
            continue

        # ========== Parse the APPN folder structure for metadata ==========
        parsed = cf.parse_APPN_dataset_path(run_dir)
        sensor = parsed["sensor"]
        if sensor not in cfg.valid_sensors:
            if verbose:
                tqdm.write(f"Skipping run for sensor {sensor} (valid: "
                           f"{cfg.valid_sensors}): {run_dir}")
            continue

        for gpro_nu, gpro_dir in enumerate(sorted(gpros)):
            job: Dict[str, Any] = {
                "run_dir": run_dir,
                "node": parsed["node"],
                "project": parsed["project"],
                "site": parsed["site_folder"],
                "sensor": sensor,
                "date": parsed["date"],
                "run": parsed["run_folder"],
                "gpro_nu": gpro_nu if len(gpros) > 1 else None,
                "outdir": run_dir / "T1_proc" / cfg.output_dirname,
            }
            # +++++ Attach the expected region orthos for this .gpro +++++
            region_orthos: Dict[str, pathlib.Path] = {}
            for region in cfg.ortho_types()[sensor]:
                matches = [o for o in gpros[gpro_dir]
                           if o.name.endswith(f"_{region}_Orthomosaic.bin")]
                if len(matches) == 0:
                    if verbose:
                        tqdm.write(f"No {region} orthomosaic in {gpro_dir}; "
                                   f"skipping {region} for {run_dir}.")
                    continue
                if len(matches) > 1:
                    warn.warn(
                        f"Multiple {region} orthomosaics in {gpro_dir} "
                        f"({[m.name for m in matches]}); using {matches[0].name}.")
                region_orthos[region] = matches[0]
            if not region_orthos:
                if verbose:
                    tqdm.write(f"No usable orthomosaics for {run_dir}; skipping.")
                continue
            job["orthos"] = region_orthos
            jobs.append(job)
    return jobs


# ==================================================================================
def process_run(
        job: Dict[str, Any],
        args: argparse.Namespace,
        cfg: SIConfig,
    ) -> List[Dict[str, Any]]:
    """Compute every product x method combination for one run.

    Parameters
    ----------
    job : dict
        Run job from :func:`locate_orthomosaics`.
    args : argparse.Namespace
        Parsed command-line arguments.
    cfg : SIConfig
        Active configuration.

    Returns
    -------
    list of dict
        One summary row per product x method.
    """
    methods = ["Peak", "Mean"] if args.method == "both" else [args.method]

    # ========== Build the product list for this run ==========
    # VNIR alone carries the full VNIR-computable index set. When SWIR is
    # also present, a combined VNIR+SWIR product (VNIR resampled onto the
    # SWIR grid) carries only the indices that need a SWIR band.
    products: List[Dict[str, Any]] = []
    if "VNIR" in job["orthos"]:
        products.append({"region": "VNIR", "sources": {"VNIR": job["orthos"]["VNIR"]}})
    if "SWIR" in job["orthos"]:
        if "VNIR" in job["orthos"]:
            products.append({"region": "VNIRSWIR",
                             "sources": {"VNIR": job["orthos"]["VNIR"],
                                         "SWIR": job["orthos"]["SWIR"]}})
        else:
            warn.warn(
                f"SWIR orthomosaic without a VNIR partner in {job['run_dir']}; "
                "processing SWIR alone (few indices are computable from SWIR bands only).")
            products.append({"region": "SWIR", "sources": {"SWIR": job["orthos"]["SWIR"]}})

    rows: List[Dict[str, Any]] = []
    for method in methods:
        for product in products:
            rows.append(process_product(job, product, method, args, cfg))
    return rows


# ==================================================================================
def process_product(
        job: Dict[str, Any],
        product: Dict[str, Any],
        method: str,
        args: argparse.Namespace,
        cfg: SIConfig,
    ) -> Dict[str, Any]:
    """Compute, save, and report one region x method index product.

    Parameters
    ----------
    job : dict
        Run job from :func:`locate_orthomosaics`.
    product : dict
        ``{"region": str, "sources": {region: ortho_path}}``.
    method : str
        Band aggregation method (``"Peak"`` or ``"Mean"``).
    args : argparse.Namespace
        Parsed command-line arguments.
    cfg : SIConfig
        Active configuration.

    Returns
    -------
    dict
        Summary row: run metadata plus ``region``, ``method``,
        ``n_indices``, ``n_skipped``, and ``status``.
    """
    stem = _product_stem(product["region"], method, job["gpro_nu"])
    outdir: pathlib.Path = job["outdir"]
    report_path = outdir / f"{stem}_report.json"
    sources = list(product["sources"].values())
    row = {
        "project": job["project"], "site": job["site"], "sensor": job["sensor"],
        "date": job["date"].date() if pd.notna(job["date"]) else job["date"],
        "run": job["run"], "region": product["region"], "method": method,
        "n_indices": 0, "n_skipped": 0}

    # ========== Caching: skip when the report and its files are current ==========
    if not args.force and _product_up_to_date(report_path, sources, args):
        tqdm.write(f"Skipping up-to-date product: {report_path.relative_to(job['run_dir'])}")
        report = json.loads(report_path.read_text())
        row.update({"n_indices": len(report.get("indices_computed", [])),
                    "n_skipped": len(report.get("indices_skipped", {})),
                    "status": "skipped (up to date)"})
        return row

    tqdm.write(f"Processing {job['sensor']} {product['region']} {method} for "
               f"{job['run_dir']} started at {pd.Timestamp.now()}")
    outdir.mkdir(parents=True, exist_ok=True)

    # ========== Map the raster bands to spyndex symbols (lazy reads) ==========
    params, mapping_info, ref_ds, open_handles = build_params(product, method, args, cfg)
    try:
        # ========== Decide which indices to compute ==========
        valid, skipped, delegated = _select_indices(
            product, mapping_info, restrict=args.indices)
        if len(valid) == 0:
            warn.warn(f"No computable indices for {stem} in {job['run_dir']}.")
            row.update({"n_skipped": len(skipped), "status": "skipped (no computable indices)"})
            return row

        # ========== Compute the index maps in memory-bounded chunks ==========
        chunks = _memory_chunks(ref_ds, valid, cfg)
        produced: List[pathlib.Path] = []
        stats: List[Dict[str, Any]] = []
        headline: Dict[str, Any] = {}
        for indx, chunk in enumerate(chunks):
            ds_idx = _compute_chunk(chunk, params, job, ref_ds, product, method)
            stats.extend(_index_stats(ds_idx))
            if not args.skipplot:
                _collect_headline(ds_idx, headline, cfg)
            produced.extend(_write_index_maps(
                ds_idx, outdir, stem, indx, len(chunks), args.format))
            ds_idx.close()
            del ds_idx
            gc.collect()
    finally:
        for handle in open_handles:
            handle.close()

    # ========== Figures + overview report + manifest ==========
    figures: List[pathlib.Path] = []
    if not args.skipplot:
        figures = _write_figures(headline, outdir / cfg.figures_dirname, stem, cfg)
    report = _build_report(job, product, method, mapping_info, valid, skipped,
                           delegated, produced, figures, stats, cfg)
    report["requested_indices"] = sorted(args.indices) if args.indices else "all"
    report["format"] = args.format
    _write_markdown_report(report, outdir / f"{stem}_overview.md", outdir)
    report_path.write_text(json.dumps(report, indent=2, default=str))
    tqdm.write(f"Wrote report: {report_path}")

    row.update({"n_indices": len(valid), "n_skipped": len(skipped), "status": "reported"})
    return row


# ==================================================================================
def build_params(
        product: Dict[str, Any],
        method: str,
        args: argparse.Namespace,
        cfg: SIConfig,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], xr.DataArray, List[Any]]:
    """Open the source raster(s) lazily and build the spyndex parameters.

    Every entry in the returned parameter dictionary is a lazy (dask)
    2-D reflectance array, so only the bands an index actually needs
    are ever read from disk.

    Parameters
    ----------
    product : dict
        ``{"region": str, "sources": {region: ortho_path}}``. For the
        combined ``VNIRSWIR`` product the VNIR bands are resampled onto
        the SWIR grid (lazily) before mapping.
    method : str
        Band aggregation method (``"Peak"`` or ``"Mean"``).
    args : argparse.Namespace
        Parsed command-line arguments (``resample_method``).
    cfg : SIConfig
        Active configuration (integer reflectance scale).

    Returns
    -------
    dict
        Spyndex parameter mapping: band symbol → lazy 2-D
        reflectance array, plus the resolved constants.
    dict
        Mapping provenance per source region: band map, wavelengths,
        constants, scale factors.
    xarray.DataArray
        Reference (output-grid) array used for memory estimates and
        spatial metadata.
    list
        Open rioxarray handles; the caller must close them after
        computing.
    """
    handles: List[Any] = []
    ds_by_region: Dict[str, xr.DataArray] = {}
    mapping_by_region: Dict[str, si.BandMapping] = {}
    scale_by_region: Dict[str, float] = {}

    # ========== Open each source and read its band wavelengths ==========
    for region, ortho in product["sources"].items():
        ds = rioxarray.open_rasterio(ortho, chunks={"band": 1})
        handles.append(ds)
        if ds.rio.crs is None:
            raise ValueError(f"Raster {ortho} does not have a CRS defined.")
        wavelengths, src_dtype = cf.band_wavelengths(ortho)
        mapping_by_region[region] = si.map_bands_to_spyndex(wavelengths, method=method)
        scale_by_region[region] = (
            cfg.int_scale if np.issubdtype(np.dtype(src_dtype), np.integer) else 1.0)
        ds_by_region[region] = ds

    # ========== Resample VNIR onto the SWIR grid for the combined product ==========
    grid_region = "SWIR" if product["region"] == "VNIRSWIR" else product["region"]
    ref_ds = ds_by_region[grid_region]
    if product["region"] == "VNIRSWIR":
        ds_by_region["VNIR"] = ds_by_region["VNIR"].interp(
            x=ref_ds.x, y=ref_ds.y, method=args.resample_method)

    # ========== Build the lazy per-symbol reflectance parameters ==========
    # VNIR keeps its symbols on collision (SWIR only contributes new ones).
    params: Dict[str, Any] = {}
    constants: Dict[str, float] = {}
    priority = [r for r in ("VNIR", "SWIR") if r in ds_by_region]
    for region in priority:
        mapping = mapping_by_region[region]
        for symbol, band_idxs in mapping.band_map.items():
            if symbol in params:
                continue
            params[symbol] = _band_param(
                ds_by_region[region], band_idxs, scale_by_region[region])
        for key, val in mapping.constants.items():
            constants.setdefault(key, val)
    params.update(constants)

    mapping_info = {
        "constants": constants,
        "regions": {
            region: {
                "source": product["sources"][region].as_posix(),
                "scale_factor": scale_by_region[region],
                "band_map": mapping_by_region[region].band_map,
                "band_wavelengths_nm": {
                    sym: [mapping_by_region[region].wavelengths[b] for b in idxs]
                    for sym, idxs in mapping_by_region[region].band_map.items()},
            } for region in mapping_by_region},
    }
    return params, mapping_info, ref_ds, handles


# ==================================================================================
def _band_param(
        ds: xr.DataArray,
        band_idxs: List[int],
        scale: float,
    ) -> xr.DataArray:
    """Return one symbol's lazy 2-D reflectance array.

    Selects (``Peak``) or averages (``Mean``) the raster bands, applies
    the reflectance scale factor, and masks values outside ``(0, 1]``
    to NaN (zeros are the orthomosaic nodata fill).

    Parameters
    ----------
    ds : xarray.DataArray
        Lazily opened raster with a 1-based ``band`` coordinate.
    band_idxs : list of int
        1-based band indices for this symbol.
    scale : float
        Reflectance scale divisor (10000 for integer GRYFN orthos).

    Returns
    -------
    xarray.DataArray
        Lazy 2-D float32 reflectance array.
    """
    if len(band_idxs) == 1:
        da = ds.sel(band=band_idxs[0], drop=True)
    else:
        da = ds.sel(band=band_idxs).mean(dim="band")
    da = (da / scale).astype(np.float32)
    return da.where((da > 0) & (da <= 1), np.nan)


# ==================================================================================
def _select_indices(
        product: Dict[str, Any],
        mapping_info: Dict[str, Any],
        restrict: Optional[List[str]] = None,
    ) -> Tuple[List[str], Dict[str, List[str]], List[str]]:
    """Decide which indices this product computes.

    For the combined ``VNIRSWIR`` product, indices computable from the
    VNIR bands alone are delegated to the (higher-resolution) VNIR
    product rather than recomputed on the coarser SWIR grid.

    Parameters
    ----------
    product : dict
        ``{"region": str, "sources": ...}``.
    mapping_info : dict
        Mapping provenance from :func:`build_params`.
    restrict : list of str, optional
        Curated index list (``--indices``). Default None (all).

    Returns
    -------
    list of str
        Indices to compute in this product.
    dict of str to list of str
        Index → missing band symbols (not computable at all).
    list of str
        Indices delegated to the VNIR-only product (``VNIRSWIR`` only).
    """
    all_symbols = [s for region in mapping_info["regions"].values()
                   for s in region["band_map"]]
    valid, skipped = si.computable_indices(
        sorted(set(all_symbols)), mapping_info["constants"], restrict=restrict)

    delegated: List[str] = []
    if product["region"] == "VNIRSWIR":
        vnir_symbols = list(mapping_info["regions"]["VNIR"]["band_map"])
        vnir_valid, _ = si.computable_indices(
            vnir_symbols, mapping_info["constants"], restrict=restrict)
        delegated = [i for i in valid if i in set(vnir_valid)]
        valid = [i for i in valid if i not in set(vnir_valid)]
    return valid, skipped, delegated


# ==================================================================================
def _memory_chunks(
        ref_ds: xr.DataArray,
        valid: List[str],
        cfg: SIConfig,
    ) -> List[List[str]]:
    """Split the index list into chunks that fit the memory budget.

    Each computed index map is a float32 array on the output grid; the
    chunk size is chosen so one chunk of maps (plus safety factor 2 for
    dask intermediates) fits in available memory minus the headroom.

    Parameters
    ----------
    ref_ds : xarray.DataArray
        Reference array defining the output grid size.
    valid : list of str
        Indices slated for computation.
    cfg : SIConfig
        Active configuration (memory headroom).

    Returns
    -------
    list of list of str
        Ordered chunks of index names.
    """
    bytes_per_index = float(ref_ds.sizes["x"]) * float(ref_ds.sizes["y"]) * 4
    budget = max(psutil.virtual_memory().available - cfg.mem_headroom_bytes, 4e9)
    per_chunk = max(1, int(budget / (2 * bytes_per_index)))
    if per_chunk >= len(valid):
        return [valid]
    n_chunks = int(np.ceil(len(valid) / per_chunk))
    return [part.tolist() for part in np.array_split(np.array(valid), n_chunks)]


# ==================================================================================
def _compute_chunk(
        chunk: List[str],
        params: Dict[str, Any],
        job: Dict[str, Any],
        ref_ds: xr.DataArray,
        product: Dict[str, Any],
        method: str,
    ) -> xr.Dataset:
    """Compute one chunk of index maps into an in-memory dataset.

    Parameters
    ----------
    chunk : list of str
        Index names to compute.
    params : dict
        Spyndex parameter mapping from :func:`build_params`.
    job : dict
        Run job (metadata for the time coordinate and attrs).
    ref_ds : xarray.DataArray
        Reference array carrying the output grid's CRS/transform.
    product : dict
        Product description (source paths for provenance).
    method : str
        Band aggregation method (attrs provenance).

    Returns
    -------
    xarray.Dataset
        One float32 variable per index with a length-1 ``time``
        dimension and spatial reference coordinates.
    """
    tqdm.write(f"Computing {len(chunk)} indices starting at {pd.Timestamp.now()}")
    da_idx = spyndex_compute(chunk, params)
    with warn.catch_warnings():
        warn.simplefilter("ignore")  # spyndex formulas divide by zero on nodata
        with ProgressBar():
            da_idx = da_idx.compute()

    ds_idx = da_idx.to_dataset(dim="index")
    ts = job["date"] if pd.notna(job["date"]) else pd.Timestamp.now().normalize()
    ds_idx = ds_idx.expand_dims({"time": [ts]})
    ds_idx.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=True)
    ds_idx["spatial_ref"] = ref_ds.spatial_ref
    ds_idx.rio.write_crs(ref_ds.rio.crs, inplace=True)
    ds_idx.rio.write_transform(ref_ds.rio.transform(), inplace=True)

    sources = ", ".join(p.as_posix() for p in product["sources"].values())
    # Drop attrs inherited from the source raster (per-band ENVI tags are
    # misleading on an index product) and attach per-index provenance.
    ds_idx.attrs = {}
    for var in ds_idx.data_vars:
        ind_info = spyndex.indices[str(var)]
        ds_idx[var].attrs = {
            "long_name": ind_info.long_name or "",
            "formula": ind_info.formula or "",
            "reference": ind_info.reference or "",
        }
    ds_idx.attrs.update({
        "description": "Spectral indices computed with spyndex",
        "selection_method": method,
        "source_files": sources,
        "indices": ",".join(chunk),
        "history": (f"{pd.Timestamp.now()}, (user: {os.environ.get('USER', 'unknown')}), "
                    f"{__file__} {__version__}, computed from {sources}"),
    })
    return ds_idx


# ==================================================================================
def spyndex_compute(chunk: List[str], params: Dict[str, Any]) -> xr.DataArray:
    """Evaluate spyndex indices, guaranteeing an ``index`` dimension.

    Parameters
    ----------
    chunk : list of str
        Index names to compute.
    params : dict
        Spyndex parameter mapping (lazy band arrays + constants).

    Returns
    -------
    xarray.DataArray
        Lazy stacked result with an ``index`` dimension.
    """
    da = spyndex.computeIndex(index=list(chunk), params=params)
    da = da.astype(np.float32)
    if "index" not in da.dims:
        da = da.expand_dims(index=list(chunk))
    return da


# ==================================================================================
def _write_index_maps(
        ds_idx: xr.Dataset,
        outdir: pathlib.Path,
        stem: str,
        indx: int,
        total: int,
        file_format: str,
    ) -> List[pathlib.Path]:
    """Write one computed chunk to disk.

    Parameters
    ----------
    ds_idx : xarray.Dataset
        Computed index maps (one variable per index).
    outdir : pathlib.Path
        Output folder (``T1_proc/SpectralIndices/``).
    stem : str
        Product file stem (``SI_{region}_{method}[_gproN]``).
    indx : int
        Zero-based chunk counter.
    total : int
        Total number of chunks for this product.
    file_format : str
        ``"netcdf"`` (single file, or ``_partNNofMM`` files when
        ``total > 1``) or ``"geotiff"`` (one single-band tiled GTiff
        per index under ``{stem}_GTiff/``).

    Returns
    -------
    list of pathlib.Path
        Files written.
    """
    produced: List[pathlib.Path] = []
    if file_format == "geotiff":
        tif_dir = outdir / f"{stem}_GTiff"
        tif_dir.mkdir(parents=True, exist_ok=True)
        for var in tqdm(list(ds_idx.data_vars), desc="Writing GTiffs", leave=False):
            fpath = tif_dir / f"{var}.tif"
            ds_idx[var].squeeze("time", drop=True).rio.to_raster(
                fpath, compress="DEFLATE", tiled=True)
            produced.append(fpath)
        return produced

    # +++++ NetCDF: part files only when the memory budget forced chunking +++++
    if total > 1:
        fpath = outdir / f"{stem}_part{indx + 1:02d}of{total:02d}.nc"
    else:
        fpath = outdir / f"{stem}.nc"
    tqdm.write(f"Writing {len(ds_idx.data_vars)} indices to {fpath.name} "
               f"starting at {pd.Timestamp.now()}")
    # grid_mapping must ride in the explicit encoding: passing encoding= to
    # to_netcdf replaces each variable's stored .encoding, silently dropping
    # the CRS association write_crs created (readers then see crs=None).
    ds_idx.to_netcdf(
        fpath, engine="h5netcdf",
        encoding={var: {"zlib": True, "complevel": 4, "_FillValue": np.nan,
                        "grid_mapping": "spatial_ref"}
                  for var in ds_idx.data_vars})
    produced.append(fpath)
    return produced


# ==================================================================================
def _index_stats(ds_idx: xr.Dataset) -> List[Dict[str, Any]]:
    """Compute per-index raster statistics for the overview report.

    Parameters
    ----------
    ds_idx : xarray.Dataset
        Computed index maps (one variable per index).

    Returns
    -------
    list of dict
        Per index: ``index``, ``min``, ``median``, ``max``,
        ``valid_fraction`` (finite pixels / total pixels).
    """
    stats: List[Dict[str, Any]] = []
    for var in ds_idx.data_vars:
        arr = ds_idx[var].values
        finite = np.isfinite(arr)
        n_valid = int(finite.sum())
        if n_valid == 0:
            stats.append({"index": str(var), "min": np.nan, "median": np.nan,
                          "max": np.nan, "valid_fraction": 0.0})
            continue
        vals = arr[finite]
        stats.append({
            "index": str(var),
            "min": float(vals.min()),
            "median": float(np.median(vals)),
            "max": float(vals.max()),
            "valid_fraction": float(n_valid / arr.size)})
    return stats


# ==================================================================================
def _collect_headline(
        ds_idx: xr.Dataset,
        headline: Dict[str, Any],
        cfg: SIConfig,
    ) -> None:
    """Stash downsampled data for the overview figures.

    Chunks are written and freed one at a time, so the figure inputs
    (a value sample for the histograms, a strided thumbnail for the
    first headline index) are captured while each chunk is in memory.

    Parameters
    ----------
    ds_idx : xarray.Dataset
        Computed index maps for the current chunk.
    headline : dict
        Mutable collector: ``{"hist": {index: 1-D array},
        "thumb": (index, 2-D array)}`` filled in place.
    cfg : SIConfig
        Active configuration (headline set, sample sizes).
    """
    headline.setdefault("hist", {})
    rng = np.random.default_rng(42)
    for var in cfg.headline_indices:
        if var not in ds_idx.data_vars or var in headline["hist"]:
            continue
        arr = ds_idx[var].values
        vals = arr[np.isfinite(arr)]
        if vals.size == 0:
            continue
        if vals.size > cfg.hist_sample_size:
            vals = rng.choice(vals, size=cfg.hist_sample_size, replace=False)
        headline["hist"][var] = vals.astype(np.float32)
        if "thumb" not in headline:
            plane = np.squeeze(arr)
            step = max(1, int(np.ceil(max(plane.shape) / cfg.thumbnail_max_px)))
            headline["thumb"] = (var, plane[::step, ::step].copy())


# ==================================================================================
def _write_figures(
        headline: Dict[str, Any],
        fig_dir: pathlib.Path,
        stem: str,
        cfg: SIConfig,
    ) -> List[pathlib.Path]:
    """Write the headline histogram grid and index-map thumbnail.

    Figure filenames never contain ``%`` (it breaks the VS Code
    markdown preview).

    Parameters
    ----------
    headline : dict
        Collector filled by :func:`_collect_headline`.
    fig_dir : pathlib.Path
        Figure output folder (``SI_figures/``).
    stem : str
        Product file stem used to prefix the figure names.
    cfg : SIConfig
        Active configuration.

    Returns
    -------
    list of pathlib.Path
        Figures written (may be empty when no headline index was
        computable).
    """
    figures: List[pathlib.Path] = []
    hists = headline.get("hist", {})
    if not hists:
        return figures
    matplotlib.use("Agg")
    fig_dir.mkdir(parents=True, exist_ok=True)

    # ========== Histogram grid ==========
    n = len(hists)
    ncols = min(4, n)
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(4 * ncols, 3 * nrows), squeeze=False)
    for ax, (var, vals) in zip(axes.flat, hists.items()):
        ax.hist(vals, bins=100, color="tab:green")
        ax.set_title(var)
    for ax in axes.flat[n:]:
        ax.set_visible(False)
    fig.suptitle(f"{stem} headline index distributions (sampled pixels)")
    fig.tight_layout()
    hist_path = fig_dir / f"{stem}_histograms.png"
    fig.savefig(hist_path, dpi=150)
    plt.close(fig)
    figures.append(hist_path)

    # ========== Thumbnail for the first headline index ==========
    if "thumb" in headline:
        var, plane = headline["thumb"]
        fig, ax = plt.subplots(figsize=(8, 8))
        im = ax.imshow(plane, cmap="RdYlGn", vmin=-1, vmax=1)
        ax.set_title(f"{stem} {var} (downsampled)")
        ax.set_axis_off()
        fig.colorbar(im, ax=ax, shrink=0.7, label=var)
        thumb_path = fig_dir / f"{stem}_{var}_map.png"
        fig.savefig(thumb_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        figures.append(thumb_path)
    return figures


# ==================================================================================
def _build_report(
        job: Dict[str, Any],
        product: Dict[str, Any],
        method: str,
        mapping_info: Dict[str, Any],
        valid: List[str],
        skipped: Dict[str, List[str]],
        delegated: List[str],
        produced: List[pathlib.Path],
        figures: List[pathlib.Path],
        stats: List[Dict[str, Any]],
        cfg: SIConfig,
    ) -> Dict[str, Any]:
    """Assemble the machine-readable product manifest.

    Parameters
    ----------
    job : dict
        Run job from :func:`locate_orthomosaics`.
    product : dict
        Product description.
    method : str
        Band aggregation method.
    mapping_info : dict
        Mapping provenance from :func:`build_params`.
    valid, delegated : list of str
        Indices computed / delegated to the VNIR product.
    skipped : dict of str to list of str
        Index → missing band symbols.
    produced, figures : list of pathlib.Path
        Files written (index maps / figures).
    stats : list of dict
        Per-index raster statistics.
    cfg : SIConfig
        Active configuration (schema version).

    Returns
    -------
    dict
        JSON-serialisable report. File paths are stored relative to
        the report's folder so the container stays portable.
    """
    outdir = job["outdir"]
    return {
        "schema_version": cfg.schema_version,
        "generated": str(pd.Timestamp.now()),
        "script": f"{__title__} {__version__}",
        "meta": {k: str(job[k].date() if k == "date" and pd.notna(job[k]) else job[k])
                 for k in ["node", "project", "site", "sensor", "date", "run"]},
        "region": product["region"],
        "method": method,
        "gpro_nu": job["gpro_nu"],
        "sources": {r: p.as_posix() for r, p in product["sources"].items()},
        "band_mapping": mapping_info,
        "indices_computed": valid,
        "indices_skipped": skipped,
        "indices_delegated_to_VNIR": delegated,
        "index_maps": [p.relative_to(outdir).as_posix() for p in produced],
        "figures": [p.relative_to(outdir).as_posix() for p in figures],
        "index_stats": stats,
    }


# ==================================================================================
def _write_markdown_report(
        report: Dict[str, Any],
        md_path: pathlib.Path,
        outdir: pathlib.Path,
    ) -> None:
    """Write the human-readable overview report.

    Uses relative-path figure embeds so the report renders in the
    VS Code / GitHub markdown preview from inside the run folder.

    Parameters
    ----------
    report : dict
        Manifest from :func:`_build_report`.
    md_path : pathlib.Path
        Output ``.md`` path.
    outdir : pathlib.Path
        Folder the manifest's relative paths are anchored to.
    """
    meta = report["meta"]
    lines = [
        f"# Spectral index overview — {meta['project']} / {meta['site']} / "
        f"{meta['sensor']} / {meta['date']} / {meta['run']}",
        "",
        f"- **Product**: {report['region']} ({report['method']} band aggregation)",
        f"- **Generated**: {report['generated']} by {report['script']}",
        f"- **Sources**: " + ", ".join(f"`{p}`" for p in report["sources"].values()),
        f"- **Indices computed**: {len(report['indices_computed'])}"
        f" | **skipped (missing bands)**: {len(report['indices_skipped'])}"
        f" | **delegated to VNIR product**: {len(report['indices_delegated_to_VNIR'])}",
        f"- **Index maps**: " + ", ".join(f"`{p}`" for p in report["index_maps"]),
        "",
    ]

    # ========== Figures ==========
    for fig in report["figures"]:
        lines += [f"![{pathlib.Path(fig).stem}]({fig})", ""]

    # ========== Per-index stats table ==========
    lines += ["## Per-index raster statistics", "",
              "| Index | Min | Median | Max | Valid fraction |",
              "|---|---|---|---|---|"]
    for s in report["index_stats"]:
        lines.append(
            f"| {s['index']} | {s['min']:.4g} | {s['median']:.4g} | "
            f"{s['max']:.4g} | {s['valid_fraction']:.3f} |")

    # ========== Skipped indices ==========
    if report["indices_skipped"]:
        lines += ["", "## Skipped indices (missing bands)", "",
                  "| Index | Missing band symbols |", "|---|---|"]
        for ind, missing in report["indices_skipped"].items():
            lines.append(f"| {ind} | {', '.join(missing)} |")
    if report["indices_delegated_to_VNIR"]:
        lines += ["", "## Delegated to the VNIR product", "",
                  "Computable from VNIR bands alone, so produced at full VNIR "
                  "resolution in the `SI_VNIR_*` maps instead of this coarser grid:",
                  "", ", ".join(report["indices_delegated_to_VNIR"])]

    md_path.write_text("\n".join(lines) + "\n")


# ==================================================================================
def _product_stem(region: str, method: str, gpro_nu: Optional[int]) -> str:
    """Return the stable file stem for one product.

    Parameters
    ----------
    region : str
        ``VNIR``, ``SWIR``, or ``VNIRSWIR``.
    method : str
        Band aggregation method.
    gpro_nu : int or None
        ``.gpro`` index for multi-gpro debugging runs.

    Returns
    -------
    str
        ``SI_{region}_{method}[_gproN]``.
    """
    stem = f"SI_{region}_{method}"
    if gpro_nu is not None:
        stem += f"_gpro{gpro_nu}"
    return stem


# ==================================================================================
def _product_up_to_date(
        report_path: pathlib.Path,
        sources: List[pathlib.Path],
        args: argparse.Namespace,
    ) -> bool:
    """Check whether a product's report and all its files are current.

    Parameters
    ----------
    report_path : pathlib.Path
        The product's manifest JSON.
    sources : list of pathlib.Path
        Source orthomosaic(s).
    args : argparse.Namespace
        Parsed command-line arguments; the cached product must have
        been produced with the same ``--indices`` set and ``--format``.

    Returns
    -------
    bool
        True when the report exists, matches the requested index set
        and format, and every file it lists is newer than every source.
    """
    if not report_path.is_file():
        return False
    try:
        report = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    requested = sorted(args.indices) if args.indices else "all"
    if report.get("requested_indices") != requested or report.get("format") != args.format:
        return False
    listed = [report_path.parent / p
              for p in report.get("index_maps", []) + report.get("figures", [])]
    return cf.outputs_up_to_date([report_path] + listed, sources)


# ==================================================================================
def _print_run_summary(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """Print and return the end-of-run summary table.

    Parameters
    ----------
    rows : list of dict
        Summary rows accumulated by :func:`process_product`.

    Returns
    -------
    pandas.DataFrame
        One row per run/region/method.
    """
    if not rows:
        print("\nNo products were processed.")
        return pd.DataFrame()
    summary = pd.DataFrame(rows)
    print("\n================ SPECTRAL INDEX SUMMARY ================")
    print(summary.to_string(index=False))
    reported = (summary["status"] == "reported").sum()
    print(f"\n{reported} product(s) computed, "
          f"{(summary['status'] != 'reported').sum()} skipped.")
    return summary


# ==================================================================================
if __name__ == '__main__':
    # ========== Parse args ==========
    parser = argparse.ArgumentParser(description="Compute spyndex spectral index maps from hyperspectral orthomosaics (DS05).")
    parser.add_argument("--path", type=str, default=None, help="The path of the folder to crawl for orthomosaics. By default it will search from the root dir of the git repo.")
    parser.add_argument("-f", "--force", default=False, action="store_true", help="Force recomputation even when outputs are newer than the source orthomosaics.")
    parser.add_argument("--method", type=str, default="Mean", choices=["Peak", "Mean", "both"], help="Band aggregation method: Mean averages every band inside the symbol's wavelength window (default; closer to what a broadband sensor integrates); Peak selects the single band nearest each spyndex symbol's nominal centre; both computes the two variants.")
    parser.add_argument("--indices", type=str, nargs="+", default=None, help="Restrict computation to a curated list of spyndex index names (e.g. --indices NDVI NDRE EVI). Default is every index computable from the sensor's bands.")
    parser.add_argument("--format", type=str, default="netcdf", choices=["netcdf", "geotiff"], help="Index-map output format. netcdf (default) writes one compressed file per product (split into part files when memory-bound); geotiff writes one single-band tiled GTiff per index.")
    parser.add_argument("--resample-method", type=str, default="nearest", choices=["nearest", "linear"], help="Interpolation used to resample VNIR bands onto the SWIR grid for the combined VNIRSWIR product. Default nearest.")
    parser.add_argument("-s", "--skipplot", default=False, action="store_true", help="Skip the figure generation. Index maps and reports are still produced.")
    parser.add_argument("-v", "--verbose", default=False, action="store_true", help="Enable verbose output for debugging.")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[], help="One or more directory names to exclude from the crawl. e.g. --exclude-dir 2025_TestData")
    parser.add_argument("--allow-multi-gpro", default=False, action="store_true", help="Process runs that contain more than one .gpro folder instead of skipping them (outputs get _gproN suffixes). Debugging only: the product set is ambiguous.")

    args = parser.parse_args()

    # +++++ Resolve the search path: --path wins, else the cwd's git root +++++
    if args.path is not None:
        search_root = args.path
    else:
        try:
            search_root = git.Repo(
                os.getcwd(), search_parent_directories=True
            ).git.rev_parse("--show-toplevel")
        except git_exc.InvalidGitRepositoryError as err:
            raise git_exc.InvalidGitRepositoryError(
                f"This script was called from an unknown path ({os.getcwd()}). Must be in a git repo or provide a valid --path argument. Original error: {err}"
            ) from err

    # ========= Check if the provided path exists ==========
    # Resolve to absolute BEFORE chdir, otherwise a relative --path breaks.
    path = pathlib.Path(search_root).resolve()
    if not path.is_dir():
        raise NotADirectoryError(f"The provided path does not exist: {path}")
    os.chdir(path)

    # ========== Parse Args to main function ==========
    cf.check_environment(_git_root)
    main(args, path)
