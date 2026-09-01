# `reference/panels/` — panel DHR reference library

Manufacturer DHR (directional-hemispherical reflectance) JSONs for every
APPN reflectance-panel set. These are the QC02 *expected* reference
curves (QC pipeline plan §5b). One folder per node, one subfolder per
physical set (plain serial, e.g. `UF200-24008/`); physical sticker
labels live in the table below, not in folder names.

**No cross-node fallback.** A set resolves only within its node's
folder; a missing node library is `not_checked`, never a borrowed curve.
Set identification follows plan §5b: the gpro pipeline YAML pin is
primary for ELM tables; nominal signatures cannot identify hardware;
ambiguity is a hard error.

## Inventory

| Node  | Set    | Label     | Panels (nominal %) | Manufactured | Range (nm) | Source |
|-------|--------|-----------|--------------------|--------------|------------|--------|
| AU    | 24005  | APPN1 / APPN-AU1 | 11/30/56/82 | 20240508 | 300–2500 | APEx_Analysis `AU/` (verbatim) |
| AU    | 25005  | APPN-AU2  | 11/30/56/82        | 20250304     | 300–2500   | APEx_Analysis `AU/` (verbatim) |
| AU    | 26004  | APPN-AU   | 20/45              | 20260415     | 300–5000   | APEx_Analysis `AU/` (verbatim) |
| UQ    | 24006  | APPN2     | 11/30/56/82        | 20240529     | 300–2500   | fleet dump, tail-filled† |
| UQ    | 24007  | APPN3     | 11/30/56/82        | 20240529     | 300–2500   | fleet dump, tail-filled† |
| UQ    | 26002  | APPN-UQ   | 20/45              | 20260415     | 300–5000   | APEx_Analysis `UQ/` (verbatim) |
| USYD  | 24008  | APPN4 / Gryfn4P | 11/30/56/82  | 20240529     | 300–2500   | APEx_Analysis `SHARED/` (verbatim, re-homed‡) |
| USYD  | 24009  | APPN5     | 11/30/56/82        | 20240529     | 300–2500   | fleet dump, tail-filled† |
| USYD  | 26001  | APPN-USyd | 20/45              | 20260415     | 300–5000   | APEx_Analysis `USYD/` (verbatim) |
| CSU   | 24010  | APPN6     | 11/30/56/82        | 20240529     | 300–2500   | fleet dump, tail-filled† |
| CSU   | 24011  | APPN7     | 11/30/56/82        | 20240529     | 300–2500   | fleet dump, tail-filled† |
| CSU   | 26003  | APPN-CSU  | 20/45              | 20260415     | 300–5000   | APEx_Analysis `CSU/` (verbatim) |
| UWA   | 24012  | APPN8     | 11/30/56/82        | 20240529     | 300–2500   | fleet dump, tail-filled† |
| UWA   | 24013  | APPN9     | 11/30/56/82        | 20240529     | 300–2500   | fleet dump, tail-filled† |
| UWA   | 26005  | APPN-WA   | 20/45              | 20260415     | 300–5000   | APEx_Analysis `UWA/` (verbatim) |
| DPIRD | 26006  | APPN-DPIRD | 20/45             | 20260415     | 300–5000   | APEx_Analysis `DPIRD/` (verbatim) |

Sources (audited 25.08.2026, plan §5b "Source inventory"):

- **fleet dump** — `USYD_Narrabri/Documents/Sensor Files/GOBI Cal Files/
  Reflectance Panels/` (this repo): all nine 2024-batch sets, curves
  truncated at 2400 nm. Node ownership from the JSON `customer` field.
- **APEx_Analysis** — `APEx_Analysis/data/panels/` (APPN-51 repo):
  full-range exports.

## †Tail-fill provenance (2401–2500 nm)

The seven dump-only 2024 sets (24006, 24007, 24009–24013) were exported
truncated at 2400 nm. **Sets 24006–24013 carry byte-identical DHR curves
per panel** — the manufacturer issued one batch calibration curve for
the 20240529 delivery, verified value-by-value over the full 300–2400 nm
overlap (25.08.2026). Their 2401–2500 nm tail is therefore adopted
verbatim from the full-range `UF200-24008` export. Each filled file
carries a `dhr_tail_provenance` key; metadata fields are the dump
originals. Only 24005 (20240508 delivery) has its own per-set curves
(max deltas vs batch: 0.36/1.49/0.62/3.61 pp for panels 11/30/56/82).

‡24008 was previously filed as `SHARED/UF200-24008-Gryfn4P` in
APEx_Analysis; its `customer` field ("University of Sydney") re-homes it
to USYD.

## Consequences for set identification

All nine 2024-batch sets share the 11/30/56/82 nominal signature —
signature matching cannot distinguish any of them; only the gpro pin
can. Within 24006–24013 a mis-pin changes nothing in expected values
(identical curves) — the pin still matters for provenance and wherever
curves genuinely differ (24005, 25005, the per-set-calibrated 2026
sets).

Integrity is pinned by `Code/DS02_DatasetQA/tests/test_panel_library.py`.
