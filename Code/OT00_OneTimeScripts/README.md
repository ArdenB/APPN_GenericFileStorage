# OT00 — One-Time Scripts

Ad-hoc maintenance scripts that are run by hand (typically once) against
the APPN data store. Unlike the routine processing pipelines, these are
not invoked by [`ProjectBuilder.py`](../../ProjectBuilder.py) and they
do not assume the full project conda environment is active — a slim
purpose-built environment (see [Environment](#environment)) is
sufficient.

All scripts share a few conventions:

- **Discovery**: they locate the search root from a `--path` argument
  if provided, otherwise from `git rev-parse --show-toplevel` (i.e. the
  enclosing git repository). The git fallback only works when the
  current working directory is inside a checkout of this repo.
- **Safety**: every script that mutates the filesystem prompts for an
  interactive `y/N` confirmation before applying changes, and most
  also support a `--dry-run` flag.
- **Scope**: scripts are written to be re-run safely. They detect
  already-converted folders, skip Synology metadata (`@eaDir`) and
  recycle bins, and do not descend into `.graw`/`.gpro` payload
  folders.

---

## Contents

| Script | Purpose |
| --- | --- |
| [`OT00_RenameTiertoT.py`](OT00_RenameTiertoT.py) | Rename legacy `Tier*` folders to the current `T*_<role>` convention. |
| [`OT01_MoveGrawToRaw.py`](OT01_MoveGrawToRaw.py) | Relocate `*.graw` payload folders from `T1_proc` into the sibling `T0_raw` tier (GOBI/CALVIS only). |
| [`OT02_CollectCalvisGobiLogs.py`](OT02_CollectCalvisGobiLogs.py) | Collect log/config files from CALVIS and GOBI `.graw`/`.gpro` folders into a single review directory with a manifest, ready to send to GRYFN. |

Run `python <script>.py --help` for the full argparse spec.

---

## OT00_RenameTiertoT — Rename `Tier*` folders to `T*`

**Why**: an early version of the APPN folder convention used names like
`Tier0_raw` / `Tier1_proc` / `Tier2_traits`. The current convention
shortens these to `T0_raw` / `T1_proc` / `T2_traits`. This script
performs the rename in bulk so historic projects line up with the new
[`parse_APPN_dataset_path`](../functions/core_functions/parse_APPN_dataset_path.py)
validator and the sync pipeline.

**What it does**

1. Resolves the search root (`--path` if given, otherwise git root).
2. Walks every directory under the root.
3. For every folder whose name starts with `Tier`, renames it to
   `T` + the rest of the name (so `Tier0_raw` → `T0_raw`,
   `Tier12_custom` → `T12_custom`).
4. Skips a rename if the target name already exists, logging a warning
   instead of clobbering.
5. Prints a summary of successes/failures at the end.

**Arguments**

| Flag | Description |
| --- | --- |
| `--path PATH` | Root directory to scan. Optional; defaults to the git repository root. |

**Typical usage**

```bash
# Apply against the full repo via git-root resolution
python Code/OT00_OneTimeScripts/OT00_RenameTiertoT.py

# Or restrict to a specific tier
python Code/OT00_OneTimeScripts/OT00_RenameTiertoT.py \
    --path /mnt/d/Tier3_ColdStorage
```

**Caveats**

- This script does not have a `--dry-run` flag; the rename is direct.
  Run against a known scratch directory first if you are unsure.
- It will not rename a folder if a sibling with the new name already
  exists — that case requires manual reconciliation.

---

## OT01_MoveGrawToRaw — Move `.graw` folders into `T0_raw`

**Why**: GOBI and CALVIS produce `*.graw` payload folders that
sometimes land in `T1_proc` (the processed tier) when they should sit
in `T0_raw`. This violates the
[APPN folder structure](https://github.com/ArdenB/APPN_GenericFileStorage/wiki)
and breaks downstream tools that expect raw payloads under `T0_raw`.

**What it does**

1. Resolves the search root (`--path` or git root).
2. Walks the tree looking for `T1_proc` folders that sit beneath a
   sensor folder whose name contains `GOBI` or `CALVIS`
   (case-insensitive).
3. For each such `T1_proc`, lists its direct child folders ending in
   `.graw` and pairs them with the sibling `T0_raw` folder (created if
   missing).
4. Prints the planned moves, asks for confirmation, then performs the
   moves with a tqdm progress bar.

**Arguments**

| Flag | Description |
| --- | --- |
| `--path PATH` | Root directory to scan. Optional; defaults to the git root. |
| `--dry-run` | List planned moves without touching disk. |

**Layout assumption**

```
.../<sensor>/<YYYYMMDD>/run_XX/T1_proc/<X>.graw
                                 ↓
.../<sensor>/<YYYYMMDD>/run_XX/T0_raw/<X>.graw
```

**Typical usage**

```bash
# Preview
python Code/OT00_OneTimeScripts/OT01_MoveGrawToRaw.py --dry-run

# Apply against a specific tier
python Code/OT00_OneTimeScripts/OT01_MoveGrawToRaw.py \
    --path /mnt/d/Tier2_DataArchive
```

**Caveats**

- Only GOBI/CALVIS sensor folders are touched; other sensors are left
  alone even if they contain `.graw` folders.
- Moves are skipped (with a warning) if a folder of the same name
  already exists in `T0_raw`.

---

## OT02_CollectCalvisGobiLogs — Bundle CALVIS/GOBI logs for review

**Why**: GRYFN periodically asks for a self-contained snapshot of
mission/processing logs from CALVIS and GOBI captures. Manually copying
the relevant files out of every `.graw`/`.gpro` folder is tedious and
error-prone. This script automates that bundle and produces a manifest
that records the original layout plus any APPN-structure problems with
the source paths.

**What it does**

1. Resolves one or more search roots (`--path` accepts multiple values
   and is also de-duplicated). Falls back to the git root if no
   `--path` is supplied.
2. Walks each root looking for `*.graw` and `*.gpro` folders that sit
   under a sensor folder whose name contains `CALVIS` or `GOBI`. Does
   not descend into the `.graw`/`.gpro` payloads themselves.
3. Sorts and labels each discovered folder as
   `<SENSOR>_<KIND>_<NN>` (e.g. `CALVIS_GRAW_00`, `GOBI_GPRO_03`),
   zero-padded so labels sort lexicographically.
4. Validates each source path against the APPN folder structure with
   [`parse_APPN_dataset_path`](../functions/core_functions/parse_APPN_dataset_path.py)
   at `path_level="sub_tier"` and records the result in the manifest.
5. Builds a copy plan from a curated set of glob patterns (see below),
   prints a preview, then prompts for confirmation.
6. Copies files (preserving each file's relative path inside its source
   folder) into `<dest>/<label>/...` and writes
   `<dest>/manifest.csv`.

**Arguments**

| Flag | Description |
| --- | --- |
| `--path PATH [PATH ...]` | One or more search roots. May be repeated or supplied as a space-separated list. |
| `--dest PATH` | Output directory. Defaults to `<first --path>/GRYFN_logs`. |
| `--dry-run` | List the copy plan and per-folder APPN validation results without touching disk. |
| `-y`, `--yes` | Skip the interactive confirmation prompt and proceed automatically. |

**Files collected**

From each `*.graw` folder:

- `mission_data.yaml`
- `targets.yaml`
- `elm_coefficients.json`
- `*HP-*/**/settings.txt` (recursive; matches e.g. `nHP-929`, `cAHP-191`)
- `uVS-*/**/settings.txt` (recursive)
- `SBG/**/export_*.txt` (recursive)
- `APX-15/**/export_*.txt` (recursive)

From each `*.gpro` folder:

- `products.yaml`
- `pipelines/*.yml`
- `processing_logs/*.txt`

**Manifest columns**

```
sensor, kind, number, label,
source_path, output_path,
appn_valid, appn_errors,
node, project, site_folder, year, sensor_folder,
date, run_folder, run, tier, sub_tier
```

`appn_valid` is a boolean and `appn_errors` is a `|`-separated list of
human-readable problems emitted by `parse_APPN_dataset_path` (e.g.
"missing a site folder above 'GOBI'…"). Rows with `appn_valid=False`
have their parsed metadata fields blank — the path didn't conform, so
the metadata cannot be trusted.

**Typical usage**

```bash
# All three storage tiers in one bundle, written to a custom location
python Code/OT00_OneTimeScripts/OT02_CollectCalvisGobiLogs.py \
    --path /mnt/d/APPN-42-datastorage \
           /mnt/d/Tier2_DataArchive \
           /mnt/d/Tier3_ColdStorage \
    --dest /mnt/d/APPN-42-datastorage/GRYFN_logs

# Preview only
python Code/OT00_OneTimeScripts/OT02_CollectCalvisGobiLogs.py \
    --path /mnt/d/APPN-42-datastorage --dry-run
```

**Caveats**

- The same source folder discovered via two overlapping search roots is
  collected only once (de-duplicated by resolved absolute path).
- The destination directory is automatically excluded from the search,
  so re-running the script does not re-scan a previously collected
  bundle (e.g. `<root>/GRYFN_logs`).
- Sensor matching is by ancestor folder name containing `CALVIS` or
  `GOBI` — folders that follow the convention but live under an
  oddly-named parent will be skipped.
- An invalid APPN path is reported in the manifest but does **not**
  abort the copy; the bundle still ships, it's just flagged.

---

## Environment

The scripts in this folder only need a small set of third-party
packages on top of the Python standard library:

| Package | Used by | Why |
| --- | --- | --- |
| `pandas` | OT02 (transitively via `parse_APPN_dataset_path`) | Date parsing / `Timestamp` handling. |
| `tqdm` | OT01, OT02 | Progress bars for copy/move loops. |
| `pyyaml` | OT02 (transitively) and any future YAML-aware script | Reading `mission_data.yaml`, `products.yaml`, etc. |
| `git` (CLI) | All three | `git rev-parse --show-toplevel` for default root resolution. |

### Create with mamba (recommended — faster solver)

```bash
mamba create -n appn-onetime -c conda-forge \
    python=3.11 pandas tqdm pyyaml git
mamba activate appn-onetime
```

### Create with conda

```bash
conda create -n appn-onetime -c conda-forge \
    python=3.11 pandas tqdm pyyaml git
conda activate appn-onetime
```

### Add to an existing environment

```bash
mamba install -n <env> -c conda-forge pandas tqdm pyyaml git
# or
conda install -n <env> -c conda-forge pandas tqdm pyyaml git
```

### Reusing the main project environment

If you already have the project-wide `fire` environment from the repo
root [`environment.yml`](../../environment.yml) activated, you do not
need a separate environment — it is a strict superset of the packages
listed above.



## Running a script

All scripts are run from the repository root so that the
`Code.functions.core_functions.parse_APPN_dataset_path` import resolves
correctly:

```bash
cd /path/to/APPN-42-datastorage
mamba activate appn-onetime
python Code/OT00_OneTimeScripts/OT02_CollectCalvisGobiLogs.py --help
```

Most scripts accept `--dry-run` (or an equivalent) — use it first to
preview changes before letting the script touch disk.
