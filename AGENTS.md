# AGENTS.md — Coding rules for AI assistants

> Machine-readable. Read this first.
> Audience: LLM coding agents (Copilot, Claude, Cursor, Aider, etc.).
> Scope: Python data-science / scientific-analysis scripts in this repo.

---

## 1. Hard rules (must follow)

- **R1** — All executable code lives inside functions. **No top-level work** other than imports, `__title__`/`__author__` metadata, and the `if __name__ == "__main__":` block.
- **R2** — **Functions have no hidden inputs.** Everything a function uses arrives through its signature, is defined inside it, or is fetched by an explicit call — bodies never read bare module-level names. No module-level data of any kind (including `UPPER_SNAKE_CASE` "constants"): tunables → argparse → `main()` → arguments; overridable defaults → inline signature defaults (immutable only; repo-relative paths fine, absolute never); fixed facts (physical constants, unit conversions) → a function/frozen dataclass that callers invoke; local details → inside the function. The only allowed module-level names are: imports, dunder metadata, and the git-root bootstrap variable needed to add the repo to `sys.path`.
- **R3** — `main()` is defined **at the top of the file**, immediately after imports. It reads like pseudocode; complex logic lives in helper functions below.
- **R4** — All functions use **NumPy-style docstrings** with `Parameters`, `Returns`, and (when relevant) `Raises` / `Notes` sections. Include type hints on signatures.
- **R5** — Scripts run from the **git repo root**. The git root is resolved with `gitpython` and added to `sys.path` **at module top** (before any `Code.functions.*` imports), and the `__main__` block `chdir`s into it before calling `main()`. All paths in code are relative to the repo root or come from CLI args.
- **R6** — Use `argparse` for any user-tunable input. No hard-coded paths inside `main()`.
- **R7** — **No Jupyter notebooks** for analysis. Notebooks are for teaching/exploration only.
- **R8** — Prefer existing helpers in `Code.functions.core_functions` (imported as `import Code.functions.core_functions as cf`; e.g. `parse_APPN_dataset_path`, `outputs_up_to_date`, `build_run_metadata`, `write_metadata_yaml`) and the other `Code/functions/` packages over re-implementing them.
- **R9** — Don't add `try/except` around code unless a *specific* failure mode is being handled. No bare `except:`.

## 1a. Decision ladder — before writing code (should follow)

Stop at the first rung that holds — after reading the code the change
touches, never instead of it:

1. Does this need to exist?               → no: skip it (YAGNI)
2. Already in `Code/functions/`?          → import it, don't rewrite (R8)
3. Stdlib does it?                        → use it
4. Scientific stack does it?              → use it (numpy/pandas/xarray/geopandas beat hand-rolled loops)
5. Already an installed dependency?       → use it before adding a new one
6. A few lines?                           → a few lines — not a class, not a wrapper
7. Only then: the minimum that works

The ladder is a SHOULD; rungs 2 and 4 overlap MUSTs (R8). Minimal governs
what gets built *above the floor*, never the floor itself: the canonical
template, docstrings + type hints, argparse, provenance metadata, and
dry-run/confirmation layers on destructive operations are never on the
chopping block.

## 2. Soft preferences

- **P1** — Section banners use `# ========== Title ==========` for major sections and `# +++++ subnote +++++` for inline subsections. Function separators use a line of `=` (~80 chars).
- **P2** — Keep `main()` short. Each step is one call to a helper.
- **P3** — Use `pathlib.Path` over `os.path` strings.
- **P4** — Use `tqdm` for any loop over files/large iterables.
- **P5** — Use `warnings.warn(...)` (imported as `warn`) for recoverable issues; raise for unrecoverable ones.
- **P6** — Plotting: `seaborn` for stats plots, `matplotlib` for fine-tuning. Set style/rcParams once inside the plot function, not at module level.
- **P7** — File-type defaults: `parquet` for tabular data on disk; `csv` only for human-edited / small metadata files.
- **P8** — Print a one-line progress message at the start of each major step (`print(f"Loading {fpath} ...")`).
- **P9** — Avoid copying a function between scripts. If two scripts need it, prefer moving it into `Code/functions/` and importing it.
- **P10** - Use `tqdm` when using for loops instead of print statements.

## 3. Forbidden patterns

- ❌ Module-level data of any kind (`results = []` at top level, `UPPER_SNAKE_CASE` "constants", paths under the imports).
- ❌ Importing `*`.
- ❌ `os.chdir` inside `main()` or helper functions (only allowed in `__main__`).
- ❌ Hard-coded absolute paths (`/mnt/d/...`) in committed code. Use CLI args / repo-root-relative paths.
- ❌ Re-implementing things already in `Code/functions/`.
- ❌ Silent `except: pass`.
- ❌ Adding new top-level scripts that don't follow the template in §4.

## 4. Canonical script template

Every new script in `Code/` MUST match this skeleton:

```python
"""One-line summary.

Longer description of what the script does, its inputs, and its outputs.

Command-line Arguments
----------------------
--foo : str
    Description.
"""

# ==============================================================================

__title__ = "Short title"
__author__ = "Arden Burrell"
__version__ = "v1.0(DD.MM.YYYY)"
__email__ = "arden.burrell@sydney.edu.au"

# ==============================================================================
# ========== Import core packages ==========
import os
import sys
import argparse
import pathlib
from typing import Optional, List

# ========== Import other packages ==========
import git
from git import exc as git_exc
import numpy as np
import pandas as pd
from tqdm import tqdm
import warnings as warn

import matplotlib.pyplot as plt
import seaborn as sns

# ========== Resolve git root (must happen before importing Code.functions.*) ==========
try:
    _git_root = git.Repo(os.getcwd(), search_parent_directories=True
                         ).git.rev_parse("--show-toplevel")
except git_exc.InvalidGitRepositoryError as err:
    raise git_exc.InvalidGitRepositoryError(
        f"Script must be run from inside a git repo (cwd={os.getcwd()})."
    ) from err
if _git_root not in sys.path:
    sys.path.insert(0, _git_root)

# ========== Import custom packages ==========
import Code.functions.core_functions as cf
# import Code.functions.plot_layout as pl

# ==================================================================================
def main(args: argparse.Namespace) -> None:
    """Top-level orchestration. Reads like pseudocode.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.

    Returns
    -------
    None
    """
    # ========== Step 1 ==========
    df = load_data(pathlib.Path(args.input))
    # ========== Step 2 ==========
    df = clean_data(df)
    # ========== Step 3 ==========
    make_plots(df)


# ==================================================================================
def load_data(path: pathlib.Path) -> pd.DataFrame:
    """Load a parquet/csv file.

    Parameters
    ----------
    path : pathlib.Path
        Input file.

    Returns
    -------
    pd.DataFrame
    """
    ...


# ==================================================================================
if __name__ == "__main__":
    # ========== chdir to git root (resolved at module top) ==========
    os.chdir(_git_root)

    # ========== Parse args ==========
    parser = argparse.ArgumentParser(description="...")
    parser.add_argument("--input", type=str, required=True, help="...")
    args = parser.parse_args()

    main(args)
```

## 5. Quick checklist for the agent (run before returning code)

- [ ] `main()` is the first function definition.
- [ ] No globals; no top-level work outside `__main__`.
- [ ] Every function has a NumPy-style docstring + type hints.
- [ ] Section banners use the `# ========== ... ==========` style.
- [ ] CLI args used instead of hard-coded paths.
- [ ] Git root is added to `sys.path` at module top (before `Code.functions.*` imports); `__main__` `chdir`s to it.
- [ ] Reused logic comes from `Code/functions/` (not copy-pasted).
- [ ] No new notebooks for analysis.

## 6. Reference files

- Shared helpers: [`Code/functions/core_functions/__init__.py`](Code/functions/core_functions/__init__.py)
- Note: `ProjectBuilder.py` predates the template and does not follow it (module-level
  work, no NumPy docstrings, `main(args, repo)` signature). Don't use it as a model,
  and don't rewrite it wholesale to comply unless asked.

## 7. Repo map

### What this repository is

A **generic, publishable template** for the APPN aerial-phenotyping data store:
the folder-structure builder plus the QC/extraction pipelines that operate on it.
It ships **no data**.

### Commands

Everything runs from the **repo root** — scripts resolve the git root with
`gitpython`, `chdir` into it, and use it as the default `--path`.

```bash
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
