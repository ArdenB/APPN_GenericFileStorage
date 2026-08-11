"""ELM Validation Panel Extraction.

This script automatically crawls the dataset file structure and looks for the
ELM and validation panel vector files. It then extracts the relevant pixels
from the raster datasets, saving them as a DataFrame.

Panel files must follow the official AerialDataQC naming convention
``QC_{ELM|VAL}[_{TargetIdentifier}]_Panels[_{Extra}].geojson`` (GeoJSON
preferred, shapefile accepted) and live in ``<run>/T1_proc/QC_data/``.
See Protocols/QA/QAprocess/AerialDataQC.md in the
APPN-Field-Protocols-and-Pipelines repo.

Notes
-----
The script expects to be run from within a git repository, or with a
``--path`` argument pointing to the dataset root. When run without
``--no-git``, it will resolve the git root and use that as the search
path.

Command-line Arguments
----------------------
--no-git : flag
    Disable git operations.
--path : str, optional
    The path of the folder to look for QA shapefiles. Defaults to the
    root directory of the git repository.
--save-dir : str, optional
    Also save a copy of each extracted spectra file into this
    directory.  The directory is created if it does not exist.
--load-dir : str, optional
    Load previously extracted spectra files from this folder
    (e.g. data received from other nodes) and append them to the
    QC list for plotting.
"""

# ==============================================================================

__title__ = "ELM validation"
__author__ = "Arden Burrell"
__version__ = "v1.1(11.08.2026)"
__email__ = "arden.burrell@sydney.edu.au"


# ==============================================================================

import os
import re
import sys
import git
from git import exc as git_exc
import argparse
import pathlib
from datetime import datetime
from typing import Dict, List, Any, Tuple, Optional, Union

# Add git root to Python path before other imports
try:
    git_repo = git.Repo(os.getcwd(), search_parent_directories=True)
    git_root = git_repo.git.rev_parse("--show-toplevel")
    if git_root not in sys.path:
        sys.path.insert(0, git_root)
except git_exc.InvalidGitRepositoryError:
    # If not in a git repo, try to find the root by looking for specific markers
    current_path = pathlib.Path(__file__).resolve()
    for parent in [current_path] + list(current_path.parents):
        if (parent / '.git').exists() or (parent / 'Code').exists():
            if str(parent) not in sys.path:
                sys.path.insert(0, str(parent))
            break

import numpy as np
import pandas as pd
import xarray as xr
import rioxarray
import geopandas as gpd
from shapely.geometry import mapping
# import yaml
# import json
from collections import OrderedDict
from tqdm import tqdm
import warnings as warn

# Suppress noisy "invalid value encountered in cast" RuntimeWarning emitted by
# xarray/numpy when rasters contain NaN values that get cast to an integer
# dtype during clipping/extraction. These NaNs are expected (nodata) and the
# warning clutters the tqdm progress output.
warn.filterwarnings(
    "ignore",
    message="invalid value encountered in cast",
    category=RuntimeWarning,
)

import matplotlib.pyplot as plt
import seaborn as sns

# ==================================================================================
def main(        
        args: argparse.Namespace,
        repo: Optional[git.Repo],
        path: pathlib.Path,
    ) -> None:
    """Run the ELM validation panel extraction pipeline.

    Searches the provided path for QC panel shapefiles, then iterates
    over each project to extract and write out the relevant data.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments.
    repo : git.Repo or None
        The git repository object, or ``None`` if git operations are
        disabled via ``--no-git``.
    path : pathlib.Path
        Root directory to search for QC panel shapefiles.

    Returns
    -------
    None
    """
    # ========== Find a list of projects that have shp files ==========
    Panel_files = locate_qc_panels(path)
    QC_list = []
    

    # ========== Find a list of projects that have shp files ==========
    for panel in tqdm(Panel_files, total=len(Panel_files), desc="Processing panels"):
        # print(f"Processing panel: {panel['path']}")
        df_list = extract_panel_spectra(panel, args, path)
        QC_list.extend(df_list)
    
    # ========== Load external spectra from --load-dir if provided ==========
    if args.load_dir is not None:
        external_spectra = load_external_spectra(
            pathlib.Path(args.load_dir), args.type
        )
        QC_list.extend(external_spectra)

    # ========== Save copies to --save-dir if provided ==========
    if args.save_dir is not None:
        save_spectra_copies(QC_list, pathlib.Path(args.save_dir), args.type)

    if not args.skipplot:
        if len(QC_list) > 0:
            plot_panel_spectra(QC_list)
        else:
            print("No spectra data available to plot. Ensure that the panel extraction step produced output files or that valid spectra files were loaded from --load-dir.")

    # TO DO LIST, in order of priority:

    # 3. Add the curve from the panel reflectance file for comparison to the extracted spectra. This will 
    #    allow for a visual comparison of the extracted spectra to the known reflectance values of the panel, 
    #    which can help to identify any issues with the extraction process.

    pass


# ==================================================================================
def plot_panel_spectra(QC_list: List[pd.DataFrame]) -> None:
    """Plot the extracted spectra from the QC panels.

    This function is a placeholder for future implementation. It will take
    the list of DataFrames produced by :func:`extract_panel_spectra` and
    generate visualizations to assess the quality of the extracted spectra.

    Parameters
    ----------
    QC_list : list of pd.DataFrame
        List of DataFrames containing the extracted spectral data for each
        panel and raster.

    Returns
    -------
    None
    """
    # ========== Deal with float vs int for values ==========
    # Normalise all tables to 0-1 float reflectance. Tables with int values
    # (0-10000 scale) are converted by dividing by 10000.0.
    for i, tbl in enumerate(QC_list):
        if np.issubdtype(tbl["value"].dtype, np.integer): # type: ignore
            QC_list[i] = tbl.assign(value=tbl["value"] / 10000.0)

    # ========== Combine the DataFrames in QC_list into a single DataFrame for plotting ==========
    df = pd.concat(QC_list, ignore_index=True)
    df["group"]         = df.groupby(["date", "node", "site", "run", "gpro_nu"]).ngroup()
    df["date_site_run"] = df["date"].astype(str) + " g" + df["group"].astype(str)
    df["residual_per"]  = (df["value"] - (df["Panel_ref"]/100.0) )* 100.0 # to get as a percentage of valid range

    # ========== Create a dict of bands that can be cut ==========
    bad_bands = ({
        "GOBI": {
            "VNIR": np.arange(1, 6).tolist(), 
        },
        "CALVIS": {
            "VNIR": np.arange(1, 6).tolist() + np.arange(170, 173).tolist(),
            "SWIR": np.arange(1, 6).tolist() + np.arange(39, 46).tolist() + np.arange(76, 90).tolist() + np.arange(130, 140).tolist(),
        },
    })
    
    for sensor, sensor_df in df.groupby("sensor"):
        for panel_name, panel_df in sensor_df.groupby("panel_name"):
            for rtype, type_df in panel_df.groupby("EM_Region"):
                # ========== CHECK AND SEE IF THERE ARE ANY BAD BANDS TO CUT FOR THIS SENSOR ==========
                sensor = str(sensor)
                rtype = str(rtype)
                if sensor in bad_bands and rtype in bad_bands[sensor]:
                    cut_bands = bad_bands[sensor][rtype]
                    if args.verbose:
                        print(f"Zeroing bad bands {cut_bands} for sensor: {sensor}, panel: {panel_name}, type: {rtype}")
                    type_df_clipped = type_df.copy()
                    type_df_clipped.loc[type_df_clipped["band"].isin(cut_bands), "value"] = 0


                else:
                    type_df_clipped = None

                for var in ["value", "residual_per"]:
                
                    # +++++ Generate plots for this sensor / panel / type combination +++++
                    print(f"Plotting spectra for sensor: {sensor}, panel: {panel_name}, type: {rtype}")
                    g = sns.relplot(
                        data=type_df,
                        x="band", y=var,
                        col="Panel_ref", # Splits into columns by 'region'
                        hue="date_site_run", # Colors by 'date_site_run'
                        kind="line", # Specifies line plot
                        col_wrap=2, # Wraps columns after 2
                        errorbar="pi"
                    )
                    g.figure.suptitle(f"Sensor: {sensor}, Panel: {panel_name}, EM range: {rtype}", y=0.98, fontweight="bold")
                    g.figure.subplots_adjust(top=0.92)
                    plt.show()

                    # ========== Make a second version with no bad bands ==========
                    if not type_df_clipped is None:
                        g_clipped = sns.relplot(
                            data=type_df_clipped,
                            x="band", y=var,
                            col="Panel_ref", # Splits into columns by 'region'
                            hue="date_site_run", # Colors by 'date_site_run'
                            kind="line", # Specifies line plot
                            col_wrap=2, # Wraps columns after 2
                            errorbar="pi"
                        )
                        g_clipped.figure.suptitle(f"Bad Bands Removed. Sensor: {sensor}, Panel: {panel_name}, EM range: {rtype}", y=0.98, fontweight="bold")
                        g_clipped.figure.subplots_adjust(top=0.92)
                        plt.show()

    # breakpoint()


def save_spectra_copies(
        QC_list: List[pd.DataFrame],
        save_dir: pathlib.Path,
        file_type: str,
    ) -> None:
    """Save a copy of each extracted spectra DataFrame to a central directory.

    Each DataFrame in *QC_list* is written to *save_dir* using a filename
    derived from its metadata columns so that files from different nodes,
    projects, sensors, and dates are uniquely identifiable.

    Parameters
    ----------
    QC_list : list of pd.DataFrame
        DataFrames produced by :func:`extract_panel_spectra` or
        :func:`load_external_spectra`.
    save_dir : pathlib.Path
        Destination directory.  Created (with parents) if it does not
        already exist.
    file_type : str
        ``"csv"`` or ``"parquet"``.

    Returns
    -------
    None
    """
    save_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for df in QC_list:
        if df.empty:
            continue
        # +++++ Build a descriptive filename from the first row's metadata +++++
        row = df.iloc[0]
        parts = []
        for col in ["node", "project", "site", "sensor", "date", "run",
                    "EM_Region", "gpro_nu", "panel_name"]:
            val = row.get(col, "unknown")
            if hasattr(val, "strftime"):
                val = val.strftime("%Y%m%d")
            parts.append(str(val))
        filename = "_".join(parts) + f".{file_type}"

        outpath = save_dir / filename
        if file_type == "csv":
            df.to_csv(outpath.as_posix(), index=False)
        elif file_type == "parquet":
            df.to_parquet(outpath.as_posix(), index=False)
        saved += 1

    print(f"Saved {saved} reflectance QC file(s) to {save_dir}")


def load_external_spectra(
        load_dir: pathlib.Path,
        file_type: str,
    ) -> List[pd.DataFrame]:
    """Load previously extracted spectra from an external directory.

    Searches *load_dir* for files matching *file_type* (``csv`` or
    ``parquet``), loads each into a DataFrame, and performs basic
    structure validation before returning them.

    This is intended for incorporating spectra received from other nodes
    or collaborators into the QC plotting pipeline.

    Parameters
    ----------
    load_dir : pathlib.Path
        Directory to search for spectra files.
    file_type : str
        ``"csv"`` or ``"parquet"``.

    Returns
    -------
    list of pd.DataFrame
        Successfully loaded and validated DataFrames.

    Raises
    ------
    NotADirectoryError
        If *load_dir* does not exist or is not a directory.
    """
    if not load_dir.is_dir():
        raise NotADirectoryError(
            f"The --load-dir path does not exist or is not a directory: {load_dir}"
        )

    files = sorted(load_dir.glob(f"*.{file_type}"))
    if len(files) == 0:
        warn.warn(
            f"No .{file_type} files found in --load-dir {load_dir}. "
            f"Ensure the files match the --type setting (currently '{file_type}')."
        )
        return []

    loaded: List[pd.DataFrame] = []
    skipped = 0
    required_columns = {"band", "value", "Panel_ref", "sensor", "EM_Region"}

    for fpath in tqdm(files, desc="Loading external spectra"):
        try:
            if file_type == "csv":
                df = pd.read_csv(fpath)
            elif file_type == "parquet":
                df = pd.read_parquet(fpath)
            else:
                tqdm.write(f"Unsupported file type '{file_type}' for {fpath}. Skipping.")
                skipped += 1
                continue
        except Exception as er:
            tqdm.write(
                f"Could not read {fpath}: {er}. Skipping file."
            )
            skipped += 1
            continue

        # +++++ Validate required columns +++++
        missing = required_columns - set(df.columns)
        if missing:
            tqdm.write(
                f"File {fpath.name} is missing required columns {missing}. Skipping."
            )
            skipped += 1
            continue

        # +++++ Make sure the dataframe has rows +++++
        if df.empty:
            tqdm.write(f"File {fpath.name} has no rows. Skipping.")
            skipped += 1
            continue

        loaded.append(df)

    print(
        f"Loaded {len(loaded)} spectra file(s) from {load_dir}"
        + (f" ({skipped} skipped)" if skipped else "")
    )
    return loaded


def extract_panel_spectra(
        panel: Dict[str, Any],
        args: argparse.Namespace,
        root_path: pathlib.Path,
    ) -> List[pd.DataFrame]:
    """Extract spectral data for a given panel from associated rasters.

    Reads the panel shapefile, then iterates over each raster entry in
    ``panel["rasters"]``.  If the output file does not already exist
    (or ``--force`` is set), the raster is clipped and written to disk
    via :func:`_process_raster`.  Each output file is then loaded and
    returned as a list of DataFrames.

    Parameters
    ----------
    panel : dict
        Dictionary produced by :func:`locate_qc_panels` containing
        panel metadata and raster information.
    args : argparse.Namespace
        Parsed command-line arguments.  Relevant flags:

        - ``args.force`` -- re-create output files even if they exist.
        - ``args.type``  -- ``"csv"`` or ``"parquet"``.

    Returns
    -------
    list of pd.DataFrame
        One DataFrame per successfully loaded raster output file.
    """
    # ========== Load the shape files ==========
    
    shpdf     = gpd.read_file(panel["path"])
    # ========== Validate the vector file structure ==========
    expected_columns = ["geometry", "Panel_ref"]
    if not all(col in shpdf.columns for col in expected_columns):
        raise ValueError(
            f"QC panel file {panel['path']} does not have the expected columns "
            f"{expected_columns}. Found columns: {list(shpdf.columns)}. "
            "Fix the file to match the AerialDataQC protocol before re-running.")

    if shpdf.crs is None:
        raise ValueError(f"Shapefile {panel['path']} does not have a CRS defined. Please set a CRS on the shapefile before running this script.")

    QC_tables: List[pd.DataFrame] = []

    # ========== Open dataset(s) ==========
    for ras in panel["rasters"].values():
        # +++++ Check if the output file already exists and skip if it does +++++
        if args.skip_processing:
            if not ras["exists"]:
                tqdm.write(f"Skipping raster {ras['InputRaster']} (no existing output file). Use without --skip-processing to generate it.")
                continue
        elif not ras["exists"] or args.force:
            # In the future this will open the file
            try:
                _process_raster(ras, shpdf, panel, root_path, keep_xy=args.keep_xy)
            except Exception as er:
                tqdm.write(f"Error processing raster {ras['InputRaster']}: {er}. Skipping raster.")
                continue

        # ========== Load the data ==========
        try:
            if args.type == "csv":
                df = pd.read_csv(ras["outfile"])
            elif args.type == "parquet":
                df = pd.read_parquet(ras["outfile"])
        
        except Exception as er: 
            warn.warn(f"Could not read output file {ras['outfile']} for raster {ras['InputRaster']}. Error: {er}. May require manual inspection of the file and raster. Skipping file.")
            continue
        
        df, check = _check_table_structure(panel, ras, df, args)
        if check:
            QC_tables.append(df)
        else:
            warn.warn(f"DataFrame for raster {ras['InputRaster']} does not meet QC requirements.")
            continue
    return QC_tables


def _check_table_structure(
        panel: Dict[str, Any],
        ras: Dict[str, Any],
        df: pd.DataFrame,
        args: argparse.Namespace,
    ) -> Tuple[pd.DataFrame, bool]:
    """Check if the DataFrame has the expected structure for QC tables.

    Parameters
    ----------
    panel : dict
        Panel metadata dictionary (from :func:`locate_qc_panels`).
    ras : dict
        Raster entry for the table being checked.
    df : pd.DataFrame
        The DataFrame to check.
    args : argparse.Namespace
        Parsed command-line arguments (uses ``args.no_radiance_check``).

    Returns
    -------
    pd.DataFrame
        The (unmodified) DataFrame.
    bool
        True if the DataFrame has the expected structure, False otherwise.
    """
    valid = True
    # ========== Require the standard output columns ==========
    required_columns = {"band", "value", "Panel_ref", "sensor", "EM_Region", "gpro_nu"}
    missing = required_columns - set(df.columns)
    if missing:
        warn.warn(
            f"Output file {ras['outfile']} is missing required columns {missing}. "
            "Re-run with --force to regenerate it with the current schema.")
        return df, False

    # ========== Check if the Dataframe has values in the expected ranges ==========
    # This is a check for reflectance vs radiance
    if panel["sensor"] in ["GOBI", "CALVIS"] and not args.no_radiance_check:
        if df["value"].dtype == int:
            if df["value"].max() < 100:
                warn.warn(f"Maximum value in DataFrame for raster {ras['InputRaster']} is less than 100. This may indicate that the values are in reflectance rather than radiance, which is unexpected for this sensor. Please check the processing step and ensure that the correct values are being extracted. Skipping file.")
                valid = False
        elif df["value"].dtype == float:
            if df["value"].max() > 13.:
                warn.warn(f"Maximum value in DataFrame for raster {ras['InputRaster']} is greater than 13. This may indicate that the values are in reflectance rather than radiance, which is unexpected for this sensor. Please check the processing step and ensure that the correct values are being extracted. Skipping file.")
                # breakpoint()
                valid = False
    return df, valid


def _process_raster(
        ras: Dict[str, Any],
        shpdf: gpd.GeoDataFrame,
        panel: Dict[str, Any],
        root_path: pathlib.Path,
        keep_xy: bool = False,
    ) -> None:
    """Clip a raster to the panel geometries and save the result.

    Opens the raster referenced in *ras*, clips it to the geometries in
    *shpdf*, converts the clipped data to a :class:`~geopandas.GeoDataFrame`,
    attaches panel metadata columns, and writes the result to
    ``ras["outfile"]``.

    Parameters
    ----------
    ras : dict
        Raster entry produced by :func:`locate_qc_panels` with keys
        ``"InputRaster"`` (*pathlib.Path*), ``"outfile"``
        (*pathlib.Path*), ``"type"`` (*str*), and ``"exists"``
        (*bool*).
    shpdf : geopandas.GeoDataFrame
        Panel shapefile geometries used to clip the raster.
    panel : dict
        Panel metadata dictionary (from :func:`locate_qc_panels`).
        Values for keys ``"node"``, ``"project"``, ``"site"``,
        ``"sensor"``, ``"date"``, ``"run"``, and ``"panel_name"``
        are written as columns in the output file.
    root_path : pathlib.Path
        Repository/data root used for display-friendly relative paths.
    keep_xy : bool, optional
        If True, retain the per-pixel ``x`` and ``y`` coordinate columns
        (in the raster's native CRS) in the output table instead of
        dropping them during the spatial join. Default is False.

    Returns
    -------
    None

    Raises
    ------
    ValueError
        If the raster does not have a CRS defined.
    """
    # +++++ open the raster dataset +++++
    _ras_display = pathlib.Path(ras["InputRaster"]).relative_to(root_path)
    tqdm.write(f"Processing raster: {_ras_display} started at {pd.Timestamp.now()}. This may take a while.")
    ds  = rioxarray.open_rasterio(ras["InputRaster"])
    crs = ds.rio.crs # type: ignore
    if crs is None:
        raise ValueError(f"Raster {ras['InputRaster']} does not have a CRS defined. Please check the raster file and ensure it has a defined CRS.")
    ds_clipped = ds.rio.clip(shpdf.geometry.apply(mapping), shpdf.crs, drop=True) # type: ignore

    # +++++ Convert to DataFrame, handling multi-band data +++++
    df_xr      = ds_clipped.to_dataframe(name="value").reset_index()
    # df_xr      = df_xr.dropna()

    points_gdf = gpd.GeoDataFrame(
            df_xr, geometry=gpd.points_from_xy(df_xr['x'], df_xr['y']),
            crs=crs)
    _drop_cols = ['geometry', 'spatial_ref', 'index_right']
    if not keep_xy:
        _drop_cols += ['y', 'x']
    gdf = gpd.sjoin(points_gdf, shpdf.to_crs(crs), how='inner', predicate='within').drop(columns=_drop_cols)
    # print(f"Processing raster: {ras['InputRaster']}")
    for vname in ["node", "project", "site", "sensor", "date", "run", "panel_name"]:
        # Skip if the value is not in the panel dict for some reason
        if vname in panel:
            gdf[vname] = panel[vname]
    # Add the EM range type and gpro number from the raster dict to the DataFrame
    gdf["EM_Region"] = ras["type"]
    gdf["gpro_nu"]   = ras["gpro_nu"] # this only matter if there a multiple gpros
    # +++++ Add a boolean QC check for the expected +++++
    # check if value column is int or float
    if pd.api.types.is_integer_dtype(gdf["value"]):
        gdf["Valid_Range"] = (gdf["value"] > 0) & (gdf["value"] <= 10000)
    elif pd.api.types.is_float_dtype(gdf["value"]):
        gdf["Valid_Range"] = (gdf["value"] > 0) & (gdf["value"] <= 1.0)
    else:
        gdf["Valid_Range"] = False

    # ========= Save the DataFrame to file ==========
    if args.type == "csv":
        gdf.to_csv(ras["outfile"].as_posix(), index=False)
    elif args.type == "parquet":
        gdf.to_parquet(ras["outfile"].as_posix(), index=False)


def locate_qc_panels(
        path: pathlib.Path,
        valid_sensors: List[str] = ["GOBI", "CALVIS"],
        # data_format: str = "csv",
    ) -> List[Dict[str, Any]]:
    """Find spectral validation panel vector files in the given directory tree.

    Recursively searches ``path`` for QC panel files following the official
    AerialDataQC naming convention
    ``QC_{ELM|VAL}[_{TargetIdentifier}]_Panels[_{Extra}].geojson`` (GeoJSON
    preferred, shapefile accepted) stored under ``<run>/T1_proc/QC_data/``,
    and returns a list of dictionaries containing panel metadata and
    associated raster information.

    Parameters
    ----------
    path : pathlib.Path
        Root directory to search recursively.
    valid_sensors : list of str, optional
        Sensor names used to filter results. The sensor name is expected
        to be the name of the parent directory at a specific level in
        the path. Defaults to ``["GOBI", "CALVIS"]``.

    Returns
    -------
    list of dict
        Each dictionary contains the following keys:

        - **path** (*pathlib.Path*) -- Path to the panel shapefile.
        - **project** (*str*) -- Name of the project directory.
        - **sensor** (*str*) -- Name of the sensor directory.
        - **site** (*str*) -- Name of the site directory.
        - **panel_name** (*str*) -- Stem of the panel shapefile.
        - **outdir** (*pathlib.Path*) -- Output directory for spectral tables.
        - **rasters** (*dict*) -- Mapping of raster names to dicts with
          keys ``"InputRaster"``, ``"outfile"``, ``"type"``, and
          ``"exists"``.

    Raises
    ------
    ValueError
        If no QC panel shapefiles are found in ``path``.
    NotImplementedError
        If a sensor name is not handled by the current implementation.
    """
    # +++++ Find all the panel files (geojson preferred, shp accepted) +++++
    print(f"Scanning directory for panel files and rasters. {pd.Timestamp.now()}")

    # Official pattern: QC_{TargetType}[_{TargetIdentifier}]_Panels[_{Extra}]
    # (AerialDataQC protocol); only ELM and VAL target types carry panels.
    name_re       = re.compile(r"^QC_(ELM|VAL)(_.+)?_Panels(_.+)?$")
    shp_files     = [f for f in path.rglob("QC_*.shp") if name_re.match(f.stem)]
    geojson_files = [f for f in path.rglob("QC_*.geojson") if name_re.match(f.stem)]

    # +++++ Prefer geojson when both share the same parent dir + stem +++++
    geojson_keys = {(f.parent, f.stem) for f in geojson_files}
    shp_files    = [f for f in shp_files if (f.parent, f.stem) not in geojson_keys]
    files        = sorted(geojson_files + shp_files)

    if len(files) == 0:
        raise ValueError(
            f"No QC panel files found in {path}. Please check the path and file "
            "naming conventions. Expected pattern: "
            "QC_{ELM|VAL}[_{TargetIdentifier}]_Panels[_{Extra}].geojson (or .shp) "
            "under <run>/T1_proc/QC_data/ (see the AerialDataQC protocol).")

    # ========== Filter out excluded directories ==========
    if args.exclude_dir:
        exclude_set = set(args.exclude_dir)
        before = len(files)
        files = [f for f in files if not (set(p.name for p in f.parents) & exclude_set)]
        if args.verbose and len(files) < before:
            print(f"Excluded {before - len(files)} panel file(s) matching --exclude-dir {args.exclude_dir}")

    pan_list    = [] # List of dicts with information about the panel files
    # ========== loop over each project and write out files ==========
    for panel in files:
        # ========== Enforce the official storage location: <run>/T1_proc/QC_data/ ==========
        if panel.parent.name != "QC_data" or panel.parents[1].name != "T1_proc":
            if args.verbose:
                tqdm.write(f"Skipping {panel}: not under T1_proc/QC_data (see the AerialDataQC protocol).")
            continue
        # ========== Check if the sensor is valid ==========
        sensor = panel.parents[4].name
        if not sensor in valid_sensors:
            if args.verbose:
                warn.warn(f"Found panel file for sensor {sensor} which is not in the list of valid sensors {valid_sensors}. Skipping file: {panel}")
            # breakpoint()
            continue
        # ========== Make a dict of information ==========
        p_dict = ({
            "path": panel,
            "panel_name": panel.stem, 
            "outdir": panel.parents[0] / "QC_Spectral_Tables"
        })
        # +++++ Use the APPN dataset structure to extract the relevant information from the path +++++
        for key, idx in zip(["node", "project", "site", "sensor", "date", "run"], np.arange(7,1, -1)):
            # print(f"Checking {key} at index {idx}; path part: {panel.parents[idx].name}") # type: ignore
            val = panel.parents[idx].name # type: ignore
            if key == "date":
                val = pd.to_datetime(val, format="%Y%m%d", errors="coerce")
            p_dict[key] = val

        
        # ========= Make the output directory if it doesn't exist ==========
        p_dict["outdir"].mkdir(parents=False, exist_ok=True)

        # ========== Locate the raster data ==========
        rasters = ({})

        # +++++ Define which ortho types to search for, per sensor +++++
        if sensor in ["GOBI", "CALVIS"]:
            ortho_types = ["VNIR"]
            if sensor == "CALVIS":
                ortho_types.append("SWIR")
        else:
            raise NotImplementedError(f"Sensor {sensor} is not implemented. Valid sensors are {valid_sensors}. Please check the sensor name in the path and the list of valid sensors.")

        skip_panel = False
        for otype in ortho_types:
            orthos = list(panel.parents[1].glob(f"*.gpro/products/*_{otype}_Orthomosaic.bin"))
            if len(orthos) == 0:
                if args.verbose:
                    tqdm.write(f"No {otype} orthomosaic found for panel {panel}. Expected to find a file matching *.gpro/products/*_{otype}_Orthomosaic.bin in {panel.parents[1]}. Skipping {otype}.")
                skip_panel = True
                break
            elif len(orthos) > 1:
                if args.verbose:
                    tqdm.write(f"Multiple {otype} orthomosaics found for panel {panel}. Expected to find only one file matching *.gpro/products/*_{otype}_Orthomosaic.bin in {panel.parents[1]}. {orthos}")
            for nu, ortho in enumerate(orthos):
                name    = f"{otype}{nu}_{panel.stem}_{ortho.stem}"
                outfile = p_dict["outdir"] / f"{name}.{args.type}"
                rasters[name] = ({
                    "InputRaster": ortho,
                    "outfile": outfile,
                    "type": otype,
                    "exists": outfile.is_file(),
                    "gpro_nu": nu,
                })
        if skip_panel:
            continue
        
        # ========= Check if rasters is empty ==========
        if not rasters:
            if args.verbose:
                tqdm.write(f"No rasters found for panel {panel}. Skipping panel.")
            continue

        # ========== Add the rasters to the dict ==========
        p_dict["rasters"] = rasters
        pan_list.append(p_dict)
    return pan_list


# ==================================================================================
if __name__ == '__main__':
    # ========== Set the args Description ==========
    description='Optional Command line arguments for script'
    parser = argparse.ArgumentParser(description=description)

    # ========== Add the command line arguments ==========   
    parser = argparse.ArgumentParser(description="Generate dataset folder structure.")
    parser.add_argument("--path", type=str, default=None, help="The path of the folder to look for QA shape files. By default it will search from the root dir of the git repo")
    parser.add_argument("-f","--force", default=False, action="store_true", help="Force the creation of files even if one already exists. Default is to skip creating files that already exist to prevent overwriting.")
    parser.add_argument("--type", type=str, default="parquet", help="The file type for the output files. Default is parquet for more efficient storage and faster read/write times, but can be set to csv. Note that .parquet files will require additional dependencies to read and write.")
    parser.add_argument("-s","--skipplot", default=False, action="store_true", help="Skip plotting and only produce the extracted data tables. Default is to generate plots.")
    parser.add_argument("--skip-processing", default=False, action="store_true", help="Skip raster processing and only load existing output files for plotting. Useful for faster re-runs when output files already exist.")
    parser.add_argument("--save-dir", type=str, default=None, help="Also save a copy of each extracted spectra file into this directory. Creates the directory if it doesn't exist. Useful for sharing extracted spectra with collaborators or keeping a central record.")
    parser.add_argument("--load-dir", type=str, default=None, help="Load previously extracted spectra files from this folder (e.g. data received from other nodes). Loaded files are appended to the QC list for plotting and can also be copied via --save-dir.")
    parser.add_argument("-v", "--verbose", default=False, action="store_true", help="Enable verbose output for debugging and additional information during processing.")
    parser.add_argument("--no-radiance-check", default=False, action="store_true", help="Disable the reflectance vs radiance range check (value < 100). Useful when processing data known to be in reflectance units.")
    parser.add_argument("--exclude-dir", type=str, nargs="+", default=[], help="One or more directory names to exclude from the panel search. Any panel whose path contains a directory matching one of these names will be skipped. e.g. --exclude-dir 2025_TestData 2026_TestData")
    parser.add_argument("--keep-xy", default=False, action="store_true", help="Retain the per-pixel x and y coordinate columns (in the raster's native CRS) in the extracted spectra tables. By default these are dropped during the spatial join.")
    
    args = parser.parse_args()

    # +++++ Check the paths and set exc path to the root of the git folder +++++
    # If --path is provided, use it regardless of git repo status
    if args.path is not None:
        git_root = args.path
        # Try to check if the provided path is within a git repo
        try:
            repo = git.Repo(git_root, search_parent_directories=True)
        except git_exc.InvalidGitRepositoryError:
            repo = None
    else:
        # Otherwise, try to find the git root
        path = os.getcwd()
        try:
            git_repo = git.Repo(path, search_parent_directories=True)
            git_root = git_repo.git.rev_parse("--show-toplevel")
            repo = git.Repo(git_root)
        except git_exc.InvalidGitRepositoryError as err:
            raise git_exc.InvalidGitRepositoryError(
                f"This script was called from an unknown path ({path}). Must be in a git repo or provide a valid --path argument. Original error: {err}"
            ) from err
    
    # ========= Check if the provided path exists ==========
    sys.path.append(git_root)
    os.chdir(git_root)
    path = pathlib.Path(git_root)
    
    if not path.is_dir():
        raise NotADirectoryError(f"The provided path does not exist: {path}")



    # ========== Parse Args to main function ==========
    main(args, repo, path)