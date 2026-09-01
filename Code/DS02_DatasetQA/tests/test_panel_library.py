"""Integrity tests for the reference/panels/ DHR library (plan §5b/§5d).

Pins each panel set's signature and the audit findings the library build
relied on: complete 1-nm wavelength grids, filename↔serial agreement,
node ownership from the manufacturer ``customer`` field, the 20240529
batch-identity finding (24006–24013 share one calibration curve), and
the tail-fill provenance of the seven dump-only sets.

Run with:
    pytest Code/DS02_DatasetQA/tests/test_panel_library.py -v
"""

import json
import pathlib

import pytest

# ---------------------------------------------------------------------------
# Repo root: Code/DS02_DatasetQA/tests -> repo root
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_LIB = _REPO_ROOT / "reference" / "panels"


# ==============================================================================
def expected_sets() -> dict:
    """Pinned library inventory: node -> {set serial -> (panels, max_nm)}.

    Returns
    -------
    dict
        Expected sets per node with nominal panel signatures and the
        top of each set's wavelength range.
    """
    return {
        "AU": {"24005": (("11", "30", "56", "82"), 2500),
               "25005": (("11", "30", "56", "82"), 2500),
               "26004": (("20", "45"), 5000)},
        "UQ": {"24006": (("11", "30", "56", "82"), 2500),
               "24007": (("11", "30", "56", "82"), 2500),
               "26002": (("20", "45"), 5000)},
        "USYD": {"24008": (("11", "30", "56", "82"), 2500),
                 "24009": (("11", "30", "56", "82"), 2500),
                 "26001": (("20", "45"), 5000)},
        "CSU": {"24010": (("11", "30", "56", "82"), 2500),
                "24011": (("11", "30", "56", "82"), 2500),
                "26003": (("20", "45"), 5000)},
        "UWA": {"24012": (("11", "30", "56", "82"), 2500),
                "24013": (("11", "30", "56", "82"), 2500),
                "26005": (("20", "45"), 5000)},
        "DPIRD": {"26006": (("20", "45"), 5000)},
    }


# ==============================================================================
def load_panel(node: str, serial: str, panel: str) -> dict:
    """Load one panel JSON from the library.

    Parameters
    ----------
    node : str
        Node folder name.
    serial : str
        Set serial (e.g. "24008").
    panel : str
        Nominal reflectance suffix (e.g. "82").

    Returns
    -------
    dict
        Parsed manufacturer JSON.
    """
    path = _LIB / node / f"UF200-{serial}" / f"UF200-{serial}-{panel}.json"
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ==============================================================================
def test_inventory_complete_and_exclusive():
    """Every expected file exists and nothing unexpected is present."""
    expected_files = {
        _LIB / node / f"UF200-{serial}" / f"UF200-{serial}-{panel}.json"
        for node, sets in expected_sets().items()
        for serial, (panels, _) in sets.items()
        for panel in panels
    }
    actual_files = set(_LIB.rglob("*.json"))
    assert actual_files == expected_files


# ==============================================================================
@pytest.mark.parametrize(
    "node,serial,panels,max_nm",
    [(node, serial, panels, max_nm)
     for node, sets in expected_sets().items()
     for serial, (panels, max_nm) in sets.items()],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_set_integrity(node, serial, panels, max_nm):
    """Serial matches filename, grid is complete 1-nm 300..max, DHR sane."""
    for panel in panels:
        data = load_panel(node, serial, panel)
        assert data["serial"] == f"{serial}-{panel}"
        assert data["type"] == "UF200"
        wavelengths = [p["wavelength_nm"] for p in data["dhr"]]
        assert wavelengths == list(range(300, max_nm + 1))
        assert all(0.0 < p["reflectance"] < 1.0 for p in data["dhr"])


# ==============================================================================
def test_batch_sets_identical_to_24008():
    """20240529 batch finding: 24006-24013 curves == 24008's, per panel."""
    batch = ["24006", "24007", "24009", "24010", "24011", "24012", "24013"]
    node_of = {serial: node for node, sets in expected_sets().items()
               for serial in sets}
    for panel in ("11", "30", "56", "82"):
        ref = load_panel("USYD", "24008", panel)["dhr"]
        for serial in batch:
            assert load_panel(node_of[serial], serial, panel)["dhr"] == ref


# ==============================================================================
def test_24005_differs_from_batch():
    """24005 (20240508 delivery) has its own curves - not the batch curve."""
    for panel in ("11", "30", "56", "82"):
        ref = load_panel("USYD", "24008", panel)["dhr"]
        assert load_panel("AU", "24005", panel)["dhr"] != ref


# ==============================================================================
def test_tail_fill_provenance_marker():
    """Tail-filled sets carry the marker; verbatim sets do not."""
    filled = {"24006", "24007", "24009", "24010", "24011", "24012", "24013"}
    for node, sets in expected_sets().items():
        for serial, (panels, _) in sets.items():
            for panel in panels:
                data = load_panel(node, serial, panel)
                assert ("dhr_tail_provenance" in data) == (serial in filled)


# ==============================================================================
def test_customer_field_matches_node():
    """Node ownership was derived from the customer field - keep it true."""
    fragments = {"AU": ("Adelaide", "APPN-AU"), "UQ": ("Queensland", "APPN-UQ"),
                 "USYD": ("Sydney", "USyd"), "CSU": ("CSU",),
                 "UWA": ("Western Australia", "APPN-WA"), "DPIRD": ("DPIRD",)}
    for node, sets in expected_sets().items():
        expect = fragments[node]
        for serial, (panels, _) in sets.items():
            customer = load_panel(node, serial, panels[0])["customer"]
            assert any(frag in customer for frag in expect), (
                f"{node}/{serial}: customer {customer!r}")


# ==============================================================================
# Resolver behaviour (spectral_qc.panel_library, §5b rules) against the
# real on-disk library.
# ==============================================================================
import sys  # noqa: E402

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import Code.functions.spectral_qc as sq  # type: ignore # noqa: E402


def test_resolver_pin_wins_library_wide():
    set_dir, res = sq.resolve_panel_set("UQ_Gatton", set(),
                                        set_key="UF200-24008")
    assert set_dir.name == "UF200-24008" and set_dir.parent.name == "USYD"
    assert res["method"] == "pin"


def test_resolver_pin_missing_is_hard_error():
    with pytest.raises(FileNotFoundError, match="refusing to substitute"):
        sq.resolve_panel_set("USYD_Narrabri", set(), set_key="UF200-99999")


def test_resolver_signature_unique_two_panel_set():
    set_dir, res = sq.resolve_panel_set("USYD_Narrabri", {"20", "45"})
    assert set_dir.name == "UF200-26001"
    assert res["method"] == "signature"


def test_resolver_elimination_via_elm_pin():
    # Two 4-panel sets per node; excluding the gpro-pinned ELM set
    # identifies the VAL set.
    set_dir, res = sq.resolve_panel_set(
        "USYD_Narrabri", {"11", "30", "56", "82"},
        exclude_key="UF200-24008")
    assert set_dir.name == "UF200-24009"
    assert res["method"] == "elimination"


def test_resolver_identical_candidates_resolve():
    # 24008/24009 share the 20240529 batch calibration curve, so the
    # unpinned ambiguity is harmless and resolves with both recorded.
    set_dir, res = sq.resolve_panel_set(
        "USYD_Narrabri", {"11", "30", "56", "82"})
    assert res["method"] == "identical_candidates"
    assert res["candidates"] == ["UF200-24008", "UF200-24009"]
    assert set_dir.name == "UF200-24008"


def test_resolver_differing_candidates_stay_hard_error():
    # AU's 24005 vs 25005 genuinely differ (~3 pp SWIR): never guess.
    with pytest.raises(LookupError, match="numerically differing"):
        sq.resolve_panel_set("AU_Adelaide", {"11", "30", "56", "82"})


def test_resolver_unknown_node_no_fallback():
    with pytest.raises(FileNotFoundError, match="no cross-node fallback"):
        sq.resolve_panel_set("NOWHERE_Node", {"11"})
