# DS02 Dataset QA/QC pipeline

Per-run **QC** scripts and cross-run **QA** scripts sharing one naming
scheme, one reporting contract, and one threshold-config pattern. Design
rationale and completion history live in
[QC_PIPELINE_PLAN.md](QC_PIPELINE_PLAN.md); this README is the operator
reference.

## Script set and run order

Run the QC scripts in numbered order per run; QA scripts run afterwards
over any scope (node / project / site). There is deliberately **no
orchestrator** — each script is standalone.

| Order | Script | Scope | Purpose |
|-------|--------|-------|---------|
| 1 | `QC00_GCPCheck.py` | per-run | GCP geometric accuracy — **the gate** |
| 2 | `QC01_FlightCheck.py` | per-run | acquisition parameters, FlightCal/fieldbook spec check, bundle integrity |
| 3 | `QC02_SpectralCheck.py` | per-run | panel spectra extraction + DHR validation + homogeneity |
| 4 | `QC03_RasterCheck.py` | per-run | reflectance `.bin` data validity (advisory) |
| 5 | `QA00_GCPComparison.py` | cross-run | GCP accuracy across runs |
| 6 | `QA01_FlightComparison.py` | cross-run | acquisition anomalies across runs |
| 7 | `QA02_SpectralComparison.py` | cross-run | spectral stability + within-day DHR bias drift |
| — | `QA03_RasterComparison` | reserved | built once QC03 background rates exist |

**Dependency rule: a QC00 fail invalidates everything downstream.** A
GCP failure means GNSS reprocessing, which changes the trajectory and
therefore voids QC01 (AGL/speed/sidelap are trajectory-derived) and QC02
(pixel placement). GNSS reprocessing resets the run to QC00. QC03 is
ELM-derived, not trajectory-derived: a GNSS reprocess does *not* void
it, but an ELM reprocess does.

The per-run scripts are the only ones that open source data (rasters,
point clouds, geojson, gpro bundles); they write stable-named artefacts
into each run's `T1_proc/QC_data/`. The cross-run scripts consume those
artefacts **only** — they never re-open the sources.

## Reporting contract (all scripts)

Every script writes a dual-file report, JSON-first:

- **`<script>_summary.yaml`** — human-scannable: identity, per-check
  status lines, pointer to the detail file, artifact list.
- **`<script>_detail.json`** — everything: full check objects, per-item
  data, config snapshot (threshold YAML path + sha256), staleness fields
  (gpro path + mtime).

The YAML is a pure projection of the JSON (shared `schema_version`
pair). Check statuses are `good | acceptable | warning | fail |
not_checked`; the run status collapses worst-wins to `pass | warn |
fail | not_evaluated`. **Advisory** checks (all of QC02's DHR/
homogeneity checks, all of QC03) report warnings without affecting the
run status. One implementation for all of this:
`Code/functions/qc_report/`.

**Output locations** — per-run reports live in the run
(`<run>/T1_proc/QC_data/`): summary YAMLs at the top level, all detail
JSONs/plots/tables in one `QCxx_<Name>/` subfolder per script. Cross-run
reports never live in run folders — they route by scope (node →
`<Node>/Documents/QAReports/`, project/site →
`<level>/Documentation/QAReports/`, anything else needs `--output-dir`)
into scope-labelled subfolders/filenames.

## Reference files (`reference/`, repo root)

- `reference/thresholds/` — every threshold the scripts grade against
  (`flightcal_spec.yml`, `gcp_limits.yml`, `spectral_limits.yml`,
  `raster_validity.yml`). Each report snapshots the spec path + hash it
  used. Edit thresholds here, never in code.
- `reference/panels/<NODE>/` — the manufacturer DHR panel library QC02
  compares against (gpro-pinned physical set resolution; no cross-node
  fallback).
- `reference/sensor_pipelines/` — the PS00 processing-status registry
  (consumes these reports; see plan §8).

## Prerequisites

| Script | Needs on disk | Must run first |
|--------|---------------|----------------|
| QC00 | `QC_data/QC_GCP*points*.geojson` + product LAS/rasters | — |
| QC01 | `.gpro` bundle (+ `.graw` for integrity checks) | — |
| QC02 | `QC_data/QC_{ELM\|VAL}*_Panels.geojson` + reflectance orthos | — |
| QC03 | reflectance `.bin`/`.hdr` + `extents/hyper_extent.geojson` under the `.gpro` | QC01 recommended (line spacing for the ROI erosion) |
| QA00 | QC00 distance tables/reports | QC00 |
| QA01 | QC01 detail JSONs + `flight_lines.csv` | QC01 |
| QA02 | QC02 spectra tables + `DHR_*` parquets (+ QC01 details for the solar-geometry drift correlation) | QC02 (QC01 recommended) |

Environment: the repo-root [environment.yml](../../environment.yml) is
the single source of truth (`conda env create -f environment.yml`,
env `datastorage`). All scripts run from the repo root, resolve the git
root themselves, and accept `--path` at any APPN tree level plus
`--exclude-dir`; per-run scripts cache via metadata sidecars and accept
`-f/--force`.

## Flagged-run filtering (QA scripts only)

The QA comparisons exclude runs flagged in their date folder's
`RunOverview.csv` so degraded/failed/duplicate acquisitions never
contaminate cross-run statistics; the per-run QC scripts ignore the
flags and grade everything they can. Re-inclusion is opt-in
(`Code/functions/issue_yaml.run_exclusion`):

| Severity | Run state | Included by |
|----------|-----------|-------------|
| clean | no flags, `Deviations` only, or `Issues` with every ticket closed `ok`/`fixed` | always |
| untriaged | `Issues` with open `TODO`/`wip` tickets, or no `*_Issues.yaml` yet | `--include-runs untriaged` (or higher) |
| degraded | `Issues` with a `caution`/`failed` ticket, or an unparseable yaml | `--include-runs degraded` (or higher) |
| failed | `RunFailed` | `--include-runs failed` |

`--include-runs` is cumulative (each level includes the ones below);
`--include-duplicates` re-includes `DuplicateRun` re-runs and is
independent of the ladder. Excluded runs are always listed (QA00 also
carries them in its end-of-run summary as `status=excluded`), and the
resolution path back to the default set is closing the run's Issues
tickets — never unflipping the RunOverview bools.

## Per-script notes (QC00, QC01, QC03, QA01)

- **QC00_GCPCheck** — measured vs surveyed GCP positions per product
  layer; limits from `gcp_limits.yml` (`--spec`); key args:
  `--id-column`, `--plot`, `--type`.
- **QC01_FlightCheck** — flight lines from the gpro bundle (KML +
  timestamps), DTM-based AGL, solar geometry (pvlib SPA), exposure
  segments, FlightCal-calculator + fieldbook spec verdicts, bundle
  integrity (graw/dark-ref/panels/reflectance-ortho ELM tell); spec from
  `flightcal_spec.yml` (`--spec`); rogue take-off/landing lines are
  flagged and excluded (`--rogue-agl-frac`, `--rogue-len-frac`).
- **QC03_RasterCheck** — chunked scan of the reflectance orthos
  (zeros-in-footprint, over-range > 10000, negative, NaN/Inf,
  header↔bin integrity); advisory, thresholds from
  `raster_validity.yml`; `--chunk-mb` bounds memory (~10 min per 16 GB
  cube). All-bands-zero pixels are additionally split against the gpro
  capture polygon (`extents/hyper_extent.geojson`): the eroded ROI is
  graded (`dropout_in_roi` — all-band dropouts that the footprint
  definition hides), the bbox-minus-ROI ring is advisory
  (`zero_edge_band` — expected incomplete capture) and
  `data_outside_bbox` guards the extent/raster pairing. The ROI erosion
  uses QC01's median line spacing when available, so running QC01 first
  is recommended. Runs holding more than one `.gpro` bundle are skipped
  as ambiguous (`--allow-multi-gpro` overrides; labels get a `_gproN`
  suffix); a missing/unreadable `.hdr` fails `header_bin_integrity` for
  that product and the crawl continues.
- **QA01_FlightComparison** — cross-run acquisition anomaly checks
  (exposure mismatch, no-panels, solar window, AGL drift, sun-abeam)
  over QC01 outputs; `--start-date`/`--end-date` window, `--no-save`.

The rest of this README documents QC02/QA02 in detail, then QA00.

---

# QC02_SpectralCheck — panel spectra extraction + validation

## Overview

This script automates the extraction and quality control (QC) of spectral data from validation panels in hyperspectral imaging datasets. It crawls the dataset file structure to locate QC panel vector files and their corresponding raster orthomosaics, extracts pixel values from the panels, compares observed spectra against the manufacturer DHR reference curves (`reference/panels/`, physical set pinned via the run's gpro pipeline YAML), computes per-panel homogeneity statistics, and generates visualization plots for quality assessment. It is designed to work in directorys that follow the APPN folder structure.

QC panel files must follow the official [AerialDataQC naming convention](https://github.com/aus-plant-phenomics-network/APPN-Field-Protocols-and-Pipelines/blob/main/Protocols/QA/QAprocess/AerialDataQC.md): `QC_{ELM|VAL}[_{TargetIdentifier}]_Panels[_{Extra}].geojson` (GeoJSON preferred, shapefile accepted), stored in `<run>/T1_proc/QC_data/`.

## What It Does

1. **Searches** for QC panel files matching the official convention `QC_{ELM|VAL}[_{TargetIdentifier}]_Panels[_{Extra}].geojson` (or `.shp`) under `T1_proc/QC_data/`
2. **Locates** corresponding VNIR and SWIR orthomosaic raster files
3. **Extracts** spectral reflectance values from pixels within the panel geometries
4. **Saves** extracted data as Parquet (or CSV) tables
5. **Compares** observed spectra against the pinned panel set's manufacturer DHR curves (advisory `panel_set_pinned` / `dhr_bias_*` checks, `DHR_*` comparison + delta-stats parquets, overlay/delta figures)
6. **Grades** per-panel homogeneity (distribution-shape statistics; advisory `homogeneity_*` checks — shadow / mixed-edge / hotspot tell)
7. **Generates** visualization plots showing spectral curves across different panels and dates
8. **Writes** the contract report pair (`QC02_SpectralCheck_summary.yaml` + detail JSON)

## Prerequisites

Use the repo-root [environment.yml](../../environment.yml) (env
`datastorage`) — it carries the full stack for every DS0x pipeline
(numpy/pandas/xarray/rioxarray/geopandas/shapely, matplotlib/seaborn,
scipy, pvlib, pyyaml, gitpython, tqdm).

### System Requirements
- Python 3.13 (or at least 3.12+)
- Must be run from within an APPN folder structure git repository or with the `--path` argument specified
- Dataset must follow the APPN folder structure (see below)

## Command-Line Arguments

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--path` | str | (git root) | Path to search for QA shapefiles. Defaults to git repository root. |
| `-f, --force` | flag | False | Force re-creation of output files, overwriting existing ones. |
| `--type` | str | parquet | Output file format: `parquet` (default) or `csv`. |
| `-s, --skipplot` | flag | False | Skip plot generation (only extract data tables). |
| `--skip-processing` | flag | False | Skip raster processing; only load existing output files for reporting. |
| `--spec` | str | `reference/thresholds/spectral_limits.yml` | DHR-bias / homogeneity limits YAML (repo-root relative). |
| `--exclude-dir` | str+ | [] | Directory names to exclude from the panel search. |
| `--allow-multi-gpro` | flag | False | Process runs with more than one `.gpro` (debugging only — product set is ambiguous). |
| `--keep-xy` | flag | False | Retain per-pixel x/y coordinate columns in the tables. |
| `--no-radiance-check` | flag | False | Disable the reflectance-vs-radiance range check. |
| `-v, --verbose` | flag | False | Enable detailed output for debugging. |

> **Multi-run comparison** (figures across runs/sites/nodes, plus the
> `--save-dir` / `--load-dir` sharing workflow) lives in
> `QA02_SpectralComparison.py`. QC02 is the per-run extraction + QC
> report step. QA02 also accepts an inclusive `--start-date` /
> `--end-date` window (e.g. `2026-06-01` or `20260601`) to limit which
> runs are compared.

## Usage Examples

### Suggested Usage for Sharing files between APPN nodes
This workflow extracts spectra from your local datasets (QC02), then gathers
and saves copies to a shared location (QA02), making it easy for other nodes
to access and combine with their own data.

```bash
python QC02_SpectralCheck.py --path /path/to/APPNfolderstructure
python QA02_SpectralComparison.py --path /path/to/APPNfolderstructure --save-dir /path/to/shared/spectra
```

**Parameters:**
- `--path`: Point to your local APPN dataset root (e.g., `/mnt/d/APPN-42-datastorage/USYD_Narrabri`)
- `--save-dir`: A local directory that can be easily compressed (zip or tar) then shared with other nodes via filesender or globus

This creates standardized spectral files that other nodes can then load using
`QA02_SpectralComparison.py --load-dir` to combine all data for
comprehensive QC analysis.

### Basic Usage
Run from within the git repository:
```bash
python QC02_SpectralCheck.py
```

### Specify Custom Path
Search a specific directory:
```bash
python QC02_SpectralCheck.py --path /path/to/dataset
```

### Extract Only (Skip Per-run Figures)
Create data tables and reports without generating figures:
```bash
python QC02_SpectralCheck.py -s
```

### Force Regeneration
Overwrite existing output files:
```bash
python QC02_SpectralCheck.py --force
```

### Use Parquet Format
More efficient for large datasets:
```bash
python QC02_SpectralCheck.py --type parquet
```

### Save Data for Sharing (QA02)
Gather extracted spectra and save copies to a central directory:
```bash
python QA02_SpectralComparison.py --path /path/to/node --save-dir /path/to/shared/spectra
```

### Load External Data (QA02)
Combine local data with spectra from other nodes:
```bash
python QA02_SpectralComparison.py --path /path/to/node --load-dir /path/to/external/spectra
```

### Limit the Comparison to a Date Window (QA02)
Only compare runs inside an inclusive date range (either bound may be
omitted; any `pandas`-parseable date form works):
```bash
python QA02_SpectralComparison.py --path /path/to/node --start-date 2026-06-01 --end-date 2026-08-31
```

### Quick Re-reporting
Skip processing, just regenerate reports/figures from existing files:
```bash
python QC02_SpectralCheck.py --skip-processing
```

## Expected Folder Structure

The script expects the APPN dataset structure:
```
workspace_root/
└── node_name/
    └── project_name/
        └── site_name/
            └── sensor_name/          # e.g., GOBI, CALVIS
                └── YYYYMMDD/         # date folder
                    └── run_name/     # e.g., Run01
                        └── T1_proc/
                            ├── QC_data/
                            │   ├── QC_ELM_Panels.geojson  # Panel file (official naming)
                            │   └── QC_Spectral_Tables/    # Output directory (created by script)
                            └── *.gpro/
                                └── products/
                                    ├── *_VNIR_Orthomosaic.bin
                                    └── *_SWIR_Orthomosaic.bin (CALVIS only)
```

### Required Files
- **Panel File** (GeoJSON preferred, shapefile accepted): Must contain:
  - `geometry` column (polygon geometries)
  - `Panel_ref` column (reference reflectance values, 0-1 scale)
- **Orthomosaic Rasters**: VNIR and SWIR (CALVIS only) `.bin` files

## Output Files

### Spectral Tables
Located in `QC_Spectral_Tables/` subdirectories alongside panel files.

**Filename Format:**
```
{sensor_type}{gpro_num}_{panel_name}_{ortho_name}.{csv|parquet}
```

**Columns:**
- `band`: Band number
- `value`: Extracted reflectance value (0-100 scale for GOBI/CALVIS)
- `Panel_ref`: Reference panel reflectance (0-1 scale)
- `node`, `project`, `site`, `sensor`, `date`, `run`: Metadata fields
- `panel_name`: Name of the QC panel
- `EM_Region`: Electromagnetic region (VNIR or SWIR)
- `gpro_nu`: GoPro/acquisition number (if multiple)

### Visualization Plots
Interactive matplotlib/seaborn plots showing:
- Spectral curves grouped by panel and date
- Separate plots for each sensor, panel type, and EM region
- Error bars showing percentile intervals
- Residual plots (measured - reference)
- Optional bad-band removal views

## Supported Sensors

- **GOBI**: VNIR only
- **CALVIS**: VNIR + SWIR

Known-bad wavelength ranges are defined physically (nm) in
`Code/functions/spectral_qc.default_bad_wavelengths` — CALVIS SWIR
water-vapour windows (940/1400/1900 nm — the 1900 nm band widened to
1990 nm from DT01 evidence) and the detector tail, plus first VNIR
candidates (400–420 nm artefact, > ~920 nm noise). They are excluded
from residual/DHR statistics and rendered as line gaps in figures; edit
the helper, not per-script band lists.

## Workflow

1. **Initialization**: Determine git root or use provided `--path`
2. **Discovery**: Recursively search for QC panel files under `T1_proc/QC_data/`
3. **Validation**: Check panel file structure and locate orthomosaics
4. **Processing**: For each panel:
   - Load panel geometries
   - Clip raster to panel boundaries
   - Extract pixel values with spatial join
   - Save to Parquet/CSV
5. **DHR comparison**: Resolve the physical panel set (gpro pin / node signature / elimination), compare observed vs expected curves, write `DHR_*` tables + figures
6. **Homogeneity**: Grade per-panel distribution shape against the calibrated `spectral_limits.yml` thresholds
7. **Visualization**: Generate plots grouped by sensor/panel/EM region
8. **Reporting**: Write the contract summary/detail pair (node sharing via `--save-dir`/`--load-dir` lives in QA02)

## Troubleshooting

### Common Issues

**"No QC panel files found"**
- Check that files match the official convention `QC_{ELM|VAL}[_{TargetIdentifier}]_Panels[_{Extra}].geojson` (or `.shp`) and live under `T1_proc/QC_data/`
- Verify you're in the correct directory or provided the right `--path`

**"Panel file does not have expected columns"**
- Ensure `Panel_ref` column exists (case-sensitive) — fix the file, the script does not auto-repair non-standard files

**"No VNIR/SWIR orthomosaic found"**
- Check that `.gpro/products/` folders contain orthomosaic `.bin` files
- Verify file naming: `*_VNIR_Orthomosaic.bin` or `*_SWIR_Orthomosaic.bin`

**"Maximum value falls in the radiance range (0-50 int)"**
- GOBI/CALVIS reflectance values should be either int 0-10000 or float 0-1
- Values between 1 and 50 suggest raw radiance rather than reflectance
- Check if raster preprocessing applied the correct reflectance conversion

**"Could not read output file"**
- Match `--type` argument to existing file format
- Check file permissions
- Re-run with `--force` to regenerate

### Verbose Mode
Use `-v` or `--verbose` for detailed diagnostic output:
```bash
python QC02_SpectralCheck.py --verbose
```

## Notes

- Processing large rasters may take several minutes per file
- Use `--skip-processing` for faster re-runs when data is already extracted (the DHR comparison, homogeneity grading and contract report still regenerate from the existing tables)
- Parquet format is the default (faster I/O, smaller files)
- The script automatically handles case-sensitivity issues in older shapefiles

## QA02_SpectralComparison — cross-run spectral stability

QA02 gathers the QC02 artefacts across every run under `--path` and
produces:

- **Cross-run spectra figures** — three per sensor group × target × EM
  region, faceted by panel with one line per run, wavelengths snapped
  onto a shared reference grid:
  - `*_refl.png` — observed reflectance with the expected DHR overlaid
    as dashed black curves (serials with numerically identical curves
    collapse onto one line; different DHRs, e.g. another node's Gryfn4,
    keep separate dash patterns);
  - `*_accuracy.png` — observed − expected DHR (pp, symlog), each run
    graded against the DHR its own node resolved; skipped when no run
    in the group has a DHR artefact;
  - `*_precision.png` — residual vs the cross-run mean (% refl, symlog).

  Platforms that share an EM region (e.g. the CALVIS/GOBI shared
  Headwall VNIR) are pooled into one figure set by default (the
  platform shows via line style and the run label, and the precision
  reference pools platforms); `--split-platforms` restores per-platform
  figures. An advisory `dhr_serial_mixing` check warns when a
  signature-based target group spans more than one physical set serial
  (the precision reference then blends hardware).
- **DHR aggregation** (from the per-run `DHR_*_comparison` /
  `DHR_*_delta_stats` parquets): combined `QA02_all_runs_*` tables.
- **Within-day drift check** (`dhr_within_day_drift`, advisory) — how
  far each panel's full-region DHR bias walked across a day's runs,
  Spearman-correlated against run order and against QC01's solar
  elevation (run QC01 first to enable the solar correlation); thresholds
  from the `within_day_drift` block of `spectral_limits.yml`
  (`--spec`). Written as `QA02_dhr_within_day_drift.parquet` + a
  contract check.
- **Node-sharing containers** — `--save-dir` builds a portable container
  (tables/reports/figures), `--load-dir` merges a received one.

Outputs route to the scoped `QAReports/` folder with the contract
report pair. Pairwise distribution statistics (Wasserstein-1) remain
pending the ET00/ET03 equivalence test (APEx_SensorCalibration).

## Multi-run GCP comparison (QA00)

`QA00_GCPComparison.py` is to `QC00_GCPCheck.py` what
QA02 is to QC02: it gathers the per-run GCP distance tables
(`QC_GCP[_{Product}]_distances[_{extra}].{csv|parquet}`) and accuracy
report JSONs written by QC00 across every run under `--path` and
compares them.

**Inputs**: QC00 artefacts only (run QC00 first). Stats come from the
report JSON where present and current; missing, stale, or
foreign-schema reports are recomputed from the distance table with the
same maths (`Code/functions/gcp_qc`, shared with QC00) — the
`stats_source` column records which path was used.

**Grouping**: sensor × product layer (`QC_GCP_points` → "all
products"; `QC_GCP_{Product}_points` → per product; `_extra` filename
suffixes stay part of the label so duplicate layers in one run plot as
distinct lines).

**Outputs** (into the routed `QAReports/` dir):
- `QC_GCP_run_comparison.{parquet,csv}` — per run × product summary
  (counts, 2D/3D RMSE, mean/median/max, bias magnitude + bearing +
  fraction + class, QC00 pass/fail).
- `QC_GCP_{sensor}_metrics.png` — RMSE/median/bias per run.
- `QC_GCP_{sensor}_bias_vectors.png` — per-run 2D bias vectors on a
  compass polar axis (systematic-offset drift check).
- `QC_GCP_{sensor}_per_gcp.png` — per-GCP-id displacement across runs
  (a single moved marker vs a whole-flight shift).
- `QC_GCP_run_comparison.md` — overview report embedding the figures
  with relative paths (renders in the VS Code / GitHub preview).

**Sharing**: `--save-dir` builds a portable container (`tables/` +
`reports/` + `figures/` + `comparison_figures/` + `manifest.csv`);
`--load-dir` merges a received container (or any folder of QC00
tables) into the comparison. `--start-date`/`--end-date` bound the run
window. Outputs are mtime-cached against every gathered input; use
`--force` to regenerate.

```bash
# Project-level comparison (saves to <Project>/Documentation/QAReports/)
python Code/DS02_DatasetQA/QA00_GCPComparison.py --path USYD_Narrabri/2026_APEx

# Build a container to send to another node
python Code/DS02_DatasetQA/QA00_GCPComparison.py --path <node> --save-dir /path/to/share

# Merge a received container into the local comparison
python Code/DS02_DatasetQA/QA00_GCPComparison.py --path <node> --load-dir /path/to/received
```

## Contact

For questions or issues, contact: arden.burrell@sydney.edu.au
