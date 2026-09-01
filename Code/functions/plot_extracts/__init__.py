"""Shared helpers for the DS03 ``PlotExtracts`` output tree.

The DS03 extraction scripts (PE00/PE01/PE02) write into a common
per-run folder layout under ``<run>/T1_proc/PlotExtracts/``:

- ``PixelLevel/`` — heavy point/pixel-level parquet *datasets*
  (directories of per-plot or per-chunk part files) plus their YAML
  provenance sidecars. A dataset's sidecar is written **last**, after
  all part files, so it doubles as the completion marker and the mtime
  anchor for ``outputs_up_to_date`` caching.
- ``PlotLevel/`` — light, analyst-facing per-plot metric tables
  (single parquet files + sidecars).
- ``Reports/`` — markdown QC reports and their ``PE_figures/``.

Part files are written atomically (``.tmp`` then ``os.replace``) so a
crash never leaves a corrupt part that looks fresh to the caching
checks.

The per-group statistics helpers (:func:`group_value_stats`,
:func:`group_value_percentiles`) implement the shared PlotLevel metric
set so PE00/PE01/PE02 all emit identical column schemas. They now live
in ``Code.functions.core_functions.group_stats`` (shared with the DS02
panel-homogeneity statistics) and are re-exported here unchanged.
"""

__version__ = "1.2.0"
__author__ = "Arden Burrell"

import os
import pathlib
from typing import Dict, List, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# +++++ Shared group statistics moved to core_functions (re-exported) +++++
from ..core_functions.group_stats import (group_value_stats,
                                          group_value_percentiles)


# ==================================================================================
def plotextract_dirs(t1_dir: pathlib.Path) -> Dict[str, pathlib.Path]:
    """Return the canonical PlotExtracts sub-folder paths for one run.

    Parameters
    ----------
    t1_dir : pathlib.Path
        The run's ``T1_proc`` directory.

    Returns
    -------
    dict of str to pathlib.Path
        Keys ``extracts`` (``PlotExtracts/``), ``pixel``
        (``PixelLevel/``), ``plot`` (``PlotLevel/``) and ``reports``
        (``Reports/``). Paths are returned, not created.
    """
    extracts = t1_dir / "PlotExtracts"
    return {
        "extracts": extracts,
        "pixel": extracts / "PixelLevel",
        "plot": extracts / "PlotLevel",
        "reports": extracts / "Reports",
    }


# ==================================================================================
def write_dataset_part(
        df: pd.DataFrame,
        part_file: pathlib.Path,
        compression: str = "zstd",
    ) -> None:
    """Atomically write one part file of a parquet dataset directory.

    Object-dtype columns (``plot_id``, ``index``, run-metadata strings)
    are dictionary-encoded, and the write goes to a ``.tmp`` sibling
    first then ``os.replace``-d into place, so readers and the mtime
    caching never see a partial part.

    Parameters
    ----------
    df : pandas.DataFrame
        Rows for this part (typically one plot or one scan chunk).
    part_file : pathlib.Path
        Destination ``.parquet`` path inside the dataset directory
        (parent created if missing).
    compression : str, optional
        Parquet compression codec. Default ``"zstd"``.

    Returns
    -------
    None
    """
    df = df.copy(deep=False)
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype("category")
    table = pa.Table.from_pandas(df, preserve_index=False)
    part_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = part_file.with_suffix(part_file.suffix + ".tmp")
    pq.write_table(table, tmp, compression=compression)
    os.replace(tmp, part_file)


# ==================================================================================
def dataset_parts(dataset_dir: pathlib.Path) -> List[pathlib.Path]:
    """List the part files of a parquet dataset directory.

    Parameters
    ----------
    dataset_dir : pathlib.Path
        The dataset directory.

    Returns
    -------
    list of pathlib.Path
        Sorted ``*.parquet`` part paths; empty when the directory does
        not exist. ``.tmp`` leftovers from interrupted writes are
        excluded by the suffix match.
    """
    if not dataset_dir.is_dir():
        return []
    return sorted(dataset_dir.glob("*.parquet"))


# ==================================================================================
def dataset_row_count(dataset_dir: pathlib.Path) -> Optional[int]:
    """Sum the row counts of a dataset directory from the part footers.

    Parameters
    ----------
    dataset_dir : pathlib.Path
        The dataset directory.

    Returns
    -------
    int or None
        Total rows across all parts (footer metadata only — no data is
        read), or None when the directory is empty or a footer cannot
        be read.
    """
    parts = dataset_parts(dataset_dir)
    if not parts:
        return None
    try:
        return sum(pq.read_metadata(p).num_rows for p in parts)
    except (OSError, pa.ArrowInvalid):
        return None
