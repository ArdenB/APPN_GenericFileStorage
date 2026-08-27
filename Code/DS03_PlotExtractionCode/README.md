# DS03 — Plot Extraction Code

Scripts that extract per-plot values from processed (Tier 1) sensor
products using the site's **Plot_Layout** vector files
(`Documentation/Plot_Layout/{YYYYSiteName}_plots.geojson` — see the
[Key-Files wiki page](https://github.com/ArdenB/APPN_GenericFileStorage/wiki/Key-Files)).
All outputs are **Tier 1** products written to
`<run>/T1_proc/PlotExtracts/` (`T2_traits/` is reserved for
ML-model-derived products), split into three sub-folders:

- **`PixelLevel/`** — heavy point/pixel-level parquet **datasets**:
  directories of per-plot (PE01/PE02) or per-scan-chunk (PE00) zstd
  part files, readable as one table by any parquet dataset reader
  (`pd.read_parquet(dir)`, `pyarrow.dataset`, duckdb). Each dataset's
  `*_metadata.yaml` sidecar sits beside the directory and is written
  **last**, so it doubles as the completion marker.
- **`PlotLevel/`** — light, analyst-facing per-plot metric tables
  (single parquet files + sidecars).
- **`Reports/`** — markdown QC reports + `PE_figures/`.

| Script | Input | Output |
|--------|-------|--------|
| `PE00_LIDAR_extraction.py` | `*_LiDAR_CombinedPointCloud.las/.laz` + DSM/DTM | `PixelLevel/PE_LIDAR_points[…]/` dataset + metadata YAML, `PlotLevel/PE_LIDAR_plot_metrics[…].parquet` (+ `PE_LIDAR_plot_percentiles[…].parquet` with `--full-percentiles`) |
| `PE01_HyperspecPlotExtraction.py` | `*_{VNIR\|SWIR}_Orthomosaic.bin` (GOBI: VNIR, CALVIS: VNIR+SWIR) | `PixelLevel/PE_{REGION}_pixels[…]/` dataset, `PlotLevel/PE_{REGION}_plot_metrics[…].parquet` (+ `…_plot_percentiles[…].parquet` with `--full-percentiles`), `Reports/PE_extraction_report[…].md` + `PE_figures/` |
| `PE02_IndexPlotExtraction.py` | DS05/SI00 index maps (`SpectralIndices/SI_*_report.json` manifests + NetCDF/GeoTIFF) | `PixelLevel/PE_INDEX_{REGION}_{METHOD}_pixels[…]/` dataset, `PlotLevel/PE_INDEX_{REGION}_{METHOD}_plot_metrics[…].parquet` (+ `…_plot_percentiles[…].parquet` with `--full-percentiles`), `Reports/PE_INDEX_{REGION}_{METHOD}_report[…].md` + `PE_figures/` |

`[…]` = optional `_gproN` (only with `--allow-multi-gpro`) and
`_{variant}` (only with `--plot-variant`) suffixes.

### Reading the pixel datasets

```python
import pandas as pd
# whole table (streams file-at-a-time under the hood):
df = pd.read_parquet("…/PixelLevel/PE_VNIR_pixels")
# one plot only (prunes to that plot's part file):
df = pd.read_parquet("…/PixelLevel/PE_VNIR_pixels",
                     filters=[("plot_id", "=", "P0123")])
```

PE01 pixel tables carry `plot_id`, `band`, `value` — the band →
wavelength (nm) table lives in the dataset's `*_metadata.yaml` sidecar
(`data.wavelengths_nm`), not in every row.

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
- **Caching** — `cf.outputs_up_to_date` mtime checks anchored on the
  dataset sidecars (written last = completion marker): a re-crawl
  no-ops on already-extracted runs, and PE01/PE02 resume interrupted
  extractions at the first missing/stale per-plot part; `--force`
  overrides.
- **Provenance** — every table gets a `*_metadata.yaml` sidecar
  (`cf.build_run_metadata`: user, host, git state, inputs, counts).
- **Summary** — both scripts end with a REPORTED/SKIPPED table and
  `main()` returns it as a DataFrame.
- **Plot metrics** — all three scripts share the DS03 statistic set
  (`Code/functions/plot_extracts.group_value_stats`), computed per plot
  × group from the saved pixel/point dataset: `count`, `mean`, `std`,
  `var`, `min`, `max`, `median`, `skew`, `kurtosis` (bias-corrected,
  pandas-compatible; Fisher excess), `l_cv`/`l_skew`/`l_kurt`
  (L-moment ratios τ/τ3/τ4 — robust, bounded distribution-shape
  fingerprint; strong bimodality such as half-soil/half-canopy plots or
  lodged crops shows up as low `l_kurt`), `normality_k2`/`normality_p`
  (D'Agostino-Pearson; NaN below n=20 — with thousands of pixels the
  p-value rejects for trivial deviations, so prefer the statistic and
  skew/kurtosis as effect sizes) and the short percentiles
  `p01/p05/p10/p25/p50/p75/p90/p95/p99` (one `np.quantile` sort per
  group). `--full-percentiles` additionally writes a long-format
  `*_plot_percentiles[…].parquet` table (101 rows per group,
  percentile 0–100 where 0 = min and 100 = max; join via `plot_id`) —
  near-free to compute, kept separate so the main table stays wide-
  readable.

## PE00 — LiDAR point extraction

Reads the point cloud in chunks (`laspy.chunk_iterator`, `.laz` supported
via `lazrs`), pre-filters each chunk against the plot file's total bounds
with a cheap numpy box test, then assigns points to plots with a spatial
join. DTM and DSM elevations are sampled at every point (vectorised
nearest-neighbour on the lazily opened rasters) and canopy height is
computed as `Delta_z = z - DTM`. Each chunk streams straight to its own
part file in the `PE_LIDAR_points[…]/` dataset, so peak memory is one
chunk regardless of point-cloud size (`--type csv` writes a single flat
file instead; no plot metrics). A per-plot canopy-height metrics table
(`PlotLevel/PE_LIDAR_plot_metrics[…].parquet`, shared statistic set of
`Delta_z` + a `variable` column for future height definitions) is then
derived from the saved dataset — the point cloud is never re-read — and
refreshed independently when stale (`status=metrics_refreshed`).

```bash
python Code/DS03_PlotExtractionCode/PE00_LIDAR_extraction.py --path <Node>/<Project>
```

## PE01 — hyperspectral ortho extraction

The `.bin` orthomosaics are 16 GB+, so nothing is read whole: each plot
polygon is read through its own bounding-box window (`clip_box` →
`clip`), and each plot's rows are written atomically to its own part
file in the `PE_{REGION}_pixels[…]/` dataset — an interrupted run
resumes at the first missing/stale plot instead of restarting.
Two tables per run × EM region are produced by default:

- **Raw pixels** — long format, one row per pixel × band
  (`plot_id`, `band`, `value`; `--keep-xy` adds coordinates).
  Wavelengths come from the per-band GDAL `wavelength` tags and are
  recorded once in the sidecar (`data.wavelengths_nm`); values are
  cast back to the on-disk dtype after nodata removal.
- **Plot metrics** — per plot × band: the shared statistic set (see
  *Shared behaviour*) + `wavelength` + `valid_fraction` plus run
  metadata (and trial-info columns when joined). Metrics are always
  derived **from the saved raw dataset** (one plot-part at a time),
  never a second ortho read, unless `--force`; `--metrics-only`
  refreshes metrics/report without opening the ortho at all.
  `--raw-only` skips metrics + report.

A markdown overview report (`Reports/PE_extraction_report[…].md`) with
the extraction statistics and embedded QC figures (plot-footprint
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
plot is read through its own bounding-box window across every map at
once; the plot's per-pixel index values are written to its own part
file in the `PE_INDEX_{REGION}_{METHOD}_pixels[…]/` dataset
(`plot_id`, `index`, `value`), and the trait table is derived from the
saved dataset. Outputs: the long-format trait table (one row per plot ×
index: `mean/median/std/count/valid_fraction` + run metadata and any
trial-info columns), a markdown report with a per-index summary table,
and figures (headline-index choropleth, per-index distribution
boxplots). `--indices NDVI NDREI …` restricts the set (combine with
`--force` when changing it); caching keys on index maps + manifest +
plot file.

```bash
python Code/DS03_PlotExtractionCode/PE02_IndexPlotExtraction.py --path <Node>/<Project>
```

## Tests

The plot-file discovery/validation helpers have a machine-independent
test suite:

```bash
python -m pytest Code/functions/plot_layout/tests/ -q
```
