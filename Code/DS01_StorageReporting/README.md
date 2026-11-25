# DS01 — Storage Reporting

Read-only reporting over the node data tree.

| Script | Purpose |
| --- | --- |
| [`PS00_ProcessingStatus.py`](PS00_ProcessingStatus.py) | Processing-status scanner: where is every `project/site/sensor/date/run_XX` up to, and what has stalled or been forgotten. |

---

## PS00_ProcessingStatus

### What it does

For every run folder under `<dataroot>/<node>/<project>/<site>/<sensor>/<date>/run_XX` it:

1. **Evaluates the sensor's pipeline** from
   [`reference/sensor_pipelines/<SENSOR>.json`](../../reference/sensor_pipelines/README.md).
   Each `pipeline.steps[]` entry is a named stage with `done_when` rules
   (folder + glob + count/size limits, gated by `requires` payloads /
   deployables). E.g. GOBI/CALVIS:

   | Stage | Done when |
   | --- | --- |
   | `download` | raw capture folders in `T0_raw` (ephemeral — inferred once bundled) |
   | `bundle` | exactly one `*.graw` in `T0_raw` |
   | `process` | one `*.gpro` in `T1_proc` (+ a valid `elm_coefficients.json` when ELM panels + VNIR were flown) |
   | `qc_testing` | QC geojsons/tables in `T1_proc/QC_data` (`QC_ELM*_Panels*`, `QC_VAL*_Panels*`, `QC_GCP_points*`, …) |
   | `qc_report` | all four DS02 contract QC reports (QC00–QC03) generated and not failing (or failures `accepted`) |
   | `raw_cleanup` | loose download folders removed from `T0_raw`; only the `.graw` (+ Vault) remains |

   Sensors without a `pipeline` block report `no_pipeline`. To add or
   change a sensor's stages, edit its JSON (validated against `_schema.json`
   on every run).
2. **Reads operator metadata**: the `Deviations` / `Issues` / `RunFailed`
   bools in the date folder's `RunOverview.csv` and the per-run
   `<date>/run_XX_Issues.yaml` tickets, and folds them into a `run_status`.
3. **Writes issue-YAML templates** for runs whose trigger bools are set
   (created when absent, additively patched; operator content is never
   modified) and backfills QC finding-tickets from existing
   `QC0*_detail.json` reports (`Code.functions.issue_yaml`).
4. **Auto-commits + pushes** changed `*_Issues.yaml` / `RunOverview.csv`
   only (nothing else is ever staged). Use `--no-gitsync` if this checkout
   should not push.
5. **Writes the status outputs** (below).

It never moves, deletes or fixes data.

### Outputs

| Location | Files | Notes |
| --- | --- | --- |
| `<project>/Documentation/ProcessingStatus/` | `state_<SYS>.parquet`, `events_<SYS>.parquet` | regenerable state + append-only transition ledger |
| ″ | `<project>_Status.{csv,xlsx,json}` | human reports |
| `<node>/Documents/StorageReports/ProcessingStatus/` | `Node_Status_<SYS>.parquet` | whole-node state |
| ″ | `Node_Status.{csv,xlsx,json}`, `Sensor/<SENSOR>_Status.{csv,xlsx,json}` | human reports |

`<SYS>` is the `--system` tag (default `WS`). The csv/xlsx is a run × stage
matrix: one row per run with `completion`, `run_status` and one column per
stage.

### Status vocabulary

- **stage status**: `done`, `missing`, `empty`, `failed_check`,
  `not_applicable`, `no_pipeline`.
- **completion** (per run): `complete` (every applicable stage done),
  `pending` (unfinished, moved in the last 14 days), `stalled` (unfinished,
  nothing new for 14 days), `blocked` (a stage fails a quality rule),
  `failed` (run condemned), `no_pipeline`.
- **run_status** (per run, from triggers + tickets): `ok`, `ok_fixed`,
  `ok_deviation`, `degraded`, `under_investigation`, `failed_partial`,
  `failed_total`; `*_undocumented` when a trigger is set but no readable
  issue YAML exists.

`run_status` and `completion` are orthogonal: `ok` only means "no issues
declared", never "nothing left to do".

### Running

Run from inside the repo (it sits at the root of the data tree):

```bash
conda activate datastorage
python Code/DS01_StorageReporting/PS00_ProcessingStatus.py --dryrun                 # report only, write nothing
python Code/DS01_StorageReporting/PS00_ProcessingStatus.py --projects 2026_MyTrial  # one project
python Code/DS01_StorageReporting/PS00_ProcessingStatus.py --no-gitsync             # no metadata commit/push
```

Defaults:

| Option | Default |
| --- | --- |
| `--dataroot` | the git repo root |
| `--node` | the single node in `NodeSummary.yaml` (required if it lists several) |
| `--system` | `WS` (only used to name the parquet outputs; use `NAS` for a second scanning host) |

### Per-project summary from the outputs

The node csv already holds everything for a site → sensor → date rollup:

```python
import pandas as pd
df = pd.read_csv("<node>/Documents/StorageReports/ProcessingStatus/Node_Status.csv")
done = lambda s: (s == "done").sum()
(df[df.project == "<project>"]
   .groupby(["site", "sensor", "run_date"])
   .agg(runs=("run", "nunique"), graw=("bundle", done), gpro=("process", done),
        qc_data=("qc_testing", done), qc_report=("qc_report", done)))
```
