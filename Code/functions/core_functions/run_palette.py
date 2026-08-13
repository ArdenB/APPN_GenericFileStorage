"""Tiered qualitative colour palettes for multi-run comparison figures.

Moved out of ``spectral_qc`` so non-spectral scripts (e.g. the DS02 GCP
run comparison) can share the same run-colour conventions without
importing QC-spectra code.
"""

import re
import itertools
from typing import Any, Dict, List


# ==================================================================================
def run_sort_key(label: str) -> tuple:
    """Sort key putting run labels in acquisition (run-number) order.

    Labels embedding a ``run_NN`` component sort by that number; labels
    without one keep plain lexicographic order after the numbered runs.

    Parameters
    ----------
    label : str
        Run label.

    Returns
    -------
    tuple
        ``(run_number, label)`` with ``run_number = inf`` when absent.
    """
    m = re.search(r"run[_ ]?(\d+)", str(label), flags=re.IGNORECASE)
    return (int(m.group(1)) if m else float("inf"), str(label))


# ==================================================================================
def resolve_run_palette(labels: List[str]) -> Dict[str, Any]:
    """Build a stable ``{run label: colour}`` map using the APEx tiers.

    <=10 runs use the curated CARTO ``Bold`` qualitative palette, 11-20
    runs use ``Tableau_20`` (ET00 convention), and >20 runs fall back to
    ``glasbey_dark`` (256 maximally-distinct colours) so large combined
    figures never reuse a hue. Labels are ordered by :func:`run_sort_key`
    so a given run keeps its colour in every figure of a session.

    Parameters
    ----------
    labels : list of str
        All run labels that will appear across the figures.

    Returns
    -------
    dict of str to colour
        Mapping usable as the seaborn ``palette=`` argument.
    """
    import palettable
    ordered = sorted({str(l) for l in labels}, key=run_sort_key)
    n = len(ordered)
    if n <= 10:
        colours = palettable.cartocolors.qualitative.Bold_10.mpl_colors  # pyright: ignore[reportAttributeAccessIssue]
    elif n <= 20:
        colours = palettable.tableau.Tableau_20.mpl_colors  # pyright: ignore[reportAttributeAccessIssue]
    else:
        import colorcet
        colours = list(itertools.islice(
            itertools.cycle(colorcet.glasbey_dark), n))
    return dict(zip(ordered, colours))
