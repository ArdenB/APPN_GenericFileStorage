# QC02 panel-homogeneity check — workplan (paused 2026-08-19)

**Status:** ✅ steps 1–4 DONE (2026-08-25, pipeline Phase 3): thresholds
calibrated over all 119 store spectra tables (per-EM-region `homogeneity`
block in `reference/thresholds/spectral_limits.yml`, provenance in the
YAML comments); `group_value_stats` family moved to
`core_functions/group_stats.py`; `spectral_qc.panel_homogeneity()` +
tests; QC02 v3.1 emits the per-panel `homogeneity` block,
`median_residual_pct` and advisory `homogeneity_*` contract checks.
Step 5 (port to the copies) folds into pipeline Phase 4.

**Integrated with [QC_PIPELINE_PLAN.md](QC_PIPELINE_PLAN.md) (2026-08-24):**
the target script renames to `QC02_SpectralCheck.py` in pipeline
Phase 2 (this doc renames to match, → QC02). Steps 1–3 below are
independent and can proceed now; step 4 folds into pipeline Phase 3 and
step 5 into Phase 4. Superseded details are struck through below.

## Problem

`QC02_SpectralCheck._target_region_stats` reduces each panel × band to
`mean_refl` before computing residuals against the nominal `Panel_ref`
value. Shadow across a panel corner, mixed edge pixels (georeferencing
error), specular hotspots, or objects on the panel bias that mean
*silently* — the residual moves but nothing says why. `Valid_Range`
fraction only catches out-of-gamut pixels, not plausible-but-mixed ones.

## Signal

A clean panel's per-band pixel distribution is tight, symmetric, unimodal.
The DS03 shared statistic set (added to
`Code/functions/plot_extracts.group_value_stats` on 2026-08-19) detects the
failure modes cheaply:

| Failure mode | Signature |
|---|---|
| shadow / mixed edge pixels | bimodal → `l_kurt` depressed, abs(`skew`) inflated |
| specular hotspot | heavy right tail → `skew`, `kurtosis` up |
| general contamination | per-band `mean` vs `median` divergence |

Validated context (see `/memories/repo/ds03-plot-extraction.md` and the
2026-08-19 session): L-moment ratios are ~0.2 ms/group and a synthetic
50/50 bimodal mixture gives `l_kurt = -0.185` vs `+0.124` unimodal.
Named-distribution fitting (distfit/fitter/scipy MLE) was benchmarked and
**rejected** — winners are noise between beta/weibull/gamma and unimodal
fits cannot flag bimodality anyway.

## Design decisions already made

- Homogeneity is **advisory only**: a `homogeneity` block per `Panel_ref`
  in the report JSON + a note in the summary row. The run `status` logic
  stays `not_evaluated` pending the ET00/ET03 reference set
  (APEx_SensorCalibration) — do not entangle the two.
- **Residuals compute against the manufacturer DHR curve, not the nominal
  `Panel_ref` scalar** (added 2026-08-24 from DT01): a "20 %" panel is not
  flat 20 % — real curves vary by several pp across the range and between
  physical sets (24005 vs 25005 differ ~3 pp in SWIR despite identical
  nominals). The DHR lookup uses the panel reference library + gpro set
  pinning (pipeline §5b) and lands in the same Phase 3 wire-in as
  `median_residual_pct`. Homogeneity statistics themselves are unaffected
  (per-panel distribution shape, reference-free).
- Report **both** `mean_residual_pct` and `median_residual_pct` always
  (divergence is itself the contamination signal); no self-healing switch.
- ~~Schema version bumps 2.2 → 2.3 (`QAConfig.schema_version`).~~
  **Superseded:** the wire-in lands via pipeline Phase 3, where the
  existing JSON becomes `detail.json` under the dual-file contract with
  its own paired `schema_version` — no standalone 2.3 bump.
- The `clean`/`suspect` flag maps to the pipeline's shared check-level
  enum (§3): `suspect → warning`, advisory (excluded from `worst()` while
  run status stays `not_evaluated`).
- New stats code lives in `Code/functions/spectral_qc/` (DS02 statistics
  home), consuming the shared group-stats helper.

## Task list (in order)

1. **Threshold calibration (do first — no thresholds may be invented).**
   Throwaway script over the existing extracted spectra tables in
   `QC_data/` across all runs (CALVIS_RUN_xx, APEx days, TomsCoverCrop
   etc. on the node store): compute per-panel × band `skew`, `l_kurt`,
   `|mean−median|` distributions for presumed-clean panels; eyeball any
   known-contaminated cases. Pick thresholds at ~p95 of the clean
   population with margin. ~~Record values + provenance in the `QAConfig`
   docstring (same pattern as `radiance_int_max`).~~ **Superseded:**
   record values + provenance in the threshold config YAML (pipeline §5,
   `flightcal_spec.yml` pattern) so they aren't migrated twice.
2. **Helper migration.** Move `group_value_stats`, `_value_stats`,
   `_lmoment_ratios`, `group_value_percentiles` from
   `Code/functions/plot_extracts` to `core_functions` (they stop being
   DS03-only). Update the three DS03 call sites (`pex.` → `cf.`), or keep
   a thin re-export in `plot_extracts` if churn is unwanted.
3. **`spectral_qc.panel_homogeneity(df) -> dict`** + unit tests
   (`spectral_qc/tests/`). Per `Panel_ref`, over good bands only (reuse
   `bad_wavelength_mask`; note DT01 mask updates — pipeline §5: 1900 nm
   water band widens to ~1990 nm, VNIR gains 400–420 nm and >~920 nm
   candidates): run `group_value_stats(..., ["band"],
   value_col="refl_pct")`, summarise to `median_abs_skew`,
   `median_l_kurt`, `mean_median_divergence_pct`, `n_bands_flagged`,
   `fraction_bands_flagged`, `flag` (`clean`/`suspect`).
4. **Wire-in — folded into pipeline Phase 3.** `_target_region_stats`
   (by then in `QC02_SpectralCheck.py`) adds the `homogeneity` block
   per panel and `median_residual_pct` beside `mean_residual_pct`, as
   contract checks in `detail.json` + one summary-YAML line per suspect
   panel; thresholds come from the config YAML (step 1). Optional:
   annotate the per-target spectra figure title with the flag (no new
   figure).
5. **Port — folded into pipeline Phase 4** (master → APEX-data +
   APPN_GenericFileStorage sync; the APEX-data copy is currently missing
   this plan doc). Note the DS03 stats work was already ported 2026-08-19.

## Cost sanity

Panels are hundreds of pixels × ~170 bands × a few panels per run —
homogeneity adds milliseconds per run.
