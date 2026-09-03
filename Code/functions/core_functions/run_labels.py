"""Compact display labels for multi-run comparison figures.

Run labels used as legend entries grow unreadable when a comparison
scope spans many projects/sites/sensors. The convention here (QA02
2026-09 redesign) is: **always show node (multi-node scopes only) +
date + run number**; everything else (project, site, sensor, gpro)
lives in the ``run_key`` table written next to the figures and only
enters a label when needed to break a collision.
"""

from collections import Counter
from typing import Dict, Iterable, Sequence

import pandas as pd


# ==================================================================================
def node_short_codes(nodes: Iterable[str]) -> Dict[str, str]:
    """Map node names to short display codes.

    Uses the leading ``_``-separated token (``USYD_Narrabri -> USYD``);
    nodes whose token collides with another node's keep their full name
    (``USYD_Narrabri``/``USYD_Camden`` both stay full).

    Parameters
    ----------
    nodes : iterable of str
        Node names.

    Returns
    -------
    dict of str to str
        ``{node: short code or full name}``.
    """
    toks = {str(n): str(n).split("_")[0] for n in set(nodes)}
    counts = Counter(toks.values())
    return {n: (t if counts[t] == 1 else n) for n, t in toks.items()}


# ==================================================================================
def build_run_labels(
        df: pd.DataFrame,
        date_col: str = "date",
        run_col: str = "run",
        extra_cols: Sequence[str] = (),
    ) -> pd.DataFrame:
    """Attach compact ``run_label``/``node_label`` display columns.

    Base label = ``[node code] <date> run_NN``: the node code (see
    :func:`node_short_codes`) enters only when the frame spans more
    than one node. Where two distinct run identities still share a
    label, disambiguators are appended for the colliding rows only, in
    the order sensor, project, site, then *extra_cols*, then a ``#n``
    counter as a last resort. Full identity therefore belongs in an
    accompanying key table, not in the label.

    Parameters
    ----------
    df : pd.DataFrame
        Frame with a ``node`` column plus *date_col* and *run_col*.
        ``project``/``site``/``sensor`` are used when present.
    date_col : str, optional
        Column holding the display date string. Default ``"date"``.
    run_col : str, optional
        Column holding the run number (int or string containing
        digits, e.g. ``"run_01"``). Default ``"run"``.
    extra_cols : sequence of str, optional
        Additional disambiguator columns tried after site (e.g. a
        gpro-reprocessing label). Default ().

    Returns
    -------
    pd.DataFrame
        Copy of *df* with ``run_label`` and ``node_label`` columns.
    """
    df = df.copy()
    codes = node_short_codes(df["node"].astype(str).unique())
    df["node_label"] = df["node"].astype(str).map(codes)

    disamb = [c for c in ("sensor", "project", "site") if c in df.columns]
    disamb += [c for c in extra_cols if c in df.columns]
    ident_cols = ["node", "node_label", date_col, run_col] + disamb
    uniq = df[ident_cols].drop_duplicates().reset_index(drop=True)

    # ========== Base label: [node code] date run_NN ==========
    run_num = pd.to_numeric(
        uniq[run_col].astype(str).str.extract(r"(\d+)", expand=False),
        errors="coerce")
    run_part = ("run_" + run_num.astype("Int64").astype(str).str.zfill(2)
                ).where(run_num.notna(), uniq[run_col].astype(str))
    parts = []
    if uniq["node"].nunique() > 1:
        parts.append(uniq["node_label"])
    parts.append(uniq[date_col].astype(str))
    parts.append(run_part)
    uniq["run_label"] = parts[0].str.cat(parts[1:], sep=" ")

    # ========== Collision-only disambiguation, then #n fallback ==========
    for col in disamb:
        dup = uniq.duplicated("run_label", keep=False)
        if not dup.any():
            break
        sub = uniq.loc[dup]
        varies = sub.groupby("run_label")[col].transform("nunique") > 1
        idx = sub.index[varies]
        uniq.loc[idx, "run_label"] = (
            uniq.loc[idx, "run_label"] + " " + uniq.loc[idx, col].astype(str))
    dup = uniq.duplicated("run_label", keep=False)
    if dup.any():
        counter = uniq.loc[dup].groupby("run_label").cumcount() + 1
        uniq.loc[dup, "run_label"] += " #" + counter.astype(str)

    merged = df.merge(uniq, on=ident_cols, how="left")
    df["run_label"] = merged["run_label"].to_numpy()
    return df
