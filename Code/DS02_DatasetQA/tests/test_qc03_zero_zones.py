"""Regression tests: QC03 all-bands-zero zone split (plan §5c).

Pins the zone classifier backported from the 2026-08-26 zero-pixel
diagnostic: the half-plane point-in-convex-polygon test, the ROI inset
arithmetic (including the run_01 SWIR anchor - 72.375 m short axis and
10.89 m line spacing give a 5.445 m / 217.8 px inset), the
border-connectivity fallback, and the end-to-end split of a synthetic
cube whose dropout pixel count is known exactly. Also asserts the raw
BSQ fast path and the GDAL fallback return the same all-zero mask, since
both now feed the classifier.

Run with:
    pytest Code/DS02_DatasetQA/tests/test_qc03_zero_zones.py -v
"""

import importlib.util
import json
import pathlib
import sys

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.warp import transform as rio_transform

# ---------------------------------------------------------------------------
# Ensure repo root is importable (QC03 imports Code.functions.* at module top)
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ===== synthetic cube geometry (see the `cube` fixture) =====
HEIGHT, WIDTH, BANDS = 200, 300, 4
MARGIN_ROWS, MARGIN_COLS = 20, 30           # out-of-capture frame
HOLE_PX = 5 * 40                            # rectangular all-band data hole
STREAK_PX = 40                              # all-band scan-line dropout
ZONES_SPEC = {"short_axis_fraction": 0.10, "inset_factor": 0.5}


# ==================================================================================
@pytest.fixture(scope="module")
def qc03():
    """Import QC03_RasterCheck by file path (script naming, not a package).

    Returns
    -------
    module
        The loaded QC03_RasterCheck module.
    """
    path = _REPO_ROOT / "Code" / "DS02_DatasetQA" / "QC03_RasterCheck.py"
    spec = importlib.util.spec_from_file_location("qc03", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ==================================================================================
@pytest.fixture(scope="module")
def product(tmp_path_factory):
    """Write a synthetic ENVI BSQ ortho plus its capture polygon.

    The cube is a uniform 5000-reflectance scene with an out-of-capture
    frame (``MARGIN_ROWS`` x ``MARGIN_COLS``), one rectangular all-band
    data hole and one all-band scan-line streak inside the capture area,
    and a patch of partial-band zeros that must stay invisible to the
    zone split. The extent polygon is exactly the capture rectangle.

    Returns
    -------
    dict
        A :func:`find_ortho_products`-shaped entry.
    """
    tmp = tmp_path_factory.mktemp("qc03_zones")
    cube = np.full((BANDS, HEIGHT, WIDTH), 5000, dtype="<u2")
    cube[:, :MARGIN_ROWS, :] = 0
    cube[:, -MARGIN_ROWS:, :] = 0
    cube[:, :, :MARGIN_COLS] = 0
    cube[:, :, -MARGIN_COLS:] = 0
    cube[:, 100:105, 120:160] = 0     # interior data hole  -> HOLE_PX
    cube[:, 60, 100:140] = 0          # interior scan line  -> STREAK_PX
    cube[1, 150:160, 100:110] = 0     # partial-band zeros  -> footprint only
    bin_path = tmp / "SYN_SWIR_Orthomosaic.bin"
    bin_path.write_bytes(cube.tobytes())
    bin_path.with_suffix(".hdr").write_text(
        "ENVI\n"
        f"samples = {WIDTH}\nlines = {HEIGHT}\nbands = {BANDS}\n"
        "header offset = 0\ndata type = 12\ninterleave = bsq\n"
        "byte order = 0\n"
        "map info = {UTM, 1, 1, 500000.0, 6600000.0, 1.0, 1.0, 55, South, "
        "WGS-84, units=Meters}\n"
        "wavelength = {900, 1000, 1100, 1200}\n", encoding="utf-8")

    corners = [(MARGIN_COLS, MARGIN_ROWS), (WIDTH - MARGIN_COLS, MARGIN_ROWS),
               (WIDTH - MARGIN_COLS, HEIGHT - MARGIN_ROWS),
               (MARGIN_COLS, HEIGHT - MARGIN_ROWS)]
    with rasterio.open(bin_path) as src:
        xs, ys = zip(*[src.transform * (col, row) for col, row in corners])
        lon, lat = rio_transform(src.crs, CRS.from_epsg(4326),
                                 list(xs), list(ys))
    ring = [[a, o] for a, o in zip(lon, lat)]
    extent = tmp / "hyper_extent.geojson"
    extent.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {},
                      "geometry": {"type": "Polygon",
                                   "coordinates": [ring + [ring[0]]]}}]}),
        encoding="utf-8")
    return {"bin": bin_path, "hdr": bin_path.with_suffix(".hdr"),
            "region": "SWIR", "label": "swir", "extent": extent}


# ==================================================================================
def test_inside_mask_px_counts_and_inset(qc03):
    """An axis-aligned square masks its own pixels; inset shrinks it."""
    square = np.array([[10.0, 10.0], [90.0, 10.0], [90.0, 90.0],
                       [10.0, 90.0]])
    assert qc03.inside_mask_px(square, 100, 100).sum() == 80 * 80
    assert qc03.inside_mask_px(square, 100, 100, inset=5).sum() == 70 * 70
    # negative inset widens the polygon by half a pixel on every side, so the
    # pixel centre sitting exactly 0.5 px outside each edge is tolerated
    assert qc03.inside_mask_px(square, 100, 100, inset=-0.5).sum() == 82 * 82


# ==================================================================================
def test_inside_mask_px_is_winding_agnostic_and_blocked(qc03):
    """Reversed winding and a small row block give the identical mask."""
    square = np.array([[10.0, 10.0], [90.0, 10.0], [90.0, 90.0],
                       [10.0, 90.0]])
    forward = qc03.inside_mask_px(square, 100, 100, inset=3)
    assert np.array_equal(
        forward, qc03.inside_mask_px(square[::-1], 100, 100, inset=3))
    assert np.array_equal(
        forward,
        qc03.inside_mask_px(square, 100, 100, inset=3, rows_per_block=7))


# ==================================================================================
def test_inside_mask_px_handles_n_vertex_rings(qc03):
    """6-9 vertex convex rings exist: nothing may be hard-coded to 4."""
    angles = np.arange(6) * (2 * np.pi / 6)
    hexagon = np.column_stack([50 + 40 * np.cos(angles),
                               50 + 40 * np.sin(angles)])
    mask = qc03.inside_mask_px(hexagon, 100, 100)
    area = 1.5 * np.sqrt(3.0) * 40 ** 2          # regular-hexagon area
    assert mask.sum() == pytest.approx(area, rel=0.01)
    assert mask.sum() > qc03.inside_mask_px(hexagon, 100, 100, inset=5).sum()


# ==================================================================================
def test_is_convex_rejects_a_dart(qc03):
    """Convexity gate: the half-plane test is only valid on convex rings."""
    assert qc03._is_convex(np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0],
                                     [0.0, 10.0]]))
    assert not qc03._is_convex(np.array([[0.0, 0.0], [10.0, 0.0], [5.0, 5.0],
                                         [10.0, 10.0], [0.0, 10.0]]))


# ==================================================================================
def test_zone_inset_run01_anchor(qc03):
    """run_01 SWIR anchor: 10.89 m line spacing wins over the 10 % rule."""
    inset = qc03.zone_inset(72.37509556248725, 10.89, "qc01", ZONES_SPEC)
    assert inset["swath_margin_m"] == pytest.approx(10.89)
    assert inset["metres"] == pytest.approx(5.445)
    assert inset["metres"] / 0.025 == pytest.approx(217.8)     # 2.5 cm GSD
    assert inset["line_spacing_source"] == "qc01"


# ==================================================================================
def test_zone_inset_falls_back_to_the_short_axis_rule(qc03):
    """Without QC01 the line-spacing term is dropped, not defaulted."""
    inset = qc03.zone_inset(72.375, None, "10%-rule", ZONES_SPEC)
    assert inset["metres"] == pytest.approx(0.5 * 0.10 * 72.375)
    assert inset["line_spacing_m"] is None
    # a wide swath still wins when QC01 has run
    assert qc03.zone_inset(72.375, 3.0, "qc01", ZONES_SPEC)["metres"] == \
        pytest.approx(0.5 * 0.10 * 72.375)


# ==================================================================================
def test_interior_zero_mask_splits_border_from_holes(qc03):
    """Border-connected zeros are out-of-capture; interior ones are dropout."""
    all_zero = np.zeros((50, 50), dtype=bool)
    all_zero[0, :] = True            # border-connected
    all_zero[20:25, 20:30] = True    # interior hole
    interior = qc03._interior_zero_mask(all_zero)
    assert interior.sum() == 50
    assert not interior[0].any()
    assert qc03._interior_zero_mask(np.zeros((10, 10), dtype=bool)).sum() == 0


# ==================================================================================
def test_load_capture_extent_validates_the_ring(qc03, product, tmp_path):
    """Closing vertex dropped; non-Polygon, non-convex and missing all fail."""
    extent = qc03.load_capture_extent(product["extent"])
    assert extent["n_verts"] == 4          # 5-point closed ring in the file
    assert qc03.load_capture_extent(tmp_path / "absent.geojson") is None

    point = tmp_path / "point.geojson"
    point.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Point", "coordinates": [149.0, -30.0]}}]}),
        encoding="utf-8")
    with pytest.warns(UserWarning, match="not a Polygon"):
        assert qc03.load_capture_extent(point) is None

    dart = tmp_path / "dart.geojson"
    ring = [[0, 0], [1, 0], [0.5, 0.5], [1, 1], [0, 1], [0, 0]]
    dart.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Polygon", "coordinates": [ring]}}]}),
        encoding="utf-8")
    with pytest.warns(UserWarning, match="not a convex ring"):
        assert qc03.load_capture_extent(dart) is None


# ==================================================================================
def test_qc01_line_spacing_reads_the_median(qc03, tmp_path):
    """The inset's spacing term comes from QC01, or is dropped when absent."""
    assert qc03.qc01_line_spacing(tmp_path) == (None, "10%-rule")
    table = tmp_path / "T1_proc" / "QC_data" / "QC01_FlightCheck"
    table.mkdir(parents=True)
    (table / "flight_lines.csv").write_text(
        "line,line_spacing_m\n0,10.0\n1,11.0\n2,12.0\n", encoding="utf-8")
    assert qc03.qc01_line_spacing(tmp_path) == (11.0, "qc01")


# ==================================================================================
def test_scan_paths_agree_on_the_all_zero_mask(qc03, product):
    """Both scan paths feed the classifier, so both must build one mask."""
    raw = qc03._scan_bands_raw(product["bin"], BANDS, HEIGHT, WIDTH, 0,
                               10000, 3, False)
    gdal = qc03._scan_bands_gdal(product["bin"], 10000, 1, False, False,
                                 False)
    assert raw["footprint_px"] == gdal["footprint_px"]
    assert np.array_equal(raw["all_zero"], gdal["all_zero"])
    assert raw["all_zero"].shape == (HEIGHT, WIDTH)


# ==================================================================================
def test_zone_split_finds_the_planted_dropout(qc03, product):
    """The graded ROI holds exactly the planted all-band dropout pixels."""
    scan = qc03.scan_product(product, "CALVIS", 10000, zones_spec=ZONES_SPEC)
    zones = scan["zero_zones"]
    frame_px = HEIGHT * WIDTH - ((HEIGHT - 2 * MARGIN_ROWS)
                                 * (WIDTH - 2 * MARGIN_COLS))

    assert zones["classifier"] == "bbox"
    assert zones["grid_px"] == HEIGHT * WIDTH
    assert zones["all_zero_px"] == frame_px + HOLE_PX + STREAK_PX
    assert zones["outside_bbox_px"] == frame_px
    assert zones["bbox_px"] + zones["outside_bbox_px"] == zones["grid_px"]
    assert zones["edge_band_px"] == zones["bbox_px"] - zones["roi_px"]
    # the capture area is fully imaged here, so the edge band is clean
    assert zones["zero_edge_band_px"] == 0
    assert zones["dropout_roi_px"] == HOLE_PX + STREAK_PX
    assert zones["interior_cc_roi_px"] == HOLE_PX + STREAK_PX
    assert zones["dropout_in_roi_pct"] == pytest.approx(
        100.0 * (HOLE_PX + STREAK_PX) / zones["roi_px"])
    assert zones["data_outside_bbox_px"] == 0
    # partial-band zeros stay in the untouched footprint metric only
    assert scan["footprint_px"] == zones["grid_px"] - zones["all_zero_px"]
    assert scan["cube"]["zeros_in_footprint_pct"] > 0


# ==================================================================================
def test_connectivity_fallback_without_an_extent(qc03, product):
    """No usable polygon: border connectivity stands in and the run warns."""
    zones = qc03.scan_product({**product, "extent": product["bin"]
                               .with_name("absent.geojson")},
                              "CALVIS", 10000,
                              zones_spec=ZONES_SPEC)["zero_zones"]
    assert zones["classifier"] == "connectivity-fallback"
    assert zones["dropout_roi_px"] == HOLE_PX + STREAK_PX
    assert zones["zero_edge_band_pct"] is None
    assert zones["data_outside_bbox_px"] is None


# ==================================================================================
def test_zone_checks_grade_roi_only(qc03, product):
    """The edge band is advisory; only ROI dropout moves the run status."""
    import Code.functions.qc_report as qr

    scan = qc03.scan_product(product, "CALVIS", 10000, zones_spec=ZONES_SPEC)
    spec = {"dropout_in_roi_pct": {"warn_above": 0.1},
            "data_outside_bbox_pct": {"warn_above": 0.01}}
    report = qr.new_report("QC03_RasterCheck", "test", run={
        "node": "n", "project": "p", "site": "s", "sensor": "CALVIS",
        "run": 1, "date": None})
    qc03._add_zone_checks(report, "swir", scan["zero_zones"], spec)
    checks = report["checks"]

    assert checks["capture_extent_swir"]["status"] == "good"
    assert checks["zero_edge_band_swir"]["advisory"] is True
    assert checks["dropout_in_roi_swir"]["status"] == "warning"
    assert checks["data_outside_bbox_swir"]["status"] == "good"
    assert qr.derive_status(checks) == "warn"
    # a spec with no ceilings grades nothing, and the advisory check still
    # cannot drag the run status down
    loose = qr.new_report("QC03_RasterCheck", "test", run=report["run"])
    qc03._add_zone_checks(loose, "swir", scan["zero_zones"], {})
    assert qr.derive_status(loose["checks"]) == "pass"


# ==================================================================================
def test_unreadable_raster_yields_failed_scan_not_crash(qc03, tmp_path):
    """A missing .hdr fails header_bin_integrity; the crawl continues."""
    import Code.functions.qc_report as qr

    bin_path = tmp_path / "SYN_SWIR_Orthomosaic.bin"
    bin_path.write_bytes(b"\x00" * 64)
    prod = {"bin": bin_path, "hdr": bin_path.with_suffix(".hdr"),
            "region": "SWIR", "label": "swir",
            "extent": tmp_path / "hyper_extent.geojson"}
    with pytest.warns(UserWarning, match="raster unreadable"):
        scan = qc03.scan_product(prod, "CALVIS", 10000)

    assert scan["cube"] is None
    assert scan["header_bin_integrity"]["ok"] is False
    assert "no .hdr sidecar" in scan["header_bin_integrity"]["issues"]

    report = qr.new_report("QC03_RasterCheck", "test", run={
        "node": "n", "project": "p", "site": "s", "sensor": "CALVIS",
        "run": 1, "date": None})
    qc03.add_product_checks(report, "swir", scan, {})
    assert report["checks"]["header_bin_integrity_swir"]["status"] == "fail"
    assert report["checks"]["zeros_in_footprint_swir"]["status"] == \
        "not_checked"
    assert qr.derive_status(report["checks"]) == "fail"


# ==================================================================================
def test_multi_gpro_labels_stay_unique(qc03, product, tmp_path):
    """Two bundles with same-region orthos must not overwrite each other."""
    run_dir = tmp_path / "run_99"
    for name in ("a.gpro", "b.gpro"):
        products_dir = run_dir / "T1_proc" / name / "products"
        products_dir.mkdir(parents=True)
        for f in (product["bin"], product["hdr"]):
            (products_dir / f.name).write_bytes(f.read_bytes())
    found = qc03.find_ortho_products(run_dir)
    labels = [p["label"] for p in found]
    assert labels == ["swir_gpro1", "swir_gpro2"]
    assert [p["gpro"] for p in found] == ["a.gpro", "b.gpro"]
    # single-bundle runs keep the plain labels
    single = tmp_path / "run_98" / "T1_proc" / "only.gpro" / "products"
    single.mkdir(parents=True)
    (single / product["bin"].name).write_bytes(product["bin"].read_bytes())
    assert [p["label"] for p in qc03.find_ortho_products(
        tmp_path / "run_98")] == ["swir"]


# ==================================================================================
def test_float_scan_excludes_nonfinite_from_stats(qc03, tmp_path):
    """NaN/Inf are counted once in n_naninf and corrupt nothing else."""
    h, w = 4, 4
    cube = np.full((2, h, w), 200.0, dtype="<f4")
    cube[:, 0, 0] = 0.0            # genuine background (all bands zero)
    cube[0, 1, 1] = 0.0            # partial-band zero in the footprint
    cube[0, 2, 2] = np.nan         # partial-band NaN
    cube[1, 2, 3] = np.inf         # +Inf must not count as over-range
    cube[0, 3, 3] = -np.inf        # -Inf must not count as negative
    cube[1, 3, 0] = -0.5           # genuine negative
    cube[:, 0, 1] = np.nan         # all-band NaN pixel != background
    bin_path = tmp_path / "SYN_SWIR_Orthomosaic.bin"
    bin_path.write_bytes(cube.tobytes())
    bin_path.with_suffix(".hdr").write_text(
        "ENVI\n"
        f"samples = {w}\nlines = {h}\nbands = 2\n"
        "header offset = 0\ndata type = 4\ninterleave = bsq\n"
        "byte order = 0\nwavelength = {900, 1000}\n", encoding="utf-8")

    acc = qc03._scan_bands_gdal(bin_path, 10000, 1, True, True, False)

    assert acc["footprint_px"] == h * w - 1        # only (0,0) is background
    assert not acc["all_zero"][0, 1]               # all-band NaN != background
    assert acc["n_naninf"].tolist() == [3, 2]
    assert acc["n_zero_fp"].tolist() == [1, 0]     # NaN never counts as zero
    assert acc["n_over"].tolist() == [0, 0]        # +Inf excluded
    assert acc["n_neg"].tolist() == [0, 1]         # -Inf excluded
    assert acc["n_valid"].tolist() == [13, 14]
    assert acc["band_min"].tolist() == [0.0, -0.5]
    assert acc["band_max"].tolist() == [200.0, 200.0]
    # sums/means cover finite samples only
    assert acc["band_sum"][0] == pytest.approx(11 * 200.0)
    assert acc["band_sum"][1] == pytest.approx(12 * 200.0 - 0.5)
    assert int(acc["hist"].sum(axis=1)[0]) == 13   # histogram = finite samples


# ==================================================================================
def test_min_rect_short_axis_handles_split_sides(qc03):
    """A collinear split side must not halve the short axis."""
    rect = np.array([[0.0, 0.0], [1.0, 0.0], [100.0, 0.0],
                     [100.0, 40.0], [0.0, 40.0]])
    assert qc03._min_rect_short_axis(rect) == pytest.approx(40.0)
    # plain rectangle: identical to the short edge
    plain = np.array([[0.0, 0.0], [100.0, 0.0], [100.0, 40.0], [0.0, 40.0]])
    assert qc03._min_rect_short_axis(plain) == pytest.approx(40.0)


# ==================================================================================
def test_load_capture_extent_drops_duplicate_vertices(qc03, tmp_path):
    """Adjacent duplicate vertices would divide by zero in the mask test."""
    dup = tmp_path / "dup.geojson"
    ring = [[0, 0], [1, 0], [1, 0], [1, 1], [0, 1], [0, 0], [0, 0]]
    dup.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Polygon", "coordinates": [ring]}}]}),
        encoding="utf-8")
    extent = qc03.load_capture_extent(dup)
    assert extent["n_verts"] == 4
    verts = np.asarray(extent["ring"], dtype=float)
    edges = np.roll(verts, -1, axis=0) - verts
    assert (np.hypot(edges[:, 0], edges[:, 1]) > 0).all()
