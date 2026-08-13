"""Tests for the plot_layout discovery/validation helpers.

All tests build their fixture trees under pytest's ``tmp_path`` so they
are machine-independent (no repo data required).
"""

import json
import pathlib

import pandas as pd
import pytest
import geopandas as gpd
from shapely.geometry import Polygon

from Code.functions.plot_layout import (
    site_base_name,
    plot_layout_dir,
    find_plot_file,
    load_plot_file,
    load_site_plots,
    find_trial_info,
)


# ==================================================================================
def _write_geojson(path: pathlib.Path, n_plots: int = 3,
                   id_col: str = "plot_id", crs: str = "EPSG:7855",
                   dup: bool = False) -> pathlib.Path:
    """Write a minimal plot polygon GeoJSON for testing."""
    ids = list(range(1, n_plots + 1))
    if dup and n_plots >= 2:
        ids[1] = ids[0]
    geoms = [Polygon([(i, 0), (i + 1, 0), (i + 1, 1), (i, 1)]) for i in ids]
    gdf = gpd.GeoDataFrame({id_col: ids}, geometry=geoms, crs=crs)
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path, driver="GeoJSON")
    return path


@pytest.fixture
def site_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """A site folder with an empty Plot_Layout dir."""
    site = tmp_path / "USYD_Narrabri" / "2026_Proj" / "2026TestSite_F"
    plot_layout_dir(site).mkdir(parents=True)
    return site


# ==================================================================================
class TestSiteBaseName:
    def test_strips_field_suffix(self):
        assert site_base_name("2025IAWatson_F") == "2025IAWatson"

    def test_strips_controlled_suffix(self):
        assert site_base_name("2025Glasshouse_C") == "2025Glasshouse"

    def test_no_suffix_unchanged(self):
        assert site_base_name("2026Roseworthy-SA") == "2026Roseworthy-SA"

    def test_internal_underscore_kept(self):
        assert site_base_name("2026Area1_East") == "2026Area1_East"


# ==================================================================================
class TestFindPlotFile:
    def test_main_file_found(self, site_dir):
        main = _write_geojson(plot_layout_dir(site_dir) / "2026TestSite_plots.geojson")
        found, issues = find_plot_file(site_dir)
        assert found == main
        assert issues == []

    def test_missing_main_is_an_issue(self, site_dir):
        found, issues = find_plot_file(site_dir)
        assert found is None
        assert any("2026TestSite_plots.geojson" in i for i in issues)

    def test_missing_layout_dir(self, tmp_path):
        site = tmp_path / "n" / "2026_P" / "2026NoDocs"
        site.mkdir(parents=True)
        found, issues = find_plot_file(site)
        assert found is None
        assert any("Plot_Layout folder not found" in i for i in issues)

    def test_variant_selected(self, site_dir):
        layout = plot_layout_dir(site_dir)
        _write_geojson(layout / "2026TestSite_plots.geojson")
        variant = _write_geojson(layout / "2026TestSite_plots_unbuffered.geojson")
        found, issues = find_plot_file(site_dir, variant="unbuffered")
        assert found == variant
        assert issues == []

    def test_variant_highest_version_wins(self, site_dir):
        layout = plot_layout_dir(site_dir)
        _write_geojson(layout / "2026TestSite_plots_HIRES_v01.geojson")
        v2 = _write_geojson(layout / "2026TestSite_plots_HIRES_v02.geojson")
        found, _ = find_plot_file(site_dir, variant="HIRES")
        assert found == v2

    def test_missing_variant_is_an_issue(self, site_dir):
        _write_geojson(plot_layout_dir(site_dir) / "2026TestSite_plots.geojson")
        found, issues = find_plot_file(site_dir, variant="nope")
        assert found is None
        assert any("nope" in i for i in issues)

    def test_deprecated_ignored(self, site_dir):
        layout = plot_layout_dir(site_dir)
        _write_geojson(layout / "2026TestSite_plots_deprecated.geojson")
        found, issues = find_plot_file(site_dir)
        assert found is None  # deprecated file must not satisfy the main slot

    def test_shp_legacy_fallback_warns(self, site_dir):
        layout = plot_layout_dir(site_dir)
        ids = [1, 2]
        gdf = gpd.GeoDataFrame(
            {"plot_id": ids},
            geometry=[Polygon([(i, 0), (i + 1, 0), (i + 1, 1)]) for i in ids],
            crs="EPSG:7855")
        shp = layout / "2026TestSite_plots.shp"
        gdf.to_file(shp)
        with pytest.warns(UserWarning, match="legacy shapefile"):
            found, issues = find_plot_file(site_dir)
        assert found == shp
        assert issues == []

    def test_geojson_preferred_over_shp(self, site_dir):
        layout = plot_layout_dir(site_dir)
        main = _write_geojson(layout / "2026TestSite_plots.geojson")
        gpd.GeoDataFrame(
            {"plot_id": [1]}, geometry=[Polygon([(0, 0), (1, 0), (1, 1)])],
            crs="EPSG:7855").to_file(layout / "2026TestSite_plots.shp")
        found, _ = find_plot_file(site_dir)
        assert found == main


# ==================================================================================
class TestLoadPlotFile:
    def test_valid_file_loads(self, site_dir):
        path = _write_geojson(plot_layout_dir(site_dir) / "2026TestSite_plots.geojson")
        gdf, issues = load_plot_file(path)
        assert gdf is not None
        assert issues == []
        assert list(gdf["plot_id"]) == [1, 2, 3]

    def test_missing_plot_id(self, site_dir):
        path = _write_geojson(
            plot_layout_dir(site_dir) / "2026TestSite_plots.geojson", id_col="fid")
        gdf, issues = load_plot_file(path)
        assert gdf is None
        assert any("plot_id" in i for i in issues)

    def test_duplicate_plot_id(self, site_dir):
        path = _write_geojson(
            plot_layout_dir(site_dir) / "2026TestSite_plots.geojson", dup=True)
        gdf, issues = load_plot_file(path)
        assert gdf is None
        assert any("duplicate" in i for i in issues)

    def test_trial_info_join(self, site_dir, tmp_path):
        path = _write_geojson(plot_layout_dir(site_dir) / "2026TestSite_plots.geojson")
        trial = tmp_path / "2026TestSite_trial_info.csv"
        pd.DataFrame({"plot_id": [1, 2, 3], "genotype": ["a", "b", "c"]}
                     ).to_csv(trial, index=False)
        gdf, issues = load_plot_file(path, trial_info=trial)
        assert gdf is not None
        assert issues == []
        assert list(gdf["genotype"]) == ["a", "b", "c"]

    def test_trial_info_unmatched_plots_flagged(self, site_dir, tmp_path):
        path = _write_geojson(plot_layout_dir(site_dir) / "2026TestSite_plots.geojson")
        trial = tmp_path / "trial.csv"
        pd.DataFrame({"plot_id": [1], "genotype": ["a"]}).to_csv(trial, index=False)
        gdf, issues = load_plot_file(path, trial_info=trial)
        assert gdf is not None
        assert any("no row" in i for i in issues)

    def test_trial_info_missing_plot_id_skips_join(self, site_dir, tmp_path):
        path = _write_geojson(plot_layout_dir(site_dir) / "2026TestSite_plots.geojson")
        trial = tmp_path / "trial.csv"
        pd.DataFrame({"plot": [1]}).to_csv(trial, index=False)
        gdf, issues = load_plot_file(path, trial_info=trial)
        assert gdf is not None
        assert "genotype" not in gdf.columns
        assert any("plot_id" in i for i in issues)


# ==================================================================================
class TestLoadSitePlots:
    def test_loads_each_site_once(self, site_dir):
        _write_geojson(plot_layout_dir(site_dir) / "2026TestSite_plots.geojson")
        result = load_site_plots([site_dir, site_dir])
        assert list(result) == [site_dir]
        gdf, issues = result[site_dir]
        assert gdf is not None
        assert issues == []
        assert gdf.attrs["plot_file"].name == "2026TestSite_plots.geojson"

    def test_missing_main_warns_and_returns_none(self, site_dir):
        with pytest.warns(UserWarning, match="Mandatory main plot file"):
            result = load_site_plots([site_dir])
        gdf, issues = result[site_dir]
        assert gdf is None
        assert issues

    def test_trial_info_flag_without_csv_flags_issue(self, site_dir):
        _write_geojson(plot_layout_dir(site_dir) / "2026TestSite_plots.geojson")
        with pytest.warns(UserWarning, match="no trial-info CSV"):
            result = load_site_plots([site_dir], join_trial_info=True)
        gdf, issues = result[site_dir]
        assert gdf is not None  # missing trial info is a warning, not a failure


# ==================================================================================
class TestFindTrialInfo:
    def test_found(self, site_dir):
        tdir = site_dir / "Documentation" / "Trial_Info"
        tdir.mkdir(parents=True)
        csv = tdir / "2026TestSite_trial_info.csv"
        csv.write_text("plot_id\n1\n")
        assert find_trial_info(site_dir) == csv

    def test_absent(self, site_dir):
        assert find_trial_info(site_dir) is None
