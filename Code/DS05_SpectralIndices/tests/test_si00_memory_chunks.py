"""Regression test for SI00 ``_memory_chunks`` (16 GB laptop fix, v1.1).

The old budget ``max(avail - 8e9, 4e9)`` promised 4 GB the machine might
not have; the floor is now capped at half of actual availability.

Run with:
    pytest Code/DS05_SpectralIndices/tests/test_si00_memory_chunks.py -v
"""

import importlib.util
import pathlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import xarray as xr

# ---------------------------------------------------------------------------
# Ensure repo root is importable (SI00 imports Code.functions.*)
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ==============================================================================
@pytest.fixture(scope="module")
def si00():
    """Load SI00_SpectralIndices as a module."""
    fpath = _REPO_ROOT / "Code" / "DS05_SpectralIndices" / "SI00_SpectralIndices.py"
    spec = importlib.util.spec_from_file_location("SI00_SpectralIndices", fpath)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ==============================================================================
def test_memory_chunks_budget_fits_small_ram(si00, monkeypatch):
    """Chunks must fit in what is actually free, even on small-RAM boxes."""
    # 10k x 10k grid -> 400 MB per float32 index map.
    ref = xr.DataArray(np.zeros((1, 10_000, 10_000), np.float32),
                       dims=("band", "y", "x"))
    bytes_per_index = 10_000 * 10_000 * 4
    valid = [f"IDX{i:02d}" for i in range(40)]
    cfg = si00.SIConfig()

    for avail in (1e9, 3e9, 10e9):
        monkeypatch.setattr(
            si00.psutil, "virtual_memory",
            lambda a=avail: SimpleNamespace(available=a))
        chunks = si00._memory_chunks(ref, valid, cfg)
        # Every index computed exactly once, order preserved.
        assert [i for c in chunks for i in c] == valid
        # Chunk memory (x2 for dask intermediates) never exceeds what is
        # free, unless the hard minimum of one index per chunk forces it.
        for chunk in chunks:
            assert (len(chunk) * 2 * bytes_per_index <= avail
                    or len(chunk) == 1), (
                f"avail={avail:.0e}: chunk of {len(chunk)} over-commits RAM "
                f"(old 4e9 floor bug)")

    # Plenty of RAM -> a single chunk.
    monkeypatch.setattr(si00.psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=128e9))
    assert si00._memory_chunks(ref, valid, cfg) == [valid]
