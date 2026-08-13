"""Plot-Layout vector file discovery and validation (wiki Key-Files spec).

Shared helpers for locating and loading the site-level plot polygon files
stored in ``<site>/Documentation/Plot_Layout/`` following the APPN
Plot Delineation naming convention:

- Main plot file (mandatory): ``{YYYYSiteName}_plots.geojson``
- Variants (optional): ``{YYYYSiteName}_plots_{descriptor|sensor}[_v{NN}].geojson``
- Deprecated copies carry a terminal ``_deprecated`` tag and are ignored.

``{YYYYSiteName}`` is the site folder name without the trailing ``_F`` /
``_C`` suffix (e.g. site ``2025IAWatson_F`` -> ``2025IAWatson``).

See https://github.com/ArdenB/APPN_GenricFileStorage/wiki/Key-Files
(Plot_Layout section) for the full specification.
"""

import pathlib
import warnings as warn
from typing import Optional, Tuple, List

import pandas as pd
import geopandas as gpd


# ==================================================================================
def site_base_name(site_folder: str) -> str:
    """Return the ``{YYYYSiteName}`` filename token for a site folder.

    Strips the trailing ``_F`` (field) or ``_C`` (controlled environment)
    suffix from the site folder name, per the Plot_Layout spec.

    Parameters
    ----------
    site_folder : str
        Site folder name (e.g. ``"2025IAWatson_F"``).

    Returns
    -------
    str
        The base name used in Plot_Layout filenames
        (e.g. ``"2025IAWatson"``).
    """
    for suffix in ("_F", "_C"):
        if site_folder.endswith(suffix):
            return site_folder[: -len(suffix)]
    return site_folder


# ==================================================================================
def plot_layout_dir(site_dir: pathlib.Path) -> pathlib.Path:
    """Return the Plot_Layout folder for a site directory.

    Parameters
    ----------
    site_dir : pathlib.Path
        The site folder (``<root>/<node>/<project>/<site>``).

    Returns
    -------
    pathlib.Path
        ``<site_dir>/Documentation/Plot_Layout``.
    """
    return site_dir / "Documentation" / "Plot_Layout"


# ==================================================================================
def find_plot_file(
        site_dir: pathlib.Path,
        variant: Optional[str] = None,
    ) -> Tuple[Optional[pathlib.Path], List[str]]:
    """Locate the plot polygon file for a site per the Plot_Layout spec.

    The main file ``{YYYYSiteName}_plots.geojson`` is mandatory and the
    default. When *variant* is given, the matching variant file
    ``{YYYYSiteName}_plots_{variant}.geojson`` (optionally versioned
    ``_v{NN}``; the highest version wins) is selected instead. Files with
    a terminal ``_deprecated`` tag are always ignored. A legacy ``.shp``
    with the expected stem is accepted as a fallback with a warning.

    Parameters
    ----------
    site_dir : pathlib.Path
        The site folder (``<root>/<node>/<project>/<site>``).
    variant : str, optional
        Variant descriptor/sensor token (e.g. ``"unbuffered"``,
        ``"HIRES"``). None (default) selects the main plot file.

    Returns
    -------
    pathlib.Path or None
        The selected plot file, or None when it could not be found.
    list of str
        Issues encountered (missing folder, missing mandatory file, ...).
        Empty when a file was found without problems.
    """
    issues: List[str] = []
    layout_dir = plot_layout_dir(site_dir)
    base = site_base_name(site_dir.name)

    if not layout_dir.is_dir():
        issues.append(f"Plot_Layout folder not found: {layout_dir}")
        return None, issues

    if variant is None:
        stems = [f"{base}_plots"]
    else:
        # Highest version wins when versioned copies exist.
        versioned = sorted(
            p.stem for p in layout_dir.glob(f"{base}_plots_{variant}_v[0-9][0-9].geojson"))
        stems = ([versioned[-1]] if versioned else []) + [f"{base}_plots_{variant}"]

    for stem in stems:
        if stem.endswith("_deprecated"):
            continue
        geojson = layout_dir / f"{stem}.geojson"
        if geojson.is_file():
            return geojson, issues
        shp = layout_dir / f"{stem}.shp"
        if shp.is_file():
            warn.warn(
                f"Using legacy shapefile {shp.name} in {layout_dir}; the "
                "Plot_Layout spec requires GeoJSON. Convert the file "
                f"to {stem}.geojson.")
            return shp, issues

    if variant is None:
        issues.append(
            f"Mandatory main plot file '{base}_plots.geojson' not found in "
            f"{layout_dir} (see the Plot_Layout spec: wiki Key-Files).")
    else:
        issues.append(
            f"Plot variant '{variant}' not found in {layout_dir} "
            f"(expected '{base}_plots_{variant}[_vNN].geojson').")
    return None, issues


# ==================================================================================
def load_plot_file(
        plot_path: pathlib.Path,
        trial_info: Optional[pathlib.Path] = None,
    ) -> Tuple[Optional[gpd.GeoDataFrame], List[str]]:
    """Load and validate a plot polygon file.

    Validation per the Plot Delineation protocol: a ``plot_id`` column
    must be present with unique values, and the file must carry a CRS.
    When *trial_info* is provided the trial-information CSV is joined
    onto the plots via ``plot_id`` (left join; unmatched plots keep NaN).

    Parameters
    ----------
    plot_path : pathlib.Path
        Path to the plot GeoJSON/shapefile (from :func:`find_plot_file`).
    trial_info : pathlib.Path, optional
        Path to a ``{YYYYSiteName}_trial_info.csv`` to join on
        ``plot_id``. Default is None (no join).

    Returns
    -------
    geopandas.GeoDataFrame or None
        The validated plot polygons, or None when validation failed.
    list of str
        Validation issues; empty on success.
    """
    issues: List[str] = []
    gdf = gpd.read_file(plot_path)

    if "plot_id" not in gdf.columns:
        issues.append(
            f"Plot file {plot_path} is missing the mandatory 'plot_id' "
            f"column (found: {[c for c in gdf.columns if c != 'geometry']}). "
            "See the Plot Delineation protocol.")
    elif gdf["plot_id"].duplicated().any():
        dups = gdf.loc[gdf["plot_id"].duplicated(), "plot_id"].unique().tolist()
        issues.append(
            f"Plot file {plot_path} has duplicate plot_id values: {dups[:10]}"
            f"{'...' if len(dups) > 10 else ''}.")
    if gdf.crs is None:
        issues.append(f"Plot file {plot_path} has no CRS defined.")

    if issues:
        return None, issues

    if trial_info is not None:
        tdf, join_issues = _load_trial_info(trial_info)
        issues.extend(join_issues)
        if tdf is not None:
            unmatched = set(gdf["plot_id"]) - set(tdf["plot_id"])
            if unmatched:
                issues.append(
                    f"{len(unmatched)} plot_id value(s) in {plot_path.name} have "
                    f"no row in {trial_info.name} (e.g. {sorted(unmatched)[:5]}).")
            gdf = gdf.merge(tdf, on="plot_id", how="left",
                            suffixes=("", "_trial"))

    return gdf, issues


# ==================================================================================
def load_site_plots(
        site_dirs,
        variant: Optional[str] = None,
        join_trial_info: bool = False,
    ) -> dict:
    """Resolve and load the plot file for a set of site folders.

    One :func:`find_plot_file` + :func:`load_plot_file` pass per site,
    with every issue emitted as a warning. Used by the DS03 extraction
    scripts to load each site's plots exactly once per crawl.

    Parameters
    ----------
    site_dirs : iterable of pathlib.Path
        Site folders (``<root>/<node>/<project>/<site>``).
    variant : str, optional
        Plot-file variant selector (see :func:`find_plot_file`).
    join_trial_info : bool, optional
        Join the site's trial-info CSV onto the plots via ``plot_id``.
        Default False.

    Returns
    -------
    dict of pathlib.Path to tuple
        Mapping of site folder to ``(plots GeoDataFrame or None, issues)``.
        The GeoDataFrame carries the source path in ``.attrs['plot_file']``.
    """
    plots: dict = {}
    for site_dir in sorted(set(site_dirs)):
        plot_path, issues = find_plot_file(site_dir, variant=variant)
        if plot_path is None:
            plots[site_dir] = (None, issues)
            for issue in issues:
                warn.warn(f"{site_dir.name}: {issue}")
            continue
        trial = find_trial_info(site_dir) if join_trial_info else None
        if join_trial_info and trial is None:
            issues.append(f"join_trial_info set but no trial-info CSV found "
                          f"for {site_dir.name}.")
        gdf, load_issues = load_plot_file(plot_path, trial_info=trial)
        issues = issues + load_issues
        if gdf is not None:
            gdf.attrs["plot_file"] = plot_path
        plots[site_dir] = (gdf, issues)
        for issue in issues:
            warn.warn(f"{site_dir.name}: {issue}")
    return plots


# ==================================================================================
def find_trial_info(site_dir: pathlib.Path) -> Optional[pathlib.Path]:
    """Locate the current trial-information CSV for a site.

    Parameters
    ----------
    site_dir : pathlib.Path
        The site folder (``<root>/<node>/<project>/<site>``).

    Returns
    -------
    pathlib.Path or None
        ``Documentation/Trial_Info/{YYYYSiteName}_trial_info.csv`` when it
        exists, else None.
    """
    candidate = (site_dir / "Documentation" / "Trial_Info"
                 / f"{site_base_name(site_dir.name)}_trial_info.csv")
    return candidate if candidate.is_file() else None


# ==================================================================================
def _load_trial_info(
        trial_path: pathlib.Path,
    ) -> Tuple[Optional[pd.DataFrame], List[str]]:
    """Load a trial-information CSV and check it can join on ``plot_id``.

    Parameters
    ----------
    trial_path : pathlib.Path
        Path to the trial-info CSV.

    Returns
    -------
    pandas.DataFrame or None
        The trial table, or None when it cannot be used for a join.
    list of str
        Issues; empty on success.
    """
    issues: List[str] = []
    if not trial_path.is_file():
        issues.append(f"Trial-info file not found: {trial_path}")
        return None, issues
    tdf = pd.read_csv(trial_path)
    if "plot_id" not in tdf.columns:
        issues.append(
            f"Trial-info file {trial_path} is missing the mandatory "
            "'plot_id' column; skipping the join.")
        return None, issues
    if tdf["plot_id"].duplicated().any():
        issues.append(
            f"Trial-info file {trial_path} has duplicate plot_id values; "
            "skipping the join.")
        return None, issues
    return tdf, issues
