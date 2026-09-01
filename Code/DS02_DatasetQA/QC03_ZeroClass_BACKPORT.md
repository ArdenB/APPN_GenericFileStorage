# QC03 backport handover — zero-pixel classification (bbox + sub-box zones)

> **STATUS: backported 2026-08-26 (QC03 v1.2, plan v1.20).** Everything
> below is implemented in `QC03_RasterCheck.py` (`load_capture_extent`,
> `extent_verts_px`, `inside_mask_px`, `zone_inset`, `qc01_line_spacing`,
> `_interior_zero_mask`, `classify_zero_zones`, `_add_zone_checks`),
> specified in plan §5c, thresholded in
> `reference/thresholds/raster_validity.yml` (`dropout_in_roi_pct`,
> `data_outside_bbox_pct`, `zero_zones`) and pinned by
> `tests/test_qc03_zero_zones.py`. The regression anchor below reproduces
> to the pixel on the test bed. This file is kept only as the record of
> where the decisions came from; the live spec is the plan.

**Source:** `QC03_zero_class_diagnostic.ipynb` (deleted after this handover;
it was gitignored and is NOT in git history — this file and the validated
code below are the complete record). Diagnostic run 2026-08-26 on
`USYD_Narrabri/2026_CongWhiteHeads/2026I.A.Watson/CALVIS/20260819/run_01`
(SWIR, `20260819_VegetativeWhiteHeads_MR04.gpro`, 10481×9886 px @ 2.5 cm GSD).
All decisions below are operator-signed-off from that session.

## Problem

`QC03_RasterCheck.scan_product` classifies any all-bands-zero pixel as
background (`background = (data == 0).all(axis=0)`; footprint = the rest).
A scan-line dropout that zeros *all* bands is silently absorbed into
"background" — only partial-band zeros reach `zeros_in_footprint`. Run_01
SWIR contains real all-band dropouts (rectangular data holes + scan-line
streaks) that QC03 currently cannot see.

## The solve — bbox is the analysis domain (two reported zones)

The gpro ships the flown-area capture polygon:
`<gpro>/extents/hyper_extent.geojson` (FeatureCollection, first feature,
Polygon, CRS84 lon/lat, closed ring). **Everything outside the bbox is
discarded** (operator decision 2026-08-26: no value in analysing it — it is
background by construction; its only use is the extent-mismatch sanity
guard below). All-zero pixels inside the bbox split into:

1. **bbox − subbox ("edge band")** → `zero_edge_band` — *expected*
   incomplete capture, reported separately (advisory, never graded)
2. **inside subbox (ROI)** → `dropout_roi` — the graded artefact metric

Where **subbox = bbox eroded inward by
`0.5 × max(0.10 × short_axis, line_spacing)`** per edge.

- Fieldbook basis: CALViS/GOBI fieldbooks preflight §1 (survey polygon =
  AOI + panels/GCPs + 5 m buffer); GOBI fieldbooks step-2 note (capture
  polygon buffered perpendicular to flight lines by ≥1 line spacing);
  `Standard-Flight.md` "effective capture area ~10% smaller (per-edge
  ~10% of short axis)". GOBI QA appendix explicitly calls edge-missing
  pixels a known failure mode → edge band is advisory, not a defect.
- The 0.5 factor is an **operator decision** (2026-08-26 trial): the full
  fieldbook margin (10.9 m on run_01) barely changed `zero_edge_band`
  (1.007M→0.984M px) but absorbed real artefact into the advisory band;
  at 5.4 m the ROI keeps 95,833 px of which 80,042 are interior-connected.
- `line_spacing_m`: median of QC01's `flight_lines.csv` for the run.
  **Fallback when QC01 hasn't run:** drop the line-spacing term, use
  `0.5 × 0.10 × short_axis` alone. (As implemented the fallback also
  covers QC01 having run but written an all-NaN `line_spacing_m` — that
  is the case on 20260819 runs 02-04 — and the taken branch is recorded
  in the `line_spacing_source` provenance field.)
- `short_axis`: mean of the two shortest edges of the polygon (rotated
  bbox); fine for N-vertex variants too.

## Validated implementation (from the notebook)

Point-in-convex-polygon by half-planes — pure numpy, no rasterization,
~0.6 s for a 103 Mpx grid; the inset reuses the same test:

```python
def inside_mask_px(verts, hh, ww, inset=0.0):
    """verts: polygon vertices in PIXEL space (closed ring dropped).
    inset: inward offset in px. Winding-agnostic, N-vertex convex."""
    x, y = verts[:, 0], verts[:, 1]
    sgn = np.sign(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))
    px = np.arange(ww, dtype=np.float32) + 0.5   # pixel centres
    py = np.arange(hh, dtype=np.float32) + 0.5
    inside = np.ones((hh, ww), dtype=bool)
    for (x0, y0), (x1, y1) in zip(verts, np.roll(verts, -1, axis=0)):
        el = np.hypot(x1 - x0, y1 - y0)
        inside &= (sgn * ((x1 - x0) * (py[:, None] - y0)
                          - (y1 - y0) * (px[None, :] - x0)) / el) >= inset
    return inside
```

Vertex prep: `pyproj.Transformer.from_crs("OGC:CRS84", src.crs,
always_xy=True)` on the ring (drop the closing vertex), then
`(~src.transform) * (ex, ey)` to pixel coords. (Backported with
`rasterio.warp.transform(CRS.from_epsg(4326), src.crs, lons, lats)`
instead — identical result, explicit lon/lat axis order, no second PROJ
binding to keep in step with GDAL's. The as-shipped
`inside_mask_px` also evaluates the grid in row blocks: same arithmetic,
bounded peak memory on the 162 Mpx VNIR grids.)

Gotchas proven in the diagnostic:

- **N-vertex polygons exist**: 6 Menindee gpros carry 6–9 vertex convex
  rings. Loop over edges — never hard-code 4.
- **numpy 2.x**: 2-D `np.cross` is gone; use the manual z-component
  (`a[:,0]*b[:,1] - a[:,1]*b[:,0]`) for the convexity/winding checks.
- **Boundary rounding**: nonzero pixels can sit ≤0.4 px outside the
  polygon (3,475 px on run_01, max 0.39 px — pixel-centre vs edge
  rounding). Feeds the `data_outside_bbox` sanity guard: count nonzero
  pixels outside the bbox with a 0.5 px tolerance (`inset=-0.5`); a
  large count means the extent doesn't match the raster (wrong/stale
  file), not real out-of-bbox data.
- The all-zero test needs per-pixel zero counts over **all** bands —
  QC03 v1.1 (2026-08-26 perf refactor) already computes this: the
  band-major fast path (`_scan_bands_raw`) accumulates per-thread
  `zero_count` partials; merge them and the mask is
  `zero_count == n_bands`. Return the merged full-grid array from the
  accumulator dict — do NOT crop it to the bbox's pixel rectangle
  (`data_outside_bbox` needs nonzero knowledge *outside* the bbox, and
  at ~200-330 MB the array is irrelevant next to the 27-52 GB read).
  The GDAL fallback path (`_scan_bands_gdal`) needs the same
  accumulator added to its chunk loop.

## Extent reliability (whole-store survey, 2026-08-26)

- 90 `.gpro` dirs; 80 with hyperspec content; **72/72 gpros with processed
  hyperspec products have a valid extent** (all Polygon, CRS84, convex).
- The 8 "missing" are: 5× APExCaliWeek 20260416 `RunFailed`
  (design_flaw, no hyperspec products), 1× SIFPhototoxicity `RunFailed`
  (gnss, bundle literally named `*_NoExtent.gpro`), 2× RGB+LiDAR-only by
  intent (LIFE3888, TomsCoverCrop — raw `nHP-*`/`uVS-*` folders but no
  hyperspec intended). All are accounted for in their `*_Issues.yaml`.
- **Rule:** when a `*_{VNIR|SWIR}_Orthomosaic.bin` exists, the extent can
  be treated as required — absence is itself a warning-grade integrity
  finding. **Fallback discriminator** when absent: border connectivity
  (`scipy.ndimage.label` on the all-zero mask; border-connected components
  = out-of-capture, interior components = dropout). On run_01 the two
  methods agree on 100% of ROI dropout at the 10.9 m inset, 84% at 5.4 m
  (the remainder is ragged swath edge inside the tighter ROI).

## Metrics to add to QC03 (per product)

- `footprint` definition **unchanged** (non-all-zero pixels) so history
  stays comparable; the new checks use bbox/subbox denominators. No
  metric for the outside-bbox area — it is not reported as a zone.
- `zero_edge_band_pct` — all-zero share of the edge band (advisory
  check, `not_evaluated`/info; run_01: 16.1% of the band)
- `dropout_in_roi_pct` — all-zero share of the subbox (graded check;
  thresholds TBD via `reference/thresholds/` spec — run_01 SWIR: 0.35%
  of ROI; suggest warn ~0.1%, but calibrate on the VNIR control + more
  runs first)
- `data_outside_bbox` — sanity guard, not a zone metric: nonzero px
  outside the bbox at 0.5 px tolerance. Expected ≈0 (run_01: 0 beyond
  tolerance); warning when material — means extent/raster mismatch.
- provenance: inset metres/px, line-spacing source (qc01|10%-rule),
  extent path + n_verts, classifier (bbox|connectivity-fallback)

## Run_01 SWIR reference numbers (regression anchor)

| zone | px | share |
|---|---|---|
| grid | 103,614,566 | — |
| all-zero (old "background") | 71,336,296 | 68.85% |
| outside bbox (discarded; context only) | 70,256,685 | 67.80% |
| nonzero outside bbox at 0.5 px tol (`data_outside_bbox`) | 0 | — |
| `zero_edge_band` (5.4 m inset) | 983,778 | 16.1% of band |
| `dropout_roi` (5.4 m inset) | 95,833 | 0.351% of ROI |
| interior-cc ∩ ROI | 80,042 | 84% of ROI dropout |

## Open items before/while backporting

- [x] VNIR control (`REGION = "VNIR"`, same run) — expect little/no ROI
  dropout; confirms the classifier isn't inventing artefacts.
  **Done 2026-08-26:** ROI dropout 0.054 % (23,154 of 42.6 Mpx) against
  SWIR's 0.351 %, and only 11 % interior-connected vs SWIR's 84 % — the
  VNIR residue is ragged swath edge inside the ROI, not holes. The
  classifier is not inventing artefacts.
- [ ] Threshold calibration for `dropout_in_roi_pct` across more runs.
  Shipped uncalibrated as `warn_above: 0.1, fail_above: null`; rides
  with the QA03_RasterComparison calibration (plan §5c).
- [ ] Decide whether the composite rule (`dropout = all_zero & subbox &
  interior-connected`) should be the graded metric, or keep pure-subbox
  and report the connectivity split as evidence.
  **Shipped as the latter** (pure-subbox graded, `interior_cc_roi_px` /
  `interior_cc_roi_share_pct` reported as evidence in the detail JSON
  and on the check object) so the composite can be adopted later
  without a re-scan. Still undecided.
- [ ] Note: run_01 SWIR also shows near-whole-footprint partial-zero from
  a handful of ~100%-zero bands (see QC03 per-band
  `zeros_in_footprint_pct`) — separate issue, already visible to QC03.
