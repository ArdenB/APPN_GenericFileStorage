"""Spectral index calculation from satellite SR mosaics (DS05).

Crawls an APPN dataset tree for satellite surface-reflectance mosaics
(``<run>/T1_proc/05_Mosaic/Multi_L2R/*_SR_*_Multi_FinalRev.tif``, the
MAXAR/JILIN keeper products) and computes every spyndex spectral index
the sensor's native bands support, writing per-run index maps into
``<run>/T1_proc/SpectralIndices/`` (or a scratch mirror via
``--outdir`` during development — the Tier2 archive is a replicated
pair and must not be written to outside the coordinated production
run).

Satellite runs use **native bands** — no Peak/Mean wavelength-window
aggregation (the bands are broadband and fixed per sensor) and no
per-platform band tables: every JILIN platform is 4-band ``B,G,R,N``
and MAXAR is 8-band ``A,B,G,Y,R,RE1,RE2,N``, so the symbol→band
mapping is static data (see :func:`satellite_band_definitions`).
Nominal band centres feed the ``lambdaX`` constants used by the few
wavelength-dependent indices (``DVIplus``, ``NDGI``, ``NIRvH2``);
those indices are flagged in the report. Pixel values are vendor SR
reflectance scaled by 20000 (verified empirically across all 20
archive runs, 2026-08-21 — see ``SATELLITE_PORT_HANDOVER.md``).

Runs whose ``05_Mosaic`` holds no usable multispectral keeper (the
superseded halves of dual-date pairs, and the pansharpened-only
Arthursleigh/Camden/Nowley sites) are skipped and surfaced in the
end-of-run summary — never silently worked around.

Outputs per run match the SI00 product schema exactly (PE02 and the
caching check consume the ``_report.json``): the index NetCDF (or
GTiffs), ``_report.json``, ``_overview.md``, and ``SI_figures/``. The
output machinery is imported from ``SI00_SpectralIndices`` so the two
scripts cannot drift apart.

Command-line Arguments
----------------------
--path : str
    Folder to crawl for satellite mosaics (e.g. the project or site
    folder of the SIFsat archive).
--outdir : str, optional
    Scratch root for outputs. When set, each run's outputs are written
    to ``<outdir>/<site>/<sensor>/<date>/<run>/T1_proc/SpectralIndices/``
    instead of in place. Use during development; omit only for the
    operator-coordinated production run.
--indices : str [str ...], optional
    Restrict to a curated list of spyndex index names. Default is
    every index computable from the sensor's bands.
--format : str, optional
    ``netcdf`` (default) or ``geotiff`` (one single-band file per
    index).
"""

# ==============================================================================

__title__ = "Satellite spectral index calculation"
__author__ = "Arden Burrell"
__version__ = "v1.0(21.08.2026)"
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
import spyndex
import xarray as xr
import rioxarray
from tqdm import tqdm
import warnings as warn

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

# +++++ Reuse SI00's output machinery so the product schema cannot drift (P9) +++++
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from SI00_SpectralIndices import (  # noqa: E402
    _build_report,
    _collect_headline,
    _index_stats,
    _memory_chunks,
    _print_run_summary,
    _product_stem,
    _product_up_to_date,
    _write_figures,
    _write_index_maps,
    _write_markdown_report,
    spyndex_compute,
)
from dask.diagnostics.progress import ProgressBar  # noqa: E402


# ==================================================================================
@dataclass(frozen=True)
class SatSIConfig:
    """Tunable settings for one satellite spectral-index invocation.

    Attribute names shared with ``SI00.SIConfig`` (``schema_version``,
    ``mem_headroom_bytes``, ``headline_indices``, ``hist_sample_size``,
    ``thumbnail_max_px``) are duck-typed into the reused SI00 helpers.

    Attributes
    ----------
    schema_version : float
        Report/manifest schema version (matches SI00).
    valid_sensors : tuple of str
        Satellite platform folder names handled by this script.
    output_dirname : str
        Name of the per-run output folder inside ``T1_proc/``.
    figures_dirname : str
        Name of the figure folder inside the output folder.
    sat_scale : float
        Vendor SR reflectance scale divisor. 20000 for both MAXAR and
        JILIN FinalRev products (verified empirically 2026-08-21).
    keeper_glob : str
        Glob selecting the keeper mosaic inside ``Multi_L2R/``.
    mem_headroom_bytes : float
        Memory kept free when sizing index-computation chunks.
    headline_indices : tuple of str
        Preferred indices for the overview figures.
    hist_sample_size : int
        Max finite pixels sampled per index for the histogram figure.
    thumbnail_max_px : int
        Max thumbnail dimension (pixels).
    """
    schema_version: float = 1.0
    valid_sensors: Tuple[str, ...] = ("MAXAR", "JILIN")
    output_dirname: str = "SpectralIndices"
    figures_dirname: str = "SI_figures"
    sat_scale: float = 20000.0
    keeper_glob: str = "*_SR_*_Multi_FinalRev.tif"
    mem_headroom_bytes: float = 8e9
    headline_indices: Tuple[str, ...] = (
        "NDVI", "GNDVI", "NDREI", "EVI", "SAVI", "NDWI", "NDMI", "NBR")
    hist_sample_size: int = 200_000
    thumbnail_max_px: int = 1024


def default_config() -> SatSIConfig:
    """Return the default :class:`SatSIConfig` for this tool."""
    return SatSIConfig()


# ==================================================================================
def main(
        args: argparse.Namespace,
        path: pathlib.Path,
    ) -> pd.DataFrame:
    """Run the satellite spectral index calculation pipeline.

    Crawls the provided path for satellite SR keeper mosaics, maps each
    sensor's native bands to spyndex symbols, computes the index maps
    per run, and writes the maps, reports, and figures.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments.
    path : pathlib.Path
        Root directory to search for satellite mosaics.

    Returns
    -------
    pandas.DataFrame
        One row per run (computed or skipped) summarising the outcome.
    """
    cfg = default_config()

    # ========== Find the keeper mosaics and note unusable runs ==========
    jobs, skipped_runs = locate_satellite_mosaics(
        path, cfg, outdir_root=args.outdir, exclude_dirs=args.exclude_dir,
        verbose=args.verbose)

    # ========== Compute the index maps for every run ==========
    summary_rows: List[Dict[str, Any]] = list(skipped_runs)
    for job in tqdm(jobs, total=len(jobs), desc="Processing runs"):
        summary_rows.append(process_run(job, args, cfg))

    # ========== Print the end-of-run summary (skips listed alongside) ==========
    summary = _print_run_summary(summary_rows)
    return summary


# ==================================================================================
def locate_satellite_mosaics(
        path: pathlib.Path,
        cfg: SatSIConfig,
        outdir_root: Optional[str] = None,
        exclude_dirs: Optional[List[str]] = None,
        verbose: bool = False,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Find satellite keeper mosaics and classify unusable runs.

    Searches ``path`` for ``<run>/T1_proc/05_Mosaic`` folders belonging
    to the configured satellite sensors. A run is processable when its
    ``Multi_L2R/`` holds exactly one keeper
    (``*_SR_*_Multi_FinalRev.tif``); anything else is returned as a
    skipped row so the gap is surfaced, never silently worked around.

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively.
    cfg : SatSIConfig
        Active configuration (valid sensors, keeper glob).
    outdir_root : str, optional
        Scratch output root. When set, each job's ``outdir`` mirrors
        the ``<site>/<sensor>/<date>/<run>`` structure under it instead
        of pointing into the archive. Default None (in-place).
    exclude_dirs : list of str, optional
        Directory names to exclude from the crawl. Default None.
    verbose : bool, optional
        Print extra diagnostic messages. Default False.

    Returns
    -------
    list of dict
        Processable jobs (keys match the SI00 job shape: ``run_dir``,
        ``node``, ``project``, ``site``, ``sensor``, ``date``, ``run``,
        ``gpro_nu``, ``orthos``, ``outdir``).
    list of dict
        Summary rows for skipped runs (``status`` explains why).

    Raises
    ------
    ValueError
        If no ``05_Mosaic`` folders are found, or a ``Multi_L2R``
        holds more than one keeper (ambiguous product set — resolve
        the archive before computing).
    """
    print(f"Scanning {path} for satellite mosaics. {pd.Timestamp.now()}")
    mosaic_dirs = [
        d for d in sorted(path.rglob("05_Mosaic"))
        if d.is_dir() and d.parent.name == "T1_proc"]
    if len(mosaic_dirs) == 0:
        raise ValueError(
            f"No 05_Mosaic folders found in {path}. Expected "
            "<run>/T1_proc/05_Mosaic for MAXAR/JILIN runs.")

    # ========== Filter out excluded directories ==========
    if exclude_dirs:
        exclude_set = set(exclude_dirs)
        mosaic_dirs = [d for d in mosaic_dirs
                       if not (set(p.name for p in d.parents) & exclude_set)]

    jobs: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for mosaic_dir in mosaic_dirs:
        run_dir = mosaic_dir.parents[1]
        parsed = cf.parse_APPN_dataset_path(run_dir)
        sensor = parsed["sensor"]
        if sensor not in cfg.valid_sensors:
            if verbose:
                tqdm.write(f"Skipping non-satellite sensor {sensor}: {run_dir}")
            continue

        row = {
            "project": parsed["project"], "site": parsed["site_folder"],
            "sensor": sensor,
            "date": parsed["date"].date() if pd.notna(parsed["date"]) else parsed["date"],
            "run": parsed["run_folder"], "region": "Multi", "method": "Native",
            "n_indices": 0, "n_skipped": 0}

        # ========== Keeper selection: explicit and assert-single ==========
        multi_l2r = mosaic_dir / "Multi_L2R"
        if not multi_l2r.is_dir():
            row["status"] = "skipped (no Multi_L2R multispectral SR)"
            skipped.append(row)
            continue
        keepers = sorted(multi_l2r.glob(cfg.keeper_glob))
        if len(keepers) == 0:
            row["status"] = "skipped (no _FinalRev keeper; superseded folder?)"
            skipped.append(row)
            continue
        if len(keepers) > 1:
            raise ValueError(
                f"Multiple keeper mosaics in {multi_l2r}: "
                f"{[k.name for k in keepers]}. Resolve the archive to one "
                "_FinalRev product before computing.")

        # ========== Resolve the output folder (scratch mirror or in place) ==========
        if outdir_root is not None:
            outdir = (pathlib.Path(outdir_root)
                      / parsed["site_folder"] / sensor
                      / parsed["date"].strftime("%Y%m%d") / parsed["run_folder"]
                      / "T1_proc" / cfg.output_dirname)
        else:
            outdir = run_dir / "T1_proc" / cfg.output_dirname

        jobs.append({
            "run_dir": run_dir,
            "node": parsed["node"],
            "project": parsed["project"],
            "site": parsed["site_folder"],
            "sensor": sensor,
            "date": parsed["date"],
            "run": parsed["run_folder"],
            "gpro_nu": None,
            "orthos": {"Multi": keepers[0]},
            "outdir": outdir,
        })
    return jobs, skipped


# ==================================================================================
def satellite_band_definitions(sensor: str) -> pd.DataFrame:
    """Return the static native-band table for a satellite sensor.

    One row per raster band, in band order. The symbol column maps each
    band to its spyndex band symbol; the nominal centre wavelength only
    feeds the ``lambdaX`` constants used by the few wavelength-dependent
    indices (``DVIplus``, ``NDGI``, ``NIRvH2``) — per-platform band
    tables are deliberately not used (all JILIN platforms share the
    same 4-band layout; see ``SATELLITE_PORT_HANDOVER.md``).

    Parameters
    ----------
    sensor : str
        ``"MAXAR"`` or ``"JILIN"``.

    Returns
    -------
    pandas.DataFrame
        Columns ``band`` (1-based raster index), ``standard`` (spyndex
        symbol), ``centre_nm`` (nominal band centre).

    Raises
    ------
    ValueError
        If ``sensor`` is not a supported satellite platform.
    """
    tables = {
        # WorldView-3 8-band ordering (vendor MAXAR_Summary.csv)
        "MAXAR": [
            (1, "A",   425.0),   # Coastal / aerosols
            (2, "B",   480.0),
            (3, "G",   545.0),
            (4, "Y",   605.0),
            (5, "R",   660.0),
            (6, "RE1", 705.0),
            (7, "RE2", 740.0),
            (8, "N",   832.5),
        ],
        # Every JILIN platform in the archive is 4-band B,G,R,N; centres
        # are cross-platform nominals (platform spread ≤ ~15 nm).
        "JILIN": [
            (1, "B", 480.0),
            (2, "G", 555.0),
            (3, "R", 660.0),
            (4, "N", 830.0),
        ],
    }
    if sensor not in tables:
        raise ValueError(
            f"Sensor {sensor!r} has no satellite band table; "
            f"supported: {sorted(tables)}")
    return pd.DataFrame(tables[sensor], columns=["band", "standard", "centre_nm"])


# ==================================================================================
def map_satellite_bands(sensor: str, n_bands: int) -> si.BandMapping:
    """Map a satellite raster's native bands onto spyndex symbols.

    Unlike the shared :func:`si.map_bands_to_spyndex` (which snaps
    wavelength metadata to symbols and is built for contiguous
    hyperspectral bands), this is a direct table lookup: band *i* is
    symbol *s*, full stop. The raster band count must match the table.

    Parameters
    ----------
    sensor : str
        ``"MAXAR"`` or ``"JILIN"``.
    n_bands : int
        Band count of the opened raster.

    Returns
    -------
    si.BandMapping
        ``method="Native"``, one band per symbol, spyndex default
        constants plus ``lambdaX`` for the sensor's symbols.

    Raises
    ------
    ValueError
        If the raster band count does not match the sensor table
        (real breakage — wrong product or corrupted mosaic).
    """
    table = satellite_band_definitions(sensor)
    if n_bands != len(table):
        raise ValueError(
            f"{sensor} raster has {n_bands} bands but the band table "
            f"defines {len(table)} ({table['standard'].tolist()}); refusing "
            "to guess a mapping.")

    # +++++ Constants: spyndex defaults + nominal lambdaX (no global mutation) +++++
    const_objs = spyndex.constants.to_dict()
    constants: Dict[str, float] = {
        k: float(c.default) for k, c in const_objs.items() if c.default is not None}
    band_map: Dict[str, List[int]] = {}
    wavelengths: Dict[int, float] = {}
    for _, row in table.iterrows():
        band_map[row["standard"]] = [int(row["band"])]
        wavelengths[int(row["band"])] = float(row["centre_nm"])
        if f"lambda{row['standard']}" in const_objs:
            constants[f"lambda{row['standard']}"] = float(row["centre_nm"])

    return si.BandMapping(
        method="Native", band_map=band_map, constants=constants,
        wavelengths=wavelengths)


# ==================================================================================
def process_run(
        job: Dict[str, Any],
        args: argparse.Namespace,
        cfg: SatSIConfig,
    ) -> Dict[str, Any]:
    """Compute, save, and report the index product for one run.

    Parameters
    ----------
    job : dict
        Run job from :func:`locate_satellite_mosaics`.
    args : argparse.Namespace
        Parsed command-line arguments.
    cfg : SatSIConfig
        Active configuration.

    Returns
    -------
    dict
        Summary row: run metadata plus ``n_indices``, ``n_skipped``,
        and ``status``.
    """
    product = {"region": "Multi", "sources": {"Multi": job["orthos"]["Multi"]}}
    method = "Native"
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
        tqdm.write(f"Skipping up-to-date product: {report_path}")
        report = json.loads(report_path.read_text())
        row.update({"n_indices": len(report.get("indices_computed", [])),
                    "n_skipped": len(report.get("indices_skipped", {})),
                    "status": "skipped (up to date)"})
        return row

    tqdm.write(f"Processing {job['sensor']} {job['site']} "
               f"{job['date'].date() if pd.notna(job['date']) else job['date']} "
               f"started at {pd.Timestamp.now()}")
    outdir.mkdir(parents=True, exist_ok=True)

    # ========== Map the native bands to spyndex symbols (lazy read) ==========
    params, mapping_info, ref_ds, handle = build_params(product, job["sensor"], cfg)
    try:
        # ========== Decide which indices to compute ==========
        mapping = mapping_info["regions"]["Multi"]
        valid, skipped = si.computable_indices(
            list(mapping["band_map"]), mapping_info["constants"],
            restrict=args.indices)
        if len(valid) == 0:
            warn.warn(f"No computable indices for {stem} in {job['run_dir']}.")
            row.update({"n_skipped": len(skipped),
                        "status": "skipped (no computable indices)"})
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
        handle.close()

    # ========== Figures + overview report + manifest ==========
    figures: List[pathlib.Path] = []
    if not args.skipplot:
        figures = _write_figures(headline, outdir / cfg.figures_dirname, stem, cfg)
    report = _build_report(job, product, method, mapping_info, valid, skipped,
                           [], produced, figures, stats, cfg)
    report["requested_indices"] = sorted(args.indices) if args.indices else "all"
    report["format"] = args.format
    # +++++ Flag the indices whose formula used the nominal lambda constants +++++
    report["indices_using_nominal_lambda"] = [
        i for i in valid
        if any(b.startswith("lambda") for b in spyndex.indices[i].bands)]
    _write_markdown_report(report, outdir / f"{stem}_overview.md", outdir)
    report_path.write_text(json.dumps(report, indent=2, default=str))
    tqdm.write(f"Wrote report: {report_path}")

    row.update({"n_indices": len(valid), "n_skipped": len(skipped),
                "status": "reported"})
    return row


# ==================================================================================
def build_params(
        product: Dict[str, Any],
        sensor: str,
        cfg: SatSIConfig,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], xr.DataArray, Any]:
    """Open the keeper mosaic lazily and build the spyndex parameters.

    Every entry in the returned parameter dictionary is a lazy (dask)
    2-D reflectance array, so only the bands an index actually needs
    are ever read from disk.

    Parameters
    ----------
    product : dict
        ``{"region": "Multi", "sources": {"Multi": keeper_path}}``.
    sensor : str
        Satellite platform (``"MAXAR"`` or ``"JILIN"``).
    cfg : SatSIConfig
        Active configuration (SR reflectance scale).

    Returns
    -------
    dict
        Spyndex parameter mapping: band symbol → lazy 2-D reflectance
        array, plus the resolved constants.
    dict
        Mapping provenance (SI00 report shape): band map, nominal
        wavelengths, constants, scale factor.
    xarray.DataArray
        Reference array used for memory estimates and spatial metadata.
    Any
        Open rioxarray handle; the caller must close it after
        computing.

    Raises
    ------
    ValueError
        If the raster has no CRS or its band count does not match the
        sensor's band table.
    """
    keeper: pathlib.Path = product["sources"]["Multi"]
    ds = rioxarray.open_rasterio(keeper, chunks={"band": 1})
    if ds.rio.crs is None:
        ds.close()
        raise ValueError(f"Raster {keeper} does not have a CRS defined.")
    mapping = map_satellite_bands(sensor, int(ds.sizes["band"]))

    # ========== Build the lazy per-symbol reflectance parameters ==========
    params: Dict[str, Any] = {}
    for symbol, band_idxs in mapping.band_map.items():
        da = ds.sel(band=band_idxs[0], drop=True)
        da = (da / cfg.sat_scale).astype(np.float32)
        # Zeros are the mosaic nodata fill; SR reflectance must sit in (0, 1].
        params[symbol] = da.where((da > 0) & (da <= 1), np.nan)
    params.update(mapping.constants)

    mapping_info = {
        "constants": mapping.constants,
        "regions": {
            "Multi": {
                "source": keeper.as_posix(),
                "scale_factor": cfg.sat_scale,
                "band_map": mapping.band_map,
                "band_wavelengths_nm": {
                    sym: [mapping.wavelengths[b] for b in idxs]
                    for sym, idxs in mapping.band_map.items()},
            }},
    }
    return params, mapping_info, ds, ds


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

    Mirrors SI00's chunk computation but records this script in the
    provenance history (the reused SI00 version would cite SI00).

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
        Band selection method (``"Native"``; attrs provenance).

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
if __name__ == '__main__':
    # ========== Parse args ==========
    parser = argparse.ArgumentParser(
        description="Compute spyndex spectral index maps from satellite "
                    "(MAXAR/JILIN) surface-reflectance mosaics (DS05).")
    parser.add_argument("--path", type=str, required=True, help="The folder to crawl for satellite mosaics (e.g. the SIFsat project or site folder).")
    parser.add_argument("--outdir", type=str, default=None, help="Scratch output root. When set, outputs mirror <site>/<sensor>/<date>/<run>/T1_proc/SpectralIndices/ under it instead of writing into the archive. Omit only for the operator-coordinated production run.")
    parser.add_argument("-f", "--force", default=False, action="store_true", help="Force recomputation even when outputs are newer than the source mosaic.")
    parser.add_argument("--indices", type=str, nargs="+", default=None, help="Restrict computation to a curated list of spyndex index names (e.g. --indices NDVI NDRE EVI). Default is every index computable from the sensor's bands.")
    parser.add_argument("--format", type=str, default="netcdf", choices=["netcdf", "geotiff"], help="Index-map output format. netcdf (default) writes one compressed file per product; geotiff writes one single-band tiled GTiff per index.")
    parser.add_argument("-s", "--skipplot", default=False, action="store_true", help="Skip the figure generation. Index maps and reports are still produced.")
    parser.add_argument("-v", "--verbose", default=False, action="store_true", help="Enable verbose output for debugging.")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[], help="One or more directory names to exclude from the crawl.")

    args = parser.parse_args()

    # ========= Check the provided path exists (resolve BEFORE chdir) ==========
    path = pathlib.Path(args.path).resolve()
    if not path.is_dir():
        raise NotADirectoryError(f"The provided path does not exist: {path}")
    if args.outdir is not None:
        args.outdir = str(pathlib.Path(args.outdir).resolve())

    # ========== chdir to git root (resolved at module top) ==========
    os.chdir(_git_root)

    main(args, path)
