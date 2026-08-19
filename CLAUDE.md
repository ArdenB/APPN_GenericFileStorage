# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Coding rules

All coding rules (script template, no-hidden-inputs, git-root bootstrap, docstrings)
live in the imported file below — follow them.

@AGENTS.md

## Commands

Everything runs from the **repo root** — scripts resolve the git root with
`gitpython`, `chdir` into it, and use it as the default `--path`.

```bash
conda activate datastorage          # env with the full geospatial stack

pytest Code -q                      # full suite (110 tests, ~1s)
pytest Code/functions/plot_layout/tests/test_plot_layout.py -v
pytest Code/functions/core_functions/tests/test_parse_APPN_dataset_path.py -k auto

python ProjectBuilder.py                                       # build/refresh folder tree + metadata
python Code/DS02_DatasetQA/QA00_SpectralValidation.py --path <node_or_project>
python Code/DS05_SpectralIndices/SI00_SpectralIndices.py --path <node_or_project>
python Code/DS03_PlotExtractionCode/PE01_HyperspecPlotExtraction.py --path <node_or_project>
```

Every script takes `--help`; the common flags across the pipelines are `--path`,
`--force`, `--skipplot`, `--allow-multi-gpro`, `--exclude-dir`.

There is no `environment.yml` here — the root README lists the `conda create`
line. (The node-side repo does have one; see below.) Sub-folder READMEs name
different env names for the same stack — `datastorage` is the working one on this
machine. Linting is pylint with `--disable=C,R` plus the template-driven warning
exemptions in `.vscode/settings.json` (untracked).

## What this repository is

A **generic, publishable template** for the APPN aerial-phenotyping data store:
the folder-structure builder plus the QC/extraction pipelines that operate on it.
It ships **no data**. 

**Code flows node-side → here.** Features are developed against real data in
APPN-42-datastorage and then *ported* into this repo generically (see the
`DS0x port: ...` commits). When something looks half-finished here, the node-side
repo is usually where the rest of it lives — check there before assuming a gap.

## Architecture

### The folder convention is the API

`FolderStructureInfo.txt` defines the on-disk contract:

```
<root>/<Node>/<YYYY_ProjectDesc>/<Site>/<SensorPlatform>/<YYYYMMDD>/runXX/{T0_raw,T1_proc,T2_traits}
```

`cf.parse_APPN_dataset_path` (424 lines, the most-tested module) turns any path at
any depth into that metadata dict, with `valid`/`errors`. **Every pipeline script
crawls with `rglob` and routes through it** — new scripts must too, rather than
splitting paths by hand. Parsing is pure string work; filesystem checks only run
when the path exists, which is what keeps the tests machine-independent.

Tier meanings are load-bearing: `T1_proc` = sensor-derived products (all pipelines
here write there), `T2_traits` = reserved for ML-model-derived products (nothing
here writes there).

### Pipeline stages

Scripts are numbered by stage and each folder has a README that is the real spec
for its outputs — read it before touching a script in that folder.

| Folder | Does | Writes under `<run>/T1_proc/` |
|---|---|---|
| `Code/DS02_DatasetQA` | QA00/QA01 per-run panel spectra + GCP distances; QA02/QA03 cross-run comparison | `QC_data/`, reports routed to `QCReports/` |
| `Code/DS05_SpectralIndices` | SI00 raster-in/raster-out spyndex index maps | `SpectralIndices/` |
| `Code/DS03_PlotExtractionCode` | PE00 LiDAR, PE01 hyperspec, PE02 index maps → per-plot values | `PlotExtracts/{PixelLevel,PlotLevel,Reports}/` |
| `Code/OT00_OneTimeScripts` | hand-run store maintenance (renames, moves, log collection) | mutates the store; `--dry-run` + y/N confirm |
| `ProjectBuilder.py` | builds the folder tree + YAML/CSV metadata from `NodeSummary.yaml`, commits changes via gitpython | the whole tree |

The per-run / cross-run split is deliberate: per-run scripts are the only ones
that open rasters, point clouds or geojson, and they write stable-named artefacts;
cross-run scripts consume **only** those artefacts. Don't reach back to the source
rasters from a comparison script.

Stages chain through declared products, not through shared memory: SI00's
`SI_*_report.json` manifest is the schema PE02 consumes, and PE01's dataset
sidecars carry the band→wavelength table that its pixel rows omit.

### `Code/functions/` — shared helpers (R8: import, don't re-implement)

- `core_functions/` — `parse_APPN_dataset_path`, `outputs_up_to_date` (mtime
  caching), `build_run_metadata`/`write_metadata_yaml` (provenance),
  `resolve_qcreports_dir`/`markdown_table` (report routing + rendering),
  `band_wavelengths` (ENVI `.hdr` centres), `resolve_run_palette` (consistent
  per-run colours across figures)
- `plot_layout/` — discovery/validation of `{YYYYSiteName}_plots.geojson` and its
  variants/versions/`_deprecated` rules; `load_site_plots`, `find_trial_info`
- `plot_extracts/` — the DS03 output tree + atomic (`.tmp` → `os.replace`) parquet
  part writing
- `spectral_indices/` — band → spyndex symbol mapping, `computable_indices`
- `spectral_qc/`, `gcp_qc/` — DS02 statistics (bad-band nm ranges, bias
  decomposition)

Tests live beside the module they cover (`<module>/tests/`), and only
`core_functions` and `plot_layout` have them.

### Three idioms every pipeline script repeats

1. **Sidecar-anchored caching.** Each output gets a `*_metadata.yaml` sidecar
   written **last**, so it doubles as the completion marker.
   `cf.outputs_up_to_date` mtime-checks against it, a re-crawl no-ops on finished
   runs, and interrupted extractions resume at the first missing part. `--force`
   overrides. Preserve the write-order when adding outputs — writing the sidecar
   early makes a crashed run look complete.
2. **Provenance on everything.** `cf.build_run_metadata` stamps user, host, git
   state, inputs and counts into the sidecar.
3. **REPORTED/SKIPPED summary.** `main()` ends by printing a status table and
   returning it as a DataFrame.

### Data files

Nothing large is tracked: `.gitignore` blanket-ignores `*.csv` and `*.parquet` and
then re-allows only the ProjectBuilder-maintained metadata files
(`*_ProjectsSummary.csv`, `*_SyncSummary.csv`, `FieldLog.csv`, `RunOverview.csv`).
On-disk tabular outputs are parquet; csv is for human-edited metadata only (P7).

Wiki-hosted specs (folder structure, Key-Files, the `QC_{ELM|VAL}_Panels` naming
convention) are normative and linked from the READMEs.
