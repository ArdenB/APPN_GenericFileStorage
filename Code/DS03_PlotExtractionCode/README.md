# DS03 — Plot Extraction Code

Scripts that extract per-plot values from processed (Tier 1) sensor
products using the site's **Plot_Layout** vector files
(`Documentation/Plot_Layout/{YYYYSiteName}_plots.geojson` — see the
[Key-Files wiki page](https://github.com/ArdenB/APPN_GenricFileStorage/wiki/Key-Files)).
All outputs are **Tier 1** products written to
`<run>/T1_proc/PlotExtracts/` (`T2_traits/` is reserved for
ML-model-derived products).

| Script | Input | Output |
|--------|-------|--------|
| `PE00_LIDAR_extraction.py` | `*_LiDAR_CombinedPointCloud.las/.laz` + DSM/DTM | `PE_LIDAR_points[…].parquet` + metadata YAML |
| `PE01_HyperspecPlotExtraction.py` | `*_{VNIR\|SWIR}_Orthomosaic.bin` (GOBI: VNIR, CALVIS: VNIR+SWIR) | `PE_{REGION}_pixels[…].parquet`, `PE_{REGION}_plot_metrics[…].parquet`, `PE_extraction_report[…].md` + `PE_figures/` |
| `PE02_IndexPlotExtraction.py` | DS05/SI00 index maps (`SpectralIndices/SI_*_report.json` manifests + NetCDF/GeoTIFF) | `PE_SI_{REGION}_{METHOD}_plot_metrics[…].parquet`, `PE_SI_{REGION}_{METHOD}_report.md` + `PE_figures/` |

`[…]` = optional `_gproN` (only with `--allow-multi-gpro`) and
`_{variant}` (only with `--plot-variant`) suffixes.

## Shared behaviour

- **Crawling** — both scripts `rglob` for their products under `--path`
  (default: git repo root), enforce the official
  `<run>/T1_proc/*.gpro/products/` location, and parse run metadata with
  `cf.parse_APPN_dataset_path`. Runs with multiple `.gpro` folders are
  skipped (`--allow-multi-gpro` overrides, debugging only).
- **Plot files** — discovery/validation lives in
  `Code/functions/plot_layout/`: the main file
  `{YYYYSiteName}_plots.geojson` is mandatory and the default;
  `--plot-variant X` selects `{YYYYSiteName}_plots_X[_vNN].geojson`
  (highest version wins); `_deprecated` files are ignored; a legacy
  `.shp` is accepted with a warning. Files must have a unique `plot_id`
  column and a CRS. `--join-trial-info` joins
  `Documentation/Trial_Info/{YYYYSiteName}_trial_info.csv` on `plot_id`.
- **Caching** — `cf.outputs_up_to_date` mtime checks: a re-crawl no-ops
  on already-extracted runs; `--force` overrides.
- **Provenance** — every table gets a `*_metadata.yaml` sidecar
  (`cf.build_run_metadata`: user, host, git state, inputs, counts).
- **Summary** — both scripts end with a REPORTED/SKIPPED table and
  `main()` returns it as a DataFrame.

## PE00 — LiDAR point extraction

Reads the point cloud in chunks (`laspy.chunk_iterator`, `.laz` supported
via `lazrs`), pre-filters each chunk against the plot file's total bounds
with a cheap numpy box test, then assigns points to plots with a spatial
join. DTM and DSM elevations are sampled at every point (vectorised
nearest-neighbour on the lazily opened rasters) and canopy height is
computed as `Delta_z = z - DTM`.

```bash
python Code/DS03_PlotExtractionCode/PE00_LIDAR_extraction.py --path <Node>/<Project>
```

## PE01 — hyperspectral ortho extraction

The `.bin` orthomosaics are 16 GB+, so nothing is read whole: each plot
polygon is read through its own bounding-box window (`clip_box` →
`clip`), and raw pixel rows stream to parquet through a pyarrow writer.
Two tables per run × EM region are produced by default:

- **Raw pixels** — long format, one row per pixel × band
  (`plot_id`, `band`, `wavelength`, `value`; `--keep-xy` adds
  coordinates). Wavelengths come from the per-band GDAL `wavelength`
  tags; values are cast back to the on-disk dtype after nodata removal.
- **Plot metrics** — per plot × band `mean/median/std/count/
  valid_fraction` plus run metadata (and trial-info columns when
  joined). Metrics are always derived **from the saved raw table**
  (streamed one plot at a time), never a second ortho read, unless
  `--force`; `--metrics-only` refreshes metrics/report without opening
  the ortho at all. `--raw-only` skips metrics + report.

A markdown overview report (`PE_extraction_report[…].md`) with the
extraction statistics and embedded QC figures (plot-footprint
choropleth, per-plot mean-spectra panel; `%`-free filenames) is written
alongside the tables.

**Read strategy benchmark** (2026-08-13, 16 GB GOBI ortho, 600 plots ×
172 bands): `--read-strategy plot` = 369 s, `--read-strategy block`
(24 plots/window) = 631 s. Block windows drag in the inter-plot gap
pixels across every band, so per-plot windows win even on dense plot
grids and are the default.

```bash
python Code/DS03_PlotExtractionCode/PE01_HyperspecPlotExtraction.py --path <Node>/<Project>
# metrics/report refresh without touching the .bin:
python Code/DS03_PlotExtractionCode/PE01_HyperspecPlotExtraction.py --path <Node>/<Project> --metrics-only
```

## PE02 — spectral-index plot extraction

Consumes the **SI00 manifests** (`SI_*_report.json`) rather than
re-discovering rasters, and never opens the `.bin` orthos — the raster
boundary is DS05's (SI00 computes maps, PE02 extracts plots). Index maps
are opened with `decode_coords="all"` (CRS on `spatial_ref`) and each
plot is read through its own bounding-box window; all index variables in
the window aggregate at once. Output is a long-format trait table (one
row per plot × index: `mean/median/std/count/valid_fraction` + run
metadata and any trial-info columns), a markdown report with a
per-index summary table, and figures (headline-index choropleth,
per-index distribution boxplots). `--indices NDVI NDREI …` restricts
the set; caching keys on index maps + manifest + plot file.

```bash
python Code/DS03_PlotExtractionCode/PE02_IndexPlotExtraction.py --path <Node>/<Project>
```

## Tests

The plot-file discovery/validation helpers have a machine-independent
test suite:

```bash
python -m pytest Code/functions/plot_layout/tests/ -q
```
