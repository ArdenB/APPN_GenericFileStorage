"""Tests for compact run labels and the adaptive node-family palette."""

import pandas as pd
import pytest

from Code.functions.core_functions.run_labels import (
    node_short_codes, build_run_labels)
from Code.functions.core_functions.run_palette import (
    resolve_run_palette, resolve_node_run_palette)


# ==================================================================================
def _ident(rows):
    """Build an identity frame from (node, project, site, sensor, date, run)."""
    return pd.DataFrame(
        rows, columns=["node", "project", "site", "sensor", "date", "run"])


# ==================================================================================
class TestNodeShortCodes:
    def test_leading_token(self):
        codes = node_short_codes(["USYD_Narrabri", "UQ_Gatton"])
        assert codes == {"USYD_Narrabri": "USYD", "UQ_Gatton": "UQ"}

    def test_token_collision_keeps_full_names(self):
        codes = node_short_codes(["USYD_Narrabri", "USYD_Camden", "UQ_Gatton"])
        assert codes["USYD_Narrabri"] == "USYD_Narrabri"
        assert codes["USYD_Camden"] == "USYD_Camden"
        assert codes["UQ_Gatton"] == "UQ"


# ==================================================================================
class TestBuildRunLabels:
    def test_single_node_omits_node(self):
        df = build_run_labels(_ident([
            ("USYD_Narrabri", "2026_APEx", "IAW", "GOBI", "20260805", "run_01"),
            ("USYD_Narrabri", "2026_APEx", "IAW", "GOBI", "20260805", "run_02"),
        ]))
        assert list(df["run_label"]) == ["20260805 run_01", "20260805 run_02"]

    def test_multi_node_prefixes_code(self):
        df = build_run_labels(_ident([
            ("USYD_Narrabri", "2026_APEx", "IAW", "GOBI", "20260805", "run_01"),
            ("UQ_Gatton", "2026_Sorg", "RedVale", "CALVIS", "20260805", "run_01"),
        ]))
        assert list(df["run_label"]) == [
            "USYD 20260805 run_01", "UQ 20260805 run_01"]

    def test_collision_appends_only_what_breaks_it(self):
        # same node/date/run in two projects -> sensor differs first
        df = build_run_labels(_ident([
            ("USYD_Narrabri", "2026_APEx", "IAW", "GOBI", "20260805", "run_01"),
            ("USYD_Narrabri", "2026_APEx", "IAW", "CALVIS", "20260805", "run_01"),
        ]))
        assert list(df["run_label"]) == [
            "20260805 run_01 GOBI", "20260805 run_01 CALVIS"]

    def test_collision_falls_through_to_project(self):
        df = build_run_labels(_ident([
            ("USYD_Narrabri", "2026_APEx", "IAW", "GOBI", "20260805", "run_01"),
            ("USYD_Narrabri", "2026_CWH", "IAW", "GOBI", "20260805", "run_01"),
        ]))
        assert list(df["run_label"]) == [
            "20260805 run_01 2026_APEx", "20260805 run_01 2026_CWH"]

    def test_identical_identities_get_counter(self):
        df = build_run_labels(_ident([
            ("N", "P", "S", "X", "20260805", "run_01"),
            ("N", "P", "S", "X", "20260805", "run_01"),
        ]).assign(other=[1, 2]))
        # duplicated identity rows share one label (same run, two tables)
        assert df["run_label"].nunique() == 1

    def test_run_number_zero_padded_from_int(self):
        df = build_run_labels(
            _ident([("N", "P", "S", "X", "20260805", 3)]))
        assert df["run_label"].iloc[0] == "20260805 run_03"

    def test_extra_cols_disambiguate(self):
        base = _ident([
            ("N", "P", "S", "X", "20260805", "run_01"),
            ("N", "P", "S", "X", "20260805", "run_01"),
        ]).assign(gpro_label=["g1", "g2"])
        df = build_run_labels(base, extra_cols=["gpro_label"])
        assert list(df["run_label"]) == [
            "20260805 run_01 g1", "20260805 run_01 g2"]

    def test_rows_map_back_to_their_identity(self):
        ident = _ident([
            ("USYD_Narrabri", "2026_APEx", "IAW", "GOBI", "20260805", "run_01"),
            ("UQ_Gatton", "2026_Sorg", "RedVale", "CALVIS", "20260812", "run_02"),
        ])
        big = pd.concat([ident] * 3, ignore_index=True)
        df = build_run_labels(big)
        assert (df.groupby("node")["run_label"].nunique() == 1).all()
        assert df["run_label"].nunique() == 2


# ==================================================================================
class TestResolveNodeRunPalette:
    def test_single_node_matches_qualitative(self):
        labels = ["20260805 run_01", "20260805 run_02"]
        node_by_label = {l: "USYD_Narrabri" for l in labels}
        assert (resolve_node_run_palette(node_by_label)
                == resolve_run_palette(labels))

    def test_multi_node_uses_families(self):
        node_by_label = {
            "USYD 20260805 run_01": "USYD_Narrabri",
            "USYD 20260805 run_02": "USYD_Narrabri",
            "UQ 20260812 run_01": "UQ_Gatton",
        }
        pal = resolve_node_run_palette(node_by_label)
        assert set(pal) == set(node_by_label)
        # RGBA tuples from colormaps, not the shared qualitative colours
        assert pal != resolve_run_palette(list(node_by_label))
        usyd = [pal["USYD 20260805 run_01"], pal["USYD 20260805 run_02"]]
        assert usyd[0] != usyd[1]

    def test_too_many_runs_per_node_falls_back(self):
        node_by_label = {f"USYD 20260805 run_{i:02d}": "USYD_Narrabri"
                         for i in range(1, 9)}
        node_by_label["UQ 20260812 run_01"] = "UQ_Gatton"
        assert (resolve_node_run_palette(node_by_label, max_family_runs=6)
                == resolve_run_palette(list(node_by_label)))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
