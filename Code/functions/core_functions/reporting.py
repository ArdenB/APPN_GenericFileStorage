"""Shared report-output helpers for the DS02 comparison scripts.

Output-folder routing for the QAReports convention, preview-safe
filename components, and a dependency-free markdown table writer.
"""

import re
import pathlib
from typing import Optional

import pandas as pd


# ==================================================================================
def resolve_qareports_dir(
        path: pathlib.Path,
        output_dir: Optional[str],
        no_save: bool,
    ) -> Optional[pathlib.Path]:
    """Resolve the cross-run output folder from the path level.

    Node folders save to ``<Node>/Documents/QAReports/``, project
    folders to ``<Project>/Documentation/QAReports/``, site folders to
    ``<Site>/Documentation/QAReports/``. Any other level requires an
    explicit ``--output-dir`` (or ``--no-save``).

    Parameters
    ----------
    path : pathlib.Path
        The crawl root passed on the command line.
    output_dir : str or None
        Explicit output directory (overrides the level-based routing).
    no_save : bool
        When True nothing is saved and None is returned.

    Returns
    -------
    pathlib.Path or None
        The output directory (created if missing), or None in
        ``--no-save`` mode.

    Raises
    ------
    ValueError
        If the path is not node, project, or site level and no
        ``--output-dir`` was given (and ``--no-save`` is not set).
    """
    from .parse_APPN_dataset_path import parse_APPN_dataset_path
    if no_save:
        print("*** --no-save: figures will be displayed, NOT saved. ***")
        return None
    if output_dir is not None:
        out = pathlib.Path(output_dir)
    else:
        parsed = parse_APPN_dataset_path(path)
        level = parsed.get("path_level")
        # The path parser cannot classify a bare node folder (no APPN
        # pattern in the name), so detect node level via its markers.
        is_node = ((path / "Documents").is_dir()
                   or any(path.glob("*_ProjectsSummary.csv")))
        if level in ("project", "site"):
            out = path / "Documentation" / "QAReports"
        elif is_node:
            out = path / "Documents" / "QAReports"
        else:
            raise ValueError(
                f"{path} parses as level '{level}', not a node, project, or "
                "site folder. Provide --output-dir to choose where the "
                "comparison outputs are saved (or --no-save to only "
                "display them).")
    out.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {out}")
    return out


# ==================================================================================
def scope_label(path: pathlib.Path) -> str:
    """Build the filename scope label for a QA crawl root (plan §4).

    Cross-run report filenames carry the crawl scope so comparisons at
    different scopes never clobber each other.

    Parameters
    ----------
    path : pathlib.Path
        The crawl root passed on the command line.

    Returns
    -------
    str
        Preview-safe label from the parsed APPN levels (node, project,
        site, sensor — whichever apply), falling back to the folder name.
    """
    from .parse_APPN_dataset_path import parse_APPN_dataset_path
    parsed = parse_APPN_dataset_path(path)
    parts = [str(parsed[key]) for key in ("node", "project", "site", "sensor")
             if parsed.get(key)]
    return safe_filename_component("-".join(parts) or path.name)


# ==================================================================================
def safe_filename_component(value: str) -> str:
    """Convert a plot label to a stable, preview-safe filename component.

    ``%`` is expanded to ``percent`` because a literal ``%`` in a
    filename breaks the VS Code markdown preview even when encoded.

    Parameters
    ----------
    value : str
        Sensor, target, region, or metric label.

    Returns
    -------
    str
        ASCII filename component without spaces or URL-sensitive symbols.
    """
    expanded = value.replace("_pct", "_percent").replace("%", "percent")
    return re.sub(r"[^A-Za-z0-9_-]+", "_", expanded).strip("_")


# ==================================================================================
def markdown_table(df: pd.DataFrame, float_fmt: str = "{:.3f}") -> str:
    """Render a DataFrame as a GitHub-flavoured markdown pipe table.

    A dependency-free alternative to ``DataFrame.to_markdown`` (which
    requires ``tabulate``). Floats are formatted with *float_fmt*;
    NaN/None render as empty cells.

    Parameters
    ----------
    df : pd.DataFrame
        Table to render. Column order is preserved; the index is not
        included.
    float_fmt : str, optional
        ``str.format`` pattern applied to float values.
        Default ``"{:.3f}"``.

    Returns
    -------
    str
        The markdown table (no trailing newline).
    """
    def _cell(val) -> str:
        if val is None or (isinstance(val, float) and pd.isna(val)) or val is pd.NA:
            return ""
        if isinstance(val, float):
            return float_fmt.format(val)
        return str(val).replace("|", "\\|")

    header = "| " + " | ".join(str(c) for c in df.columns) + " |"
    rule = "|" + "|".join([" --- "] * len(df.columns)) + "|"
    rows = ["| " + " | ".join(_cell(v) for v in row) + " |"
            for row in df.itertuples(index=False, name=None)]
    return "\n".join([header, rule, *rows])
