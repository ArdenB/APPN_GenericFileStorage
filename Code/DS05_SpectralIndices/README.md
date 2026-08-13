# DS05 — Spectral index calculation

Computes spyndex spectral index maps from the hyperspectral orthomosaics
produced by the GRYFN processing chain. **Raster in → raster out**:
scripts here never open plot geojson or parquet — all plot-level
extraction from these index maps lives in DS03 (`PE02`).

| Script | Purpose |
|---|---|
| `SI00_SpectralIndices.py` | Crawl runs, map sensor bands → spyndex symbols, compute every computable index, write per-run index maps + reports |

## SI00 usage

```bash
python Code/DS05_SpectralIndices/SI00_SpectralIndices.py --path <node_or_project_folder>
```

Key arguments (see `--help` for all):

- `--method {Peak,Mean,both}` — band aggregation (default `Peak`: single
  band nearest each spyndex symbol's nominal centre; `Mean` averages every
  band inside the symbol's wavelength window).
- `--indices NDVI NDREI …` — curated index list (default: **all** indices
  computable from the sensor's bands).
- `--format {netcdf,geotiff}` — NetCDF default; GTiff writes one
  single-band tiled file per index.
- `--force`, `--exclude-dir`, `--allow-multi-gpro`, `--skipplot` — same
  semantics as the DS02 QA scripts.

## Outputs (per run × EM region × method)

Written to `<run>/T1_proc/SpectralIndices/` (Tier 1; `T2_traits/` is
reserved for ML-derived products):

- `SI_{region}_{method}[_gproN].nc` — one variable per index, compressed
  NetCDF with `time` dim and CRS/transform. Split into
  `_partNNofMM.nc` files when the index stack exceeds the memory budget.
  Open with `xr.open_dataset(path, decode_coords="all")` so the CRS
  decodes.
- `SI_{region}_{method}[_gproN]_report.json` — manifest: sources, band
  mapping (symbol → band indices + wavelengths), indices
  computed/skipped/delegated, produced files, per-index stats. **PE02
  and the caching check consume this** — treat it as the product schema.
- `SI_{region}_{method}[_gproN]_overview.md` — human overview: stats
  table, skipped-index table, embedded figures (relative paths, no `%`
  in filenames).
- `SI_figures/` — headline-index histogram grid + map thumbnail.

Products: GOBI → `VNIR`. CALVIS → `VNIR` (full VNIR index set at native
resolution) + `VNIRSWIR` (VNIR resampled onto the SWIR grid; holds only
the indices that need a SWIR band — VNIR-only indices are *delegated* to
the finer VNIR product, listed in the report).

Caching: a product is skipped when its report matches the requested
index set/format and every file the report lists is newer than the
source orthomosaic(s) (`--force` overrides).

## Band → spyndex mapping

Shared helper module `Code/functions/spectral_indices/` (band-wavelength
reading lives in `Code/functions/core_functions/band_wavelengths.py`,
shared with QA00/PE01):

- `cf.band_wavelengths(path)` — per-band centre wavelengths from the
  GDAL band tags (ENVI `.hdr` `wavelength` block).
- `spectral_band_definitions()` — the standard symbol table: symbol,
  wavelength window, nominal centre.
- `map_bands_to_spyndex(wavelengths, method)` — symbol → band indices +
  resolved `lambdaX`/default constants.
- `computable_indices(symbols, constants, restrict)` — which of the 280
  catalogue indices the available bands support, plus per-index missing
  bands.

**Future feature (not in scope)**: matching-sensor indices — pass
another sensor's band widths (min/max/peak table) through the
`definitions` argument of `map_bands_to_spyndex` to produce indices
band-matched to that sensor (e.g. Sentinel-2-like NDVI from GOBI).
The DS05 TODO list (new sensor types — M3M multispectral, satellite
scenes, other `.gpro` sensors — and the cross-sensor band matching)
lives in the node-side repo's DS05 README; features are ported here
as they land.

## Dependencies

Beyond the DS02 environment (numpy, pandas, xarray, rioxarray,
matplotlib, tqdm, gitpython):

```bash
conda install -c conda-forge spyndex dask h5netcdf psutil rasterio
```

## System Requirements

- Python 3.12+
- Must be run from within an APPN folder structure git repository or
  with the `--path` argument specified
- Dataset must follow the APPN folder structure (orthomosaics at
  `<run>/T1_proc/*.gpro/products/*_{VNIR|SWIR}_Orthomosaic.bin`)
