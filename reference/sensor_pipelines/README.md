# Sensor pipeline registry

This folder defines the expected file/folder structure for each sensor
platform within `T0_raw`, `T1_proc`, and `T2_traits` tiers.
(Moved from top-level `sensor_platform_paths/` per plan archive §5d.)

## Layout

```
reference/sensor_pipelines/
  README.md            # this file
  _schema.json         # JSON Schema for one sensor entry
  _index.json          # metadata + ordered list of sensor codes
  <SENSOR>.json        # one file per sensor (e.g. M3M.json, CALVIS.json)
```

Files prefixed with `_` are not sensor entries. The sensor code is taken
from the file stem (e.g. `CALVIS.json` -> `CALVIS`).

## Sensor file shape

Each `<SENSOR>.json` contains a single object:

```json
{
  "description": "Short human description of the sensor",
  "tier_structures": {
    "T0_raw":   { "expected_folders": { ... }, "optional_folders": { ... } },
    "T1_proc":  { "expected_folders": { ... }, "optional_folders": { ... } },
    "T2_traits": { "expected_folders": { ... }, "optional_folders": { ... } }
  }
}
```

Within `expected_folders` / `optional_folders`:

- The **key** is a folder name (or glob, e.g. `*.gpro`). An empty string
  `""` means "the tier folder itself" (used to declare loose files at the
  tier root).
- The **value** is a list of expected file entries. Each entry is either:
  - a string glob (e.g. `"*VNIR_Orthomosaic.bin"`), or
  - an object: `{"name": "<glob>", "alt_name": "<fallback glob>",
    "size": "<approx size>"}`.

## Loading

[`PS00_ProcessingStatus.py`](../../Code/DS01_StorageReporting/PS00_ProcessingStatus.py)
(`load_registry()`) validates every `<SENSOR>.json` against `_schema.json`
and evaluates its `pipeline` block (`payloads`, `deployables`, `steps[]` with
`done_when` rules). `Code.functions.issue_yaml.load_sensor_pipeline()` reads the
same block for issue-YAML templates. `tier_structures` is descriptive only.

## Adding a sensor

1. Copy an existing `<SENSOR>.json` as a template.
2. Edit the description and tier structures.
3. Add the sensor code to `_index.json` (keeps display order stable).
