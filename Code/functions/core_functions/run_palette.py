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


# ==================================================================================
def resolve_node_run_palette(
        node_by_label: Dict[str, str],
        max_family_runs: int = 6,
    ) -> Dict[str, Any]:
    """Adaptive run palette: node colour families on multi-node scopes.

    Single-node scopes (and scopes the family scheme cannot resolve)
    fall through to :func:`resolve_run_palette` unchanged. Multi-node
    scopes with at most *max_family_runs* runs per node and at most six
    nodes assign each node a sequential-colormap family (blues,
    oranges, ...) with one shade per run, so node membership is
    readable off the lines themselves. Beyond those limits shades stop
    resolving, so the qualitative tiers take over again.

    Parameters
    ----------
    node_by_label : dict of str to str
        ``{run label: node}`` for every label appearing in the figures.
    max_family_runs : int, optional
        Most runs a node may hold before the family scheme is
        abandoned. Default 6.

    Returns
    -------
    dict of str to colour
        Mapping usable as the seaborn ``palette=`` argument, ordered
        node-by-node then by :func:`run_sort_key`.
    """
    families = ["Blues", "Oranges", "Greens", "Purples", "Reds", "Greys"]
    nodes = sorted({str(v) for v in node_by_label.values()})
    by_node = {node: sorted((str(l) for l, v in node_by_label.items()
                             if str(v) == node), key=run_sort_key)
               for node in nodes}
    if (len(nodes) <= 1 or len(nodes) > len(families)
            or max(len(v) for v in by_node.values()) > max_family_runs):
        return resolve_run_palette(list(node_by_label))
    import numpy as np
    from matplotlib import pyplot as plt
    palette: Dict[str, Any] = {}
    for fam, node in zip(families, nodes):
        cmap = plt.get_cmap(fam)
        shades = np.linspace(0.9, 0.45, max(len(by_node[node]), 2))
        for label, v in zip(by_node[node], shades):
            palette[label] = cmap(float(v))
    return palette
