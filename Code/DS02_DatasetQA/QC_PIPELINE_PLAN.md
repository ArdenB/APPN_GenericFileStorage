# DS02 QC Pipeline Plan

Version: v1.22 (01.09.2026)
Status: **in implementation** (2026-08-25) — Phases 1–3 complete; Phase 5
core (PS00 registry move + dual rules) landed, leftovers listed in §7;
Phase 4 copy sync: APPN_GenericFileStorage ✅ (2026-09-01), APEX-data
still deferred. Completion
markers in §7. Store re-QC in progress — paused before the CaliWeek QC03
batch pending the §7 revision pass.
Scope: unify per-run QC and cross-run QA scripts under one naming scheme, one
reporting contract, and one threshold-config pattern. No orchestrator — scripts
are run manually in numbered order.

Changelog:

- v1.22 — new PARKED design item (§5e, 2026-09-01): lax-path mode for
  non-compliant trees, motivated by the `2026_York_F` support case (an
  invalid site-folder name made QC01/QC02 find nothing; QC00 v2.1 /
  QC01 v2.1 / QC02 v3.4 / QC03 v1.4 now skip loudly with the parse
  errors — this item is the deferred opt-in to process anyway).
  Side-doc retirement (operator): `QC03_ZeroClass_BACKPORT.md` and
  `QC02_HomogeneityCheck_PLAN.md` deleted — fully absorbed into §5c /
  §7 Phase 3 + the `spectral_limits.yml` provenance comments (the
  backport doc's ~100 %-zero-band note is promoted to §7 revision
  item 5); recoverable via git history; in-code references repointed
  to this plan.
- v1.21 — APPN_GenericFileStorage copy sync done (Phase 4, 2026-09-01,
  generic commit dbba284): renames + all seven contract scripts,
  `qc_report`/`issue_yaml` packages, spectral_qc/core_functions updates,
  `reference/thresholds` and — **operator decision overriding the §5d
  split** — `reference/panels` with the real DHR curves (fine to share);
  `reference/sensor_pipelines` stays master-only (no PS00 in the generic
  repo). 259 tests pass there. APEX-data sync still deferred.
- v1.20 — QC03 masking audit closed (§7 revision item 2, §5c rewritten,
  2026-08-26): the all-bands-zero mask is no longer all "background".
  The gpro capture polygon (`extents/hyper_extent.geojson`) is the
  analysis domain — outside is discarded, the polygon eroded by half the
  swath margin is the graded ROI (`dropout_in_roi`), the ring between
  them is the advisory edge band (`zero_edge_band`), and
  `data_outside_bbox` guards the extent/raster pairing. `footprint` is
  unchanged so QC03 history stays comparable. Landed in QC03 v1.2.
- v1.19 — revision-pass additions (operator, 2026-08-26): the
  cross-script report gets its concrete filename (`QC_report.md`,
  §7 revision item 3) and a new item 4 — QA summaries gain markdown
  reports with embedded figures, so cross-run outputs are readable
  without opening YAML + PNGs side by side.
- v1.18 — §5e/§5f merged into one §5e “PARKED / stretch goals”
  section (structure only, no content change): intent-aware QC
  applicability, payload-aware ticket gating, and duplicate-run
  handling / final-run designation are now sub-items of a single
  deferred-ideas home. §5f references in older changelog entries read
  against the pre-merge layout.
- v1.17 — new PARKED design item (§5f, 2026-08-26): duplicate-run
  handling + final-run designation. Duplicates usually trial different
  processing methods; sometimes one of the dups should be picked as
  the canonical dataset for downstream analysis. Design open — today
  `DuplicateRun` is a flat exclusion toggle with no way to record
  which sibling won.
- v1.16 — stretch goal added to §5e (2026-08-26): payload-aware ticket
  gating, deferred from the RunFailed-awareness retrofit currently
  landing in the QA scripts. Tickets are per-payload (e.g. lidar
  failed, hyperspec ok), so in principle QA02 could ignore a
  lidar-only issue; not done in v1 because mapping ticket payload
  names to script domains is fragile, but the `issue_yaml` helper can
  return the per-payload states so it's a cheap later addition.
- v1.15 — QC02 revision item made concrete (§7, operator, 2026-08-26):
  treat 0 as nodata/NaN (account + report, incl. all-NaN panels), align
  figure design with QA02, and handle runs with two ELM panel sets.
  Test bed: CongWhiteHeads CALVIS 20260819 (SWIR gap over one panel
  set; run_04 dual ELM).
- v1.14 — operator revision pass queued (§7, 2026-08-26): QC02
  figure/reporting redesign, QC03 masking audit (possible conflation of
  not-in-capture-area with scan-line skips), and a cross-script
  markdown QC report. The long CaliWeek QC03 batch (39 runs ≈ 7 h) is
  deliberately held until after the revision so outputs aren't computed
  twice.
- v1.13 — new PARKED design item (§5e): intent-aware QC applicability
  (QC01/QA01 consult Issues.yaml intent to gate check applicability,
  never verdicts). Deliberately deferred until the dashboard refresh +
  store re-QC complete; exact design still under operator consideration.
- v1.12 — `qc_report` tracks all four contract reports (operator
  decision, 2026-08-25): verdict-gating and presence-tracking are
  different things — PS00's value is surfacing work-to-do, and the
  run-the-new-QC migration was invisible while three of four reports
  were ignored. The stage's `done_when` is now an AND-list: QC00
  (dual-rule as before), QC01 (`pass|warn` — it already grades real
  verdicts via the spec checks), QC02/QC03 presence-tracked
  (`pass|warn|not_evaluated`; only `fail` blocks) with payload
  `requires` gates. Runs regress to `missing` until the new QC is run
  on them — that regression IS the migration to-do list.
- v1.11 — Phase 5 PS00 package landed (§5d/§8): `sensor_platform_paths/`
  → `reference/sensor_pipelines/` (loader paths in PS00 + `issue_yaml`,
  docs); PS00 gains `any_of` (any-match sub-rules) and list-valued
  `json_equals`; CALVIS/GOBI `qc_report` now dual-rule — legacy loose
  report OR contract QC00 detail (`status` pass|warn). QC02 stays out of
  `done_when` until its checks grade real verdicts (currently
  all-advisory → `not_evaluated`), same principle as QC03's exclusion.
- v1.10 — §5b resolution refinement (review finding, 2026-08-25): the
  original "ambiguity is a hard error" rule was too blunt — every node
  fields two 4-panel sets plus a 2-panel set, so the gpro pin on the ELM
  set identifies the VAL set **by elimination**; and candidates that are
  numerically identical (the 20240529 batch calibration, 24006–24013)
  resolve harmlessly with all serials recorded. The hard error now
  applies only to genuinely differing candidates (AU 24005 vs 25005).
- v1.9 — panel-library source audit (§5b): the full 2024-batch fleet
  (UF200-24005…24013, APPN1–APPN9, all four 11/30/56/82 panels each) was
  found in `USYD_Narrabri/Documents/Sensor Files/GOBI Cal Files/
  Reflectance Panels/`; customer metadata resolves node ownership
  (incl. SHARED 24008 → USYD, confirming the v1.8 migration guess).
  Dump copies are truncated at 2400 nm — the APEx_Analysis copies
  (300–2500 nm, byte-identical over the overlap) win where both exist.
  Batch finding: 24006–24013 curves are byte-identical per panel (one
  batch calibration, only 24005 differs) → the truncated sets' 2400–
  2500 nm tail fills from the 24008 full-range export.
- v1.8 — drop the `SHARED/` cross-node panel folder (§5b/§5d): the design
  target is a complete per-node library (every node's panel files are being
  collected; not all in hand yet). No cross-node fallback — a panel set
  resolves only within its node's folder.
- v1.7 — declutter `QC_data/` (§4): summary YAMLs at top level, all detailed
  artefacts (detail JSON, plots, tables) in one subfolder per script;
  existing input files (panel polygons, GCP points, `QC_Spectral_Tables/`)
  stay put. Registry rule paths in §8 updated to match.
- v1.6 — new top-level `reference/` folder (new §5d): canonical in-repo home
  for the panel DHR library (`reference/panels/`), threshold spec YAMLs
  (`reference/thresholds/`) and the PS00 registry (`reference/
  sensor_pipelines/`, ex `sensor_platform_paths/`). Wiki runtime download
  rejected (§9); generic repo ships structure + loader only.
- v1.5 — add downstream consumers (new §8 + Phase 5): PS00 pipeline-registry
  updates (`sensor_platform_paths/*.json` qc_report rules → contract
  filenames/status key, dual-rule transition) and the DataSync dashboard
  stage-matrix review.
- v1.4 — add net-new QC03_RasterCheck (per-run VNIR/SWIR reflectance .bin
  data-validity scan: zeros in footprint, over-range > 10000, NaN/Inf,
  header↔bin integrity) + reserved QA03_RasterComparison counterpart (new §5c).
  Advisory at first; gates DS03/DS05 once background rates are calibrated.
- v1.3 — swap QA/QC semantics to match ISO usage and on-disk conventions:
  **QC = per-run product checks** (write to `QC_data/`, consume `QC_*`
  panel files), **QA = cross-run process monitoring**. Cross-run
  destination renamed `QAReports/` (§4).
- v1.2 — final script names: suffix `Check` for per-run, `Comparison` for
  cross-run, paired subjects GCP / Flight / Spectral.
- v1.1 — fold in DT01_GryfnPanelComparison (APEx_Analysis) findings: panel
  reference library + gpro set pinning (new §5b), DHR-based expected values
  and first empirical spectral thresholds (§5), bad-band updates (§5),
  DT01 per-run comparison → QA02 and aggregate stats → QC02 (Phase 3).
- v1.0 — initial plan (workshop decisions).

## 1. Script set and execution order

Two families, matching indices between counterparts (QCxx ↔ QAxx):

| Order | Script (new name)        | Origin                          | Scope     | Purpose |
|-------|--------------------------|---------------------------------|-----------|---------|
| 1     | QC00_GCPCheck            | QA01_PointDistanceComparison    | per-run   | GCP geometric accuracy — **the gate** |
| 2     | QC01_FlightCheck         | DT00 (APEx_Analysis)            | per-run   | acquisition params + FlightCal spec check + bundle integrity checks |
| 3     | QC02_SpectralCheck       | QA00_SpectralValidation         | per-run   | panel spectra extraction/validation |
| 4     | QC03_RasterCheck         | net-new                         | per-run   | VNIR/SWIR reflectance .bin data validity (§5c) |
| 5     | QA00_GCPComparison       | QA03_GCPRunComparison           | cross-run | GCP accuracy across runs |
| 6     | QA01_FlightComparison    | R00 (APEx_Analysis)             | cross-run | acquisition anomalies across runs |
| 7     | QA02_SpectralComparison  | QA02_SpectralRunComparison      | cross-run | spectral stability across runs |
| —     | QA03_RasterComparison    | reserved (not built)            | cross-run | bad-pixel/over-range rates across runs — built once QC03 background rates exist |

(The QA03 index is free once the rename sequence below moves the old
`QA03_GCPRunComparison` to `QA00_GCPComparison`.)

Dependency rule: **a QC00 fail invalidates everything downstream.** If GCP
accuracy fails, the run needs GNSS reprocessing, which changes the trajectory
and therefore voids QC01 (AGL/speed/sidelap are trajectory-derived) and QC02
(pixel placement). GNSS reprocessing resets pipeline status back to QC00.

There is deliberately **no orchestrator script**. Run order and prerequisites
live in this doc and the README; each script is standalone.

### Collision-safe rename sequence (single migration commit) — ✅ DONE (8e47ee3)

1. `QA02_SpectralRunComparison.py` → `QA02_SpectralComparison.py`
2. `QA03_GCPRunComparison.py` → `QA00_GCPComparison.py`
3. `QA00_SpectralValidation.py` → `QC02_SpectralCheck.py`
4. `QA01_PointDistanceComparison.py` → `QC00_GCPCheck.py`
5. Port in `QC01_FlightCheck.py` (ex-DT00) and
   `QA01_FlightComparison.py` (ex-R00) from APEx_Analysis.

`QA00_HomogeneityCheck_PLAN.md` renames to match its script (→ QC02).

## 2. Reporting contract (all seven scripts)

Dual-file output per script per invocation, JSON-first:

- **`<script>_summary.yaml`** — human-scannable: run/scope identity, statuses,
  one line per check (status + headline value + optional note), pointer to the
  detail file, artifact list. Small, git-diff friendly.
- **`<script>_detail.json`** — everything: full check objects (value, threshold
  expression, units, evidence arrays), per-line/per-panel/per-segment data,
  config snapshot (spec YAML path + hash), cache provenance, warnings log.

The YAML is a pure projection of the JSON: scripts build the JSON dict first,
then derive the summary — the two can never disagree. Shared
`schema_version` pair, bumped together.

Summary skeleton:

```yaml
schema_version: 1.0
script: {name: QC01_FlightCheck, version: "..."}
run: {node: AU, project: 2026_APEx, site: 2026Rosedale, sensor: CALVIS,
      date: 20260624, run_number: run01,                # parse_APPN_dataset_path
      gpro: 20260624_APEx_CaLVIS_1.gpro,                # bundle identifiers
      graw: 20260624_APEx_CaLVIS_1.graw}
generated_utc: ...
status: warn                      # pass | warn | fail | not_evaluated
checks:
  sidelap_vnir_fieldbook: {status: good, value: "46.1-47.7 %"}
  sidelap_swir_fieldbook: {status: warning, value: "29.6-31.8 %", note: "target > 30 %"}
detail: QC01_FlightCheck/QC01_FlightCheck_detail.json   # §4 per-script subfolder
artifacts: [QC01_FlightCheck/flight_lines.csv, QC01_FlightCheck/QC_plots/...]
```

Staleness detection: every detail JSON records the identity of the
trajectory/bundle it was computed against (gpro path + mtime, POSPac extract
log name). After GNSS reprocessing, downstream reports are *detectably* stale
rather than silently wrong.

## 3. Status vocabulary (shared enum, shared `worst()`)

Two levels:

- **Check level:** `good | acceptable | warning | fail | not_checked`
  (DT00 spec-check scale, kept for its granularity).
- **Script/run level:** `pass | warn | fail | not_evaluated`, derived by
  worst-wins collapse: `good/acceptable → pass`, `warning → warn`,
  `fail → fail`; all `not_checked` → `not_evaluated`.

One implementation in the shared helper — no per-script vocabularies.

## 4. Output locations

**QC (per-run):** in-tree `<run>/T1_proc/QC_data/` (existing location,
unchanged). Reports travel with the dataset through sync. Layout is
two-level to keep the folder scannable — current `QC_data/` folders are
cluttered:

```
QC_data/
  QC00_GCPCheck_summary.yaml        # summaries only at top level —
  QC01_FlightCheck_summary.yaml     # ls QC_data/ answers "what state is
  QC02_SpectralCheck_summary.yaml   # this run in?"
  QC03_RasterCheck_summary.yaml
  QC00_GCPCheck/                    # one subfolder per script: detail JSON,
  QC01_FlightCheck/                 # plots, tables — a rerun regenerates
  QC02_SpectralCheck/               # the whole folder
  QC03_RasterCheck/
  QC_ELM*_Panels.geojson            # existing *inputs* stay at top level
  QC_GCP_points*.geojson            # (human-created, not script-owned)
  QC_Spectral_Tables/
```

Legacy loose report files migrate into their script's subfolder in Phase 2;
the `qc_report` reader (§6) accepts both locations during transition.

**QA (cross-run):** never inside run folders. Routed by the scope argument:

| Scope   | Destination |
|---------|-------------|
| Node    | `<Node>/Documents/QAReports/` |
| Project | `<Project>/Documentation/QAReports/` |
| Site    | `<Site>/Documentation/QAReports/` |
| Sensor  | `<Site>/Documentation/QAReports/` — flat, sensor in filename |

(`QAReports/` was `QCReports/` — any existing folders rename in Phase 2.)

Filenames always carry the scope so crawls at different scopes never clobber:
`QA01_AU-2026Rosedale-CALVIS_summary.yaml`. The routing helper already in
QA00/QA02 (ex-QA03/ex-QA02_SpectralRunComparison) moves into
`functions/core_functions/` and all three QA scripts use it; QA01 (ex-R00)
currently has no routing and adopts it.

## 5. Thresholds

All thresholds live in config YAML under `reference/thresholds/` (§5d),
following the `flightcal_spec.yml` pattern
(spec file + fixture regression test). Applies to:

- FlightCal/fieldbook spec limits (already done — file migrates with QC01)
- QC00 GCP limits (ex hardcoded `QAConfig`: 0.10 m 2D, 0.04 m bias,
  0.50 m height) — ✅ `gcp_limits.yml` (3071a81)
- QA01 anomaly rules (exposure mismatch, no-panels, solar window, AGL drift,
  sun-abeam — ex hardcoded in R00) — ✅ landed as explicit checks in the
  Phase 2 port
- QC02 spectral limits — first empirical anchors from DT01 (60+ tables,
  correct set pinned): VNIR |bias| vs DHR reached ±0.6 % on the CaliWeek
  reference days, so ~1.5–2 % thresholds are realistic; SWIR is only
  meaningful after bad-band masking (masked biases mostly ±2 %).
  Provenance: APEx_Analysis `results/00.DataProcessing/DT01_GryfnPanelComparison/`
  — ✅ `spectral_limits.yml` (be4c0ec; within-day drift block d39a2cf)
- QC02 panel-homogeneity thresholds (`skew`, `l_kurt`, mean–median
  divergence — calibrated per `QC02_HomogeneityCheck_PLAN.md` step 1
  (doc retired 2026-09-01, in git history);
  recorded here, not in the `QAConfig` docstring) — ✅ per-EM-region
  `homogeneity` block in `spectral_limits.yml` (ec44954; anchors +
  verification in the YAML comments)
- Bad-band ranges (`spectral_qc.default_bad_wavelengths`) updates from
  DT01: the 1900 nm water band is wider than 1790–1960 on some runs
  (residual spike at ~1965 nm) → widen to ~1990; first VNIR candidates:
  repeatable +2 % artefact at 400–420 nm, noise beyond ~920 nm.
  — ✅ applied (be4c0ec)
- QC03 raster-validity fractions (zero-in-footprint %, over-range %,
  NaN/Inf %) — no empirical anchors yet; ship as advisory-only defaults
  and calibrate from the background rates QC03 itself accumulates (§5c).
  — ✅ `raster_validity.yml` uncalibrated defaults (42de99b);
  calibration still open

## 5b. Panel reference library + physical set identification (from DT01)

The manufacturer DHR JSONs are the QC02 *expected* reference — validated
by DT01 over 60+ tables. The library's canonical home is
`reference/panels/` (§5d): one folder per node, **complete per-node
coverage as the design target**. The APEx_Analysis `SHARED/` cross-node
folder is dropped: there is **no cross-node fallback** — a set resolves
only within its node's folder, and a missing node library is
`not_checked`, never a borrowed curve. (This generalizes the old "AU
never falls back to SHARED" rule to all nodes — nominally-identical sets
differ by real percentage points, §5b rule 1.)

### Source inventory (audited 25.08.2026) — ✅ library built (9c84407)

Two sources feed the `reference/panels/` build:

1. **The 2024-batch fleet dump** — `USYD_Narrabri/Documents/Sensor
   Files/GOBI Cal Files/Reflectance Panels/`: nine complete sets
   `UF200-24005…24013` (folders labelled `APPN1`–`APPN9`), each with all
   four 11/30/56/82 panels. The JSON `customer` field resolves node
   ownership:

   | Sets | Label | Node |
   |---|---|---|
   | 24005 | APPN1 | AU (Adelaide) |
   | 24006, 24007 | APPN2–3 | UQ |
   | 24008, 24009 | APPN4–5 | USYD |
   | 24010, 24011 | APPN6–7 | CSU |
   | 24012, 24013 | APPN8–9 | UWA |

   This settles the v1.8 migration note: the APEx_Analysis `SHARED/`
   `UF200-24008-Gryfn4P` set is **USYD's** (customer field) → re-homes to
   `panels/USYD/`. The dump's DHR curves are truncated at 2400 nm
   (2101 pts) vs the APEx_Analysis exports' 2500 nm (2201 pts); overlap
   values are identical — **resolved** by the batch finding below: the
   library gets full-range curves for all nine sets.

   **Batch finding (checked 25.08.2026): sets 24006–24013 carry
   byte-identical DHR curves per panel** — the manufacturer issued one
   batch calibration curve for the 20240529 delivery, not per-set
   measurements. Only 24005 (20240508 Adelaide delivery) has its own
   curves (max deltas vs batch: 0.36/1.49/0.62/3.61 pp for panels
   11/30/56/82). Consequences:

   - The 2400–2500 nm tail for the seven dump-only sets is filled
     verbatim from the full-range 24008 (USYD) APEx export — justified
     by identity over the whole 300–2400 nm overlap. Record the tail
     provenance in the library README; no manufacturer re-export needed.
   - 24005's full-range export already exists in APEx `AU/`.
   - Within 24006–24013 a mis-pinned set changes *nothing* in QC02
     expected values (identical curves); the gpro pin (§5b rule 1) still
     matters wherever curves genuinely differ — 24005 vs the batch,
     25005, and the per-set-calibrated 2026 sets — and for provenance.
2. **APEx_Analysis `data/panels/`** — the 2026-batch two-panel (20/45)
   sets: 26001 USYD, 26002 UQ, 26003 CSU, 26004 AU, 26005 UWA,
   26006 DPIRD, plus AU's 25005 and the fuller 24005/24008 exports.

Remaining gaps after both sources: none — DPIRD has no 2024-batch set
(2026-batch only), which is expected, not missing, and the 2400 nm
truncation is closed by the batch-curve tail fill above.

✅ DONE (9c84407): `reference/panels/` holds all 16 sets (52 JSONs,
plain-serial set folders, labels in the README table). The seven
tail-filled files carry a `dhr_tail_provenance` key and the build
asserted 300–2400 nm identity before adopting each tail;
`Code/DS02_DatasetQA/tests/test_panel_library.py` (21 tests) pins the
inventory, grid completeness, signatures, batch-identity/24005-differs
findings and customer→node ownership.

The audit also hardens §5b rule 1: **all nine 2024-batch sets share the
11/30/56/82 signature** — signature matching cannot distinguish any of
them; only the gpro pin can (even though, per the batch finding, the
stakes within 24006–24013 are provenance-only).

Set identification rules (order matters; refined in v1.10):

1. **The gpro pipeline YAML is the primary pin** (`target_location:
   ...UF200-24005-11.json` records the physical set ELM actually used).
   Nominal `Panel_ref` signatures cannot identify hardware: 24005 and
   25005 share the 11/30/56/82 signature but differ by ~3 pp in SWIR
   (see APEx_Analysis `docs/outgoing/AU_panel_set_differences/`).
2. The gpro pin applies to **ELM tables only** — VAL panels are different
   hardware; they resolve by node + signature, then **by elimination**:
   each node fields two 4-panel sets, so excluding the gpro-pinned ELM
   set identifies the VAL set (v1.10).
3. **Genuinely differing candidates are a hard error, never
   warn-and-pick-newest** — the manufacture-date tie-break is exactly
   what processed a CaliWeek dataset against the wrong set (SWIR biases
   ±4.5 % → ±2 % on fix). Candidates whose DHR curves are numerically
   identical for the needed codes (the 2024 batch calibration) resolve
   to the first with every serial recorded — the choice cannot change a
   verdict (v1.10).
4. `identify_panel_set()` (signature match) stays for labelling/fallback.

New QC02 check candidate: `panel_set_pinned` — the gpro target files
resolve to exactly one known reference set; detects cross-overs between
sets flown concurrently.

## 5c. QC03_RasterCheck — reflectance .bin data validity (net-new)

Scans the **ortho reflectance products only** (VNIR + SWIR ENVI `.bin`/`.hdr`
pairs under `<run>/T1_proc/`) — radiance and intermediates are out of scope.
Values are reflectance ×10⁴, so the physical range is 0–10000.

Check set:

- `zeros_in_footprint` — fraction of pixels with reflectance = 0 *inside the
  data footprint*. Background/nodata is also 0, so footprint must be
  established first (all-bands-zero ⇒ background; zero in some bands but not
  others ⇒ suspect data). Reported per band and whole-cube. **Definition
  deliberately unchanged** by the zone split below, so the history
  accumulated for the QA03 calibration stays comparable.
- `dropout_in_roi` — all-bands-zero fraction of the **ROI** (graded). A
  dropout that zeros *every* band is absorbed into "background" by the
  footprint definition and is invisible to `zeros_in_footprint`, so the
  zone split below re-exposes it.
- `zero_edge_band` — all-bands-zero fraction of the bbox-minus-ROI ring
  (**advisory, never graded**): expected incomplete capture at the swath
  edges, which the GOBI QA appendix calls a known failure mode.
- `data_outside_bbox` — nonzero pixels outside the capture polygon at 0.5 px
  tolerance. A sanity guard on the extent/raster pairing (stale or mismatched
  extent), not a data metric — no zone is reported for the outside area.
- `capture_extent` — the polygon is required wherever an orthomosaic exists
  (whole-store survey 2026-08-26: 72/72 gpros with processed hyperspec products
  carry a valid convex CRS84 polygon; the 8 without are `RunFailed` or
  RGB/LiDAR-only by intent, all ticketed). Absence warns and downgrades the
  split to the fallback classifier.
- `over_range` — fraction of pixels > 10000 (impossible reflectance;
  ELM extrapolation / specular / saturation tell). Per band + whole-cube,
  plus max value and worst-band identification.
- `negative` — fraction < 0 (signed dtypes only).
- `nan_inf` — NaN/Inf counts (float products only).
- `header_bin_integrity` — `.bin` size matches `lines × samples × bands ×
  dtype` from the `.hdr`; wavelength list length matches band count
  (reuses `cf.band_wavelengths`).
- Candidates (record, don't gate): all-constant bands, along-track striping.

**Zone split — the capture polygon is the analysis domain.** The gpro ships
the flown-area polygon (`<gpro>/extents/hyper_extent.geojson`, CRS84,
convex, 4–9 vertices). Everything outside it is discarded (background by
construction; its only use is the `data_outside_bbox` guard). All-bands-zero
pixels inside it split into the advisory edge band and the graded ROI, where

    ROI = bbox eroded per edge by  inset_factor × max(short_axis_fraction ×
          short_axis,  line_spacing)

with `short_axis` = mean of the two shortest polygon edges, `line_spacing` =
median of QC01's `flight_lines.csv` (fallback when QC01 has not run: drop the
term and use the short-axis rule alone), and the geometry constants in
`raster_validity.yml` (`zero_zones`). Fieldbook basis: preflight §1 survey
polygon = AOI + panels/GCPs + 5 m buffer; GOBI step 2 buffers the capture
polygon perpendicular to the lines by ≥ 1 line spacing; `Standard-Flight.md`
puts the effective capture area ~10 % inside per edge. `inset_factor` = 0.5 is
an **operator decision** (2026-08-26 trial): the full margin barely moved the
edge-band figure but absorbed real artefact into it. Implementation is a
half-plane point-in-convex-polygon test on pixel centres (no rasterisation),
which also yields the `inset = -0.5 px` tolerance band for the guard.
**Fallback classifier** when no usable polygon exists: `scipy.ndimage.label`
on the all-zero mask — border-connected components are out-of-capture,
interior components are dropout. The interior-connected share of the ROI
dropout is reported as evidence under either classifier, so the composite
rule (`all_zero ∧ ROI ∧ interior-connected`) can be calibrated later without
a re-scan. Regression anchor (CongWhiteHeads CALVIS 20260819 run_01 SWIR,
103.6 Mpx): 68.85 % all-zero, 67.8 % outside bbox, edge band 16.1 % of a
6.09 Mpx ring, ROI dropout 95,833 px = 0.351 % of 27.27 Mpx (84 %
interior-connected), `data_outside_bbox` = 0.

Reporting per the §2 contract; detail JSON carries per-band stats
(min/max/mean/percentiles, bad-pixel fractions) so QA03 can aggregate later.
Bad-band masking (§5 ranges) applies before whole-cube roll-ups so known-bad
SWIR bands don't dominate the fractions.

Gating: **advisory for now** — thresholds report `warning`/`fail` but nothing
downstream is voided. Once background rates are understood (via the reserved
QA03_RasterComparison), QC03 becomes a gate for DS03 plot extraction and DS05
index maps. Unlike QC01/QC02, a QC00 GNSS reprocess does *not* void QC03
(values are ELM-derived, not trajectory-derived), but an ELM reprocess does —
the §2 staleness fields (gpro path + mtime) cover this.

## 5d. `reference/` — repo-shipped reference files (new top-level folder)
One canonical home for git-versioned files that scripts consume read-only.
Not named `data/` — that would read as collected data next to the
`USYD_Narrabri` tree.

```
reference/
  panels/<NODE>/               # §5b DHR library — one folder per node, no SHARED/,
                               # no cross-node fallback; built from the two §5b sources
  thresholds/                  # §5 spec YAMLs (flightcal_spec.yml, GCP/spectral limits, ...)
  sensor_pipelines/            # ex sensor_platform_paths/ (PS00 registry, _schema.json, README)
```

Decisions:

- **In-repo, never wiki-fetched at runtime.** QC02 must run offline
  (field/WS), PS00 scans are scheduled and unattended on two hosts, and a
  wiki edit changing QC verdicts with no repo commit breaks
  reproducibility — the exact failure class §5b rule 3 exists to prevent.
  The §2 config snapshot (spec path + hash) stays meaningful only if the
  spec is a repo file. The wiki gets a one-way *documentation export*
  (panel-set signature/manufacture-date table à la
  `AU_panel_set_differences`), never a runtime source.
- **Generic repo split** (revised v1.21): APPN_GenericFileStorage ships
  `reference/thresholds` and the full `reference/panels` DHR library —
  the real curves are operator-approved to share (2026-09-01; supersedes
  the earlier anonymised-example-only rule). Only the
  `reference/sensor_pipelines` PS00 registry stays master-only (the
  generic repo has no PS00 consumer).
- **The `sensor_platform_paths/` move is a live-path change**: PS00's
  registry loader, README cross-links, and the scheduled scans on WS +
  mint all point at the old path. One commit (loader path + files + docs),
  both hosts pull before their next scan — coordinated in Phase 5, same
  class as the registry-rule warning there. ✅ DONE (v1.11 package).
- Fixture tests pin each panel set's signature (the `flightcal_spec.yml`
  spec-file + regression-test pattern, §5).

## 5e. PARKED / stretch goals — deferred design ideas (none final)

One home for ideas that are agreed worth doing but deliberately not
being designed or built yet. Nothing here blocks the phases in §7;
each item records its motivating case and any scope boundaries already
agreed, so the eventual design starts from decisions, not archaeology.

### Intent-aware QC applicability

Idea (2026-08-25, deferred until the dashboard refresh + store re-QC
complete; operator still weighing the exact design): QC01/QA01 consult
the run's Issues.yaml intent so bundle-shaped absences grade honestly.
Motivating case: a `LiDAR+RGB`-only gpro has no hyperspec flight lines
*by design* — today that reads "skipped, gpro incomplete", identical to
a genuinely broken bundle; likewise `no_graw`/`panels_present` warn on
runs whose tickets document panels-not-placed or raw-not-kept.

Scope boundary agreed up front, everything else open:

- **Intent may gate what QC checks, never what verdict a performed
  check gets.** Applicability only: a check whose payload was not
  intended/placed (or has a closed-`failed` ticket shrinking
  expectations) grades `not_checked`/`not_applicable` — PS00's
  `gate_met` semantics ("unmet = N/A, never failed") ported down one
  layer, one vocabulary.
- **No verdict-softening.** `caution`/`fixed` tickets never downgrade a
  measured QC fail — waivers stay PS00's job, in exactly one place.
- QA01's `no_panels`/`no_graw` anomaly rules need parity or they
  re-raise what QC01 scoped out.
- Issues.yaml becomes a QC01 input: joins the mtime cache set and the
  §2 config/provenance snapshot (ticket edits invalidate cached
  reports — intended).
- The intent parser promotes from PS00 internals into
  `Code/functions/issue_yaml/` (shared, one implementation).

### Payload-aware ticket gating

Stretch, deferred from the RunFailed-awareness retrofit (2026-08-26):
tickets are per-payload (e.g. lidar failed, hyperspec ok), so in
principle QA02 could ignore a lidar-only issue. Not for v1 — mapping
ticket payload names to script domains is fragile — but the
`issue_yaml` helper can return the per-payload states, so it's a cheap
later addition.

### Duplicate-run handling / final-run designation

Idea (2026-08-26): duplicate runs (`DuplicateRun` in RunOverview.csv)
usually exist to trial different processing methods on the same
acquisition. In some cases one of the duplicates — not the primary —
turns out to be the better product, and we'd want to designate it the
**final run**: the canonical dataset downstream analysis (DS03/DS05)
should consume.

Open questions, nothing decided:

- Today `DuplicateRun` is a flat boolean the QA scripts exclude by
  default (`--include-duplicates` to opt in); there is no way to record
  which sibling of a duplicate group is canonical, or why.
- Where the designation lives — RunOverview.csv column, Issues.yaml
  block, or a sidecar — and who consumes it (QA default inclusion set,
  PS00 `done_when` scoping, DS03/DS05 crawl filters).
- Interaction with QC verdicts: picking a final run is a curation
  decision informed by QC/QA output, not a verdict the scripts compute
  — same one-place-for-waivers principle as the intent-aware item
  above.

### Lax-path mode for non-compliant trees

Idea (2026-09-01): an opt-in to run the per-run QC scripts on trees
that fail `parse_APPN_dataset_path` validation — a mis-named folder
(the motivating support case: a site folder named `2026_York_F`, where
the underscore after the year makes it parse as a project folder) or a
date/run folder passed without its parent folders (data copied to
scratch/USB, a share mounted at date level). Since QC00 v2.1 / QC01
v2.1 / QC02 v3.4 / QC03 v1.4 (2026-09-01) the scripts skip such trees
loudly with the parse errors as the reason — the right default; this
item is the deliberate opt-out.

Sketch agreed 2026-09-01, nothing built:

- `parse_APPN_dataset_path` gains a `clear_on_invalid=True` kwarg —
  lax callers receive best-effort fields (whatever parsed cleanly)
  plus `valid=False` + `errors`, instead of today's all-None wipe on
  validation failure.
- QC00/QC01/QC03 gain `--lax-paths`: warn with the errors and process
  anyway. Their checks are bundle-driven (gpro/graw/product contents),
  so only the report's `run` identity block degrades (salvaged fields,
  None elsewhere).
- QC02 is only viable with explicit overrides — `--sensor` for the
  ortho region set (VNIR vs VNIR+SWIR) and `--node` for the §5b panel
  library resolution (no cross-node fallback) — else the DHR checks
  grade `not_checked`.
- DS03/DS05 stay strict, not part of this item: extracts without
  site/plot identity are data pollution, and PE01 cannot locate
  `{YYYYSite}_plots.geojson` without a site folder anyway.

Open question: reports written with null/partial run identity vs PS00
and the QA scripts — likely harmless (both crawl the compliant store,
which a lax-path tree is by definition outside of), but unverified;
strict stays the default regardless.

## 6. Shared helper (`functions/qc_report/`) — ✅ DONE (cf4a98d)

New package alongside `core_functions` / `spectral_qc` / `gcp_qc`:

- status enums + `worst()` (section 3)
- JSON-first report writer + YAML-summary projector (section 2)
- report reader, tolerant of legacy filenames/schemas (pre-migration JSON
  reports in existing `QC_data/` folders remain readable)
- threshold-config loader (section 5)
- unit tests

## 7. Phases

1. **Phase 1 — `qc_report` helper** in `Code/functions/`, with tests.
   ✅ DONE (cf4a98d, 2026-08-25; scope-aware QA filenames added in 7bc075f).
2. **Phase 2 — renames + migration**: the rename sequence above; port
   DT00/R00 to dataset-tree traversal, contract outputs, and routed locations;
   move `flightcal_spec.yml` + its fixture test into this repo. Bundle
   integrity checks (gpro completeness, ELM-failed/radiance tell, dark-ref and
   panel presence) stay inside QC01 as first-class checks.
   ✅ DONE (renames 8e47ee3; ports + QAReports routing/folder renames +
   `reference/thresholds/` 7bc075f; validated end-to-end on
   2025_MenindeeLakes).
3. **Phase 3 — retrofit ex-QA scripts**: existing JSON becomes `detail.json`
   (schema bump), add summary writer, externalize thresholds; promote R00
   anomaly rules to explicit checks in QA01.
   ✅ DONE: QC00 retrofit + `gcp_limits.yml` + artefact migration (3071a81);
   QC02 retrofit (1f717a0); QA00/QA02 contract outputs + scoped subfolders
   (91c6af8); QA01 anomaly rules landed as checks in the Phase 2 port.
   Includes the QC02
   panel-homogeneity wire-in (`QC02_HomogeneityCheck_PLAN.md` step 4 —
   doc retired 2026-09-01, in git history):
   `homogeneity` block + `median_residual_pct` enter as contract checks
   (advisory — `suspect → warning`, excluded from `worst()` while run
   status stays `not_evaluated`); its steps 1–3 (threshold calibration,
   helper migration, `panel_homogeneity()` + tests) are independent and
   may proceed before any phase.
   ✅ DONE (steps 1–4): thresholds calibrated over all 119 store spectra
   tables (448 panel instances; clean reference = CaliWeek per EM
   region — VNIR 0/160 false suspects, catches Narrabri 20260805 VAL-82
   and TomsCoverCrop 20260723) → per-region `homogeneity` block in
   `spectral_limits.yml`; `group_value_stats` family moved to
   `core_functions/group_stats.py` (re-exported from `plot_extracts`);
   `spectral_qc.panel_homogeneity()` + tests; QC02 v3.1 emits the
   per-panel `homogeneity` block, `median_residual_pct` and advisory
   `homogeneity_*` checks.
   **DT01 fold-in** (reference implementation:
   APEx_Analysis `code/00.DataProcessing/DT01_GryfnPanelComparison.py`):
   - → **QC02**: the per-run observed-vs-expected DHR comparison
     (comparison table schema, per-panel bias/RMSE/MAE delta stats,
     overlay + delta figures, `bad_band` column convention, gpro set
     pinning per §5b).
     ✅ DONE (be4c0ec): `spectral_qc/panel_library.py` resolver +
     advisory `panel_set_pinned` / `dhr_bias_*` checks vs
     `spectral_limits.yml`; §5 bad-band updates applied (1900 nm →
     1990, first VNIR candidates). Panel DHR library built at
     `reference/panels/` (9c84407, 16 sets, integrity tests).
   - → **QA02**: the cross-run aggregation — combined
     `all_runs_comparison` / `all_runs_delta_stats` tables, per-run delta
     curves and observed-vs-expected overlays per EM region (VNIR/SWIR as
     separate figures, matplotlib lines so masked bands render as gaps),
     and a new **within-day panel-bias drift check** (Narrabri 20260805:
     panel 82 walked +9 → −13 % monotonically over runs 01→09,
     brightness-dependent) correlated against QC01's solar geometry.
     ✅ DONE (d39a2cf): `spectral_qc.within_day_drift()` + advisory
     `dhr_within_day_drift` check vs the `spectral_limits.yml`
     `within_day_drift` block; validated on GOBI/20260805 (reproduces
     the panel-82 walk: 24.1 pp range, run-order rho −1.00).
   **QC03_RasterCheck** is built here too — net-new on the `qc_report`
   contract from day one (no legacy schema to retrofit); QA03 stays
   reserved until QC03 has accumulated background rates.
   ✅ DONE (42de99b): advisory, `raster_validity.yml` uncalibrated
   defaults, per-band detail stats ready for QA03.
4. **Phase 4 — README + docs**: run-order/dependency table, per-script
   prerequisites, contract spec; sync master → APEX-data and
   APPN_GenericFileStorage copies (note: APEX-data copy currently missing
   `QC02_HomogeneityCheck_PLAN.md`).
   ✅ README done (this repo): pipeline-level rewrite — run-order/
   dependency table, contract + output locations, `reference/` files,
   per-script prerequisites; stale QC02 facts fixed (parquet default,
   nm-range bad bands, DHR/homogeneity in scope).
   ✅ APPN_GenericFileStorage sync done (2026-09-01, generic commit
   dbba284, pushed): rename sequence applied with `git mv`, all seven
   scripts + README + plan docs + tests, `qc_report`/`issue_yaml`
   packages, spectral_qc/core_functions/plot_extracts updates
   (`resolve_qareports_dir` rename included), `reference/thresholds` +
   `reference/panels` with real curves (§5d revision). 259 tests pass;
   all seven CLIs smoke-tested.
   ⏳ DEFERRED — the APEX-data copy sync waits until the pipeline has
   been tested more thoroughly on real data (operator decision,
   2026-08-25).
5. **Phase 5 — downstream consumers (§8)**: PS00 pipeline-registry update
   (dual rules, then legacy retirement), the `reference/` folder creation +
   `sensor_platform_paths/` → `reference/sensor_pipelines/` move (§5d,
   single commit with the PS00 loader-path change; both hosts pull before
   their next scan), and the DataSync dashboard
   stage-matrix review. Registry edits land in the same commit as the
   Phase 2 renames where possible — a renamed report with an un-updated
   registry reads as `qc_report` regressing to *missing* on every run.
   ✅ DONE (v1.11): registry move + loader paths + `any_of`/list
   `json_equals` engine support + CALVIS/GOBI dual `qc_report` rules;
   validated on 2026_APEx (contract QC00 details grade done/failed_check
   correctly).
   ✅ Dashboard stage-matrix review done (2026-08-25, no code changes):
   see §8.
   ✅ v1.12: all four contract reports joined `qc_report` (QC01
   verdict-gated, QC02/QC03 presence-tracked — §8).
   ⏳ REMAINING — legacy QC00 rule retirement after store migration;
   QC02/QC03 rules tighten to verdict-gating once they grade real
   verdicts.

Development happens in this repo (APPN-42 master) first; copies sync after.

### Revision pass (queued 2026-08-26, operator) — before the CaliWeek QC03 batch

The store re-QC surfaced output-design problems worth fixing before the
remaining heavy compute (CaliWeek QC03, 39 runs ≈ 7 h) bakes them into
another 39 report sets:

1. **QC02 figures + reporting redesign** — current layout/content not
   fit for purpose (operator review of the re-QC outputs). Concrete
   requirements (operator, 2026-08-26):
   ✅ DONE (QC02 v3.2, 2026-08-26) — all three sub-items implemented
   and validated on the test bed below (50 tests pass):
   1. **0 = NaN.** In this data a 0 value is nodata, not a real
      reflectance/radiance. QC02 must mask zeros before any panel
      statistics (means, homogeneity skew/kurtosis, DHR comparison) and
      report the zero/nodata fraction per panel extraction (summary +
      detail JSON) so masked-out pixels are visible, not silent.
      Includes the degenerate case: **all values over a panel NaN** (in
      some/all bands) — stats and DHR comparison for those bands must
      come out `not_evaluated` with the nodata fraction reported, not
      NaN-propagate or crash.
      *Implemented:* `spectral_qc.zero_nodata_mask()` shared helper;
      per-panel `nodata_zero_fraction` + `all_nodata` in the stats
      blocks; new advisory `nodata_zero_<target>_<region>` contract
      checks (warn > 5 % or any all-nodata panel; collapsed to
      per-region in v3.3 — item 4); zeros masked from
      residuals, homogeneity, DHR percentiles and all figures; the
      radiance range check ignores the sentinel so all-nodata tables
      still report.
      **Suite audit (2026-08-26):** QA02 had the same hole —
      `prepare_comparison_frame` now drops sentinel rows with a counted
      warning (v1.5; CongWhiteHeads smoke test dropped 11.8 %). Its DHR
      aggregation consumes QC02's already-masked parquets (stale
      pre-fix parquets refresh on QC02 re-run). QC03 treats zeros
      explicitly (`zeros_in_footprint` — audit item 2 below);
      QC00/QC01/QA00/QA01 carry no spectral values — clean.
      **QA02 bad-band render bug (2026-08-26):** the seaborn comparison
      figures NaN-"masked" bad bands, but seaborn drops NaN rows and
      bridges the line straight across — the mask was invisible. Fixed
      per the APEx_SensorCalibration `zero_bad_bands` convention: bad
      bands are forced to a hard 0 so the exclusion renders as an
      unmissable dip. QC02's matplotlib figures gap NaN correctly and
      are unchanged.
   2. **Figure design aligned with QA02** — bring the QC02 per-run
      figures in line with `QA02_SpectralComparison.py`'s layout/style
      so per-run and cross-run spectral outputs read as one family.
      *Implemented:* the `*_spectra.png` figure type is **retired**
      (operator, 2026-08-26 — it duplicated the DHR overlay; existing
      copies are deleted on first touch and dropped from the artifact
      list). The DHR overlay/delta figures carry the QA02 conventions
      (bold rcParams, dashed grid, frameless legends,
      `Sensor:/Target:/EM range:` suptitles, tight bbox, symlog delta
      axis) and **both show the observed p5–p95 percentile envelope**
      (delta = percentiles minus expected), not just a raw line.
      **Bad-band revert (operator, 2026-08-26):** the be4c0ec DT01
      candidate ranges were never approved — VNIR has **no** bad bands
      (the 400–420/920–1010 nm masks caused the apparent sub-450 nm
      gap) and the SWIR 1900 nm band is back to 1790–1960. Later the
      same day the operator confirmed only **two** ranges are approved:
      the CALVIS SWIR water bands **1345–1435** and **1790–1960 nm** —
      the original 890–950 edge and 2440–2600 detector-tail ranges
      (from 9716afd, 2026-08-13) were removed too. Bad-band
      changes require explicit operator sign-off.
   3. **Two ELM panel sets.** Some runs carry two sets of ELM panels;
      QC02 currently assumes one. Extraction, expected-DHR matching,
      homogeneity stats, reporting, and figures must handle per-set
      results (grouped, not pooled — pooling two sets corrupts the
      homogeneity and observed-vs-expected checks).
      *Implemented:* dual-ELM runs resolve every ELM target by
      signature (the gpro pin only identifies the corrected set;
      identical 2024-batch curves resolve via `identical_candidates`,
      differing curves stay a hard error); `panel_set_pinned` carries a
      dual-ELM note + `n_elm_targets`; per-target stats/checks/figures
      were already grouped by `panel_name` (verified, not pooled).
   4. **Summary collapse** (operator, 2026-08-26): the per-target ×
      region check explosion (21 lines on the test bed) made the
      summary YAML unreadable. One line per check family per EM region
      (`nodata_zero_<region>`, `homogeneity_<region>`,
      `dhr_bias_<region>`), worst-wins across targets with offending
      targets/panels named in the value; per-target granularity lives
      in the detail JSON (`spectral_report`, `dhr_comparison`).
      Artifacts list and `schema_version` unchanged (operator
      decisions). ✅ DONE (QC02 v3.3, 2026-08-26).

   Test bed: `USYD_Narrabri/2026_CongWhiteHeads/2026I.A.Watson/CALVIS/20260819`
   — has missing SWIR data over one panel set (exercises 1, incl. the
   all-NaN-panel case) and run_04 has a dual ELM setup (exercises 3).
   Re-run 2026-08-26 after removing the stale QC02 outputs: runs 01–04
   clean (run_00 carries no panel files); Gryfn4P set-2 SWIR confirms
   the case — panels 30/56 all-nodata (warning + `all_nodata` stats),
   panels 11/82 at 87 %/37 % nodata; run_04's two ELM targets both
   resolve `identical_candidates [24008, 24009]`.
2. **QC03 masking audit** — verify the footprint/bad-data logic is not
   conflating *outside the capture area* (all-bands-zero background)
   with *scan-line skips inside the footprint* (zero rows/stripes that
   are genuine data loss). The §5c `zeros_in_footprint` check is only
   meaningful if the footprint definition excludes the former and keeps
   the latter.
   ✅ DONE (QC03 v1.2, 2026-08-26). The conflation was real: an
   all-band dropout was silently counted as background, so only
   *partial*-band zeros ever reached `zeros_in_footprint`. Fixed by the
   §5c zone split (capture polygon → advisory edge band + graded
   `dropout_in_roi`, plus the `data_outside_bbox` guard and the
   connectivity fallback); `footprint`/`zeros_in_footprint` definitions
   left untouched so the accumulated history stays comparable.
   Test bed: `2026_CongWhiteHeads/I.A.Watson/CALVIS/20260819`, all four
   processed runs (`--force`, 19.6 min, 312 GB read). run_01 SWIR
   reproduces the diagnostic anchor to the pixel — 983,778 edge-band
   zeros (16.15 % of a 6.09 Mpx ring), 95,833 ROI dropout px = 0.351 %
   of 27.27 Mpx, 80,042 of them interior-connected (84 %),
   `data_outside_bbox` = 0 — i.e. 95.8 k px of real data loss that the
   old logic reported as background. The **VNIR control** on the same
   flight confirms the classifier is not inventing artefacts: ROI
   dropout 0.054 % and only 11 % interior-connected (ragged swath edge,
   not holes). Runs 02–04 exercise the spacing fallback — QC01 wrote
   `flight_lines.csv` with an all-NaN `line_spacing_m` there, so the
   inset drops to the short-axis rule (3.62 m vs 5.45 m) and the
   `line_spacing_source` provenance field says so. Their zone geometry
   is byte-identical to run_01's (same acquisition, different
   processing — the §5e duplicate-run case); only `zeros_in_footprint`
   moves. `data_outside_bbox` is 0 on every SWIR product and 1,727 px
   (0.001 % of grid) on every VNIR one — pixel-centre rounding at the
   finer GSD, three orders under the 0.01 % warn.
   Open: `dropout_in_roi_pct` thresholds are still uncalibrated (warn
   0.1 %, no fail) and whether the graded metric should become the
   interior-connected composite is undecided — both need more runs, so
   they ride with the QA03 calibration. Regression cover:
   `tests/test_qc03_zero_zones.py` (13 tests).
3. **Cross-script markdown QC report (`QC_report.md`)** — one per-run
   human-readable report that all four QC scripts contribute their
   section to (à la QA00's `QC_GCP_run_comparison.md`), written to
   `QC_data/QC_report.md`. Must respect §9: no orchestrator — each
   script independently (re)renders its own section from the contract
   detail JSONs on its own run; the report is an artifact, not a
   runner.
4. **QA markdown reports with figure embeds** — the QA scripts'
   summary output gains a human-readable `.md` report per invocation
   (alongside the contract `_summary.yaml`, which stays — PS00 and the
   §2 contract consume the YAML/JSON, the markdown is for humans).
   Rendered from the same detail JSON, with the script's figures
   embedded as relative-path images so the report reads standalone in
   a preview next to its `QAReports/` artefacts. Scope-aware filenames
   per §4 so crawls at different scopes never clobber.
5. **QC03 dead-band detection** (operator, 2026-08-27) — a band
   that is ~100 % zeros but is *not* an approved bad band (CALVIS SWIR
   1345–1435 / 1790–1960 nm are the only approved ranges) is a major
   failure state: an entire wavelength silently missing from the
   product. The threshold cannot be exactly 100 % — a real flight has
   been found with 99.9 % zeros on multiple bands (operator,
   2026-08-27) — so treat ≥ ~99 % per-band zeros as dead (exact cutoff
   to be set at implementation). The next QC03 update must detect
   per-band dead coverage, report which bands (nm) are affected and
   their zero fractions, and grade it as a fail-class check — not fold
   it into the pixel-level `zeros_in_footprint`/`dropout_in_roi`
   metrics, which can't see a single dead band among otherwise-valid
   pixels.
6. **QC03 per-wavelength values: ROI vs bbox** (operator, 2026-08-27) —
   the per-band metrics (zero fractions, per-band stats in the detail
   JSON) are currently computed over the footprint/whole-bbox domain,
   so the §5c zone split's insight is lost at band level: expected
   edge-band incompleteness pollutes every per-wavelength figure. The
   next QC03 update should report per-band values for the ROI and the
   bbox (or edge band) separately, so band-level grading — including
   item 5's dead-band check — evaluates against the ROI, not the
   bbox.

Re-QC state at pause: QC00/QC01/QC02 complete node-wide (2026-08-25);
QC03 done for 2026_APEx (6/6 pass) + CongWhiteHeads in flight; CaliWeek
QC03 held. Triage queue from the sweep: QC00 fails (APEx run_05,
CaliWeek run_11 29 cm, SIFPhototoxicity ×3, TomsCoverCrop ×2), QC01
fails (CaliWeek 20260415 run_00/run_06, MenindeeLakes GOBI 20260311,
TomsCoverCrop ×2), CongWhiteHeads VAL_Gryfn_4 −56 % residual (evidence
for the open run_01 ELM/exposure ticket).

## 8. Downstream consumers — PS00 + DataSync dashboard

The QC reports are load-bearing beyond DS02: PS00 (processing-status
collector, `Code/DS01_StorageReporting/PS00_ProcessingStatus.py`) grades the
`qc_report` pipeline stage off report filenames and their embedded verdicts,
and the DataSync dashboard renders what PS00 collects. Both must track the
contract migration.

**PS00 pipeline registry** (`reference/sensor_pipelines/CALVIS.json`,
`GOBI.json`; moved from `sensor_platform_paths/` in the v1.11 package, §5d):

- ✅ Landed (v1.11, extended v1.12): `qc_report` `done_when` is an
  AND-list of all four contract reports — QC00 dual-rule via `any_of`
  (legacy loose glob OR contract detail, `json_equals: [pass, warn]`),
  QC01 (`[pass, warn]` — real verdicts from the spec checks), and
  QC02/QC03 presence-tracked (`[pass, warn, not_evaluated]`, only
  `fail` blocks) gated by payload `requires`
  (`[[elm_panels, val_panels], vnir|swir]` / `vnir|swir`).
  Presence-tracking rationale (v1.12): the dashboard is the migration
  to-do list — runs grade `missing` until the new QC has been run on
  them, without pretending advisory scripts have verdicts.
- **`warn` counts as done** for stage grading (warn-level checks are
  advisory by §3); only `fail` → `failed_check`, consistent with PS00's
  "exists-but-failing is failed_check, never done" rule. PS00's
  `json_check` accepts list-valued `json_equals` (the one PS00 code
  change in scope — ✅ landed in the v1.11 package).
- **Transition = dual rules**: already-scanned runs keep legacy report
  names; the registry carries both the legacy and contract rule
  (any-match) until the store is migrated, then the legacy rule retires.
  ✅ Dual rules live; retirement pending store migration.
- The registry's "spectral report WIP, added when it lands" note resolved
  in v1.12: all four contract detail JSONs are in the `qc_report`
  `done_when` set — QC01 verdict-gated, QC02/QC03 presence-tracked until
  they grade real verdicts (then their rules tighten from
  `[pass, warn, not_evaluated]` to `[pass, warn]`; QC03's tightening
  waits for its §5c gate calibration).
- `qc_testing` artefact globs (panel polygons, spectra tables) are
  unchanged — those name QC *inputs*, not reports.

**DataSync dashboard** (`DataSync/Code/DS04_Dashboard/DB00_Dashboard.py`,
separate repo + deploy: commit/push → mint `git pull` → restart
`appn-dashboard`):

- The `/processing` page and `/processing/stages` run × stage matrix read
  PS00's `Node_Status*.parquet` — no QC filename coupling, so most changes
  flow through the registry automatically.
- ✅ Review pass done (2026-08-25, no code changes needed): the two
  hardcoded canonical stage-order lists match the live registry names
  (`download…raw_cleanup`, unknown stages append by contract); colour
  maps key on PS00's status vocabulary with a neutral `None` fallback;
  `detected_stage` renders as free text (old script names in existing
  tickets still display; blank when undocumented).
- Nice-to-have (not blocking): surface the pass/warn/fail split on the
  stage matrix once contract statuses are in the parquet, instead of the
  current done/failed_check binary. Deferred with the `done_when`
  expansion — today's contract scripts other than QC00 grade
  `not_evaluated`, so there is nothing to split yet.

## 9. Out of scope (explicitly rejected during workshop)

- **QC99 orchestrator / run roll-up script** — will not be built.
- **QC bundle-integrity gate as its own script** — folded into QC01.
- **Watcher daemon / pipeline hooks** — scripts are invoked manually.
- **Wiki-hosted reference data with download-on-first-use** (panel DHR
  JSONs, sensor registry) — rejected for §5d: offline QC, unattended PS00
  scans, no unreviewed source may change QC verdicts, provenance hashes
  must point at repo files. Wiki carries documentation exports only.
