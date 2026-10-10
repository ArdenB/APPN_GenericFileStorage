"""
Py script to automatically generate a folder structure
"""

# ==============================================================================

__title__ = "Dataset File Structure"
__author__ = "Arden Burrell"
__version__ = "v1.0(22.05.2025)"
__email__ = "arden.burrell@sydney.edu.au"


# ==============================================================================

import os
import sys
import git
import argparse
import pathlib
import time
from git import exc as git_exc
from collections import OrderedDict

# ==============================================================================
# ========== Import packages ==========
import numpy as np
import pandas as pd
import yaml
# import ipdb

# ==============================================================================

def main(args, repo):

    # ========== pull the repo ==========
    if not args.no_git:
        GitPull(repo)
    # ========== set Flag to determine if the git repo has been modified ==========
    gitmod = False

    # ========== make and check the node folders ==========
    # +++++ Load the node yaml file +++++
    nodeinfo = yaml.safe_load(open(f"{args.projectsYAML}", "r"))
    # +++++ Loop over each node +++++
    for node in nodeinfo["nodes"]:
        # +++++ Check the folders and the project log +++++
        df, gitmod  = NodeChecker(args, node, gitmod, repo)

        # ========== Make the column names for the csv file ==========
        colnames = ["Year", "Month", "Day", "Sensor", "Technician", "Runs", "Site", "MakeNotesFile", "MakeTableFile", "CheckSum"]

        # ========== Check if there are any project ==========
        if df.empty:
            continue # Skip this node as there are no projects to make files for

        # ========== Track whether the project summary CSV needs re-saving ==========
        pfilename = f"./{node['name']}/{node['name']}_ProjectsSummary.csv"
        psum_modified = False

        # ========== Loop over every project ==========
        for project, row in df.iterrows():

            # ========== Check the project info and make folders ==========
            df_flog, gitmod, flog_fname, Site_names, ProjectInfo, = projBuilder(project, node, colnames, args, repo, gitmod)

            # ========== Check Has any valid field entries ==========
            if not  df_flog.empty:
                # +++++ Loop over each experimental day +++++
                for index, frow in df_flog.iterrows():
                    # +++++ Optionally auto-enable a sensor in the project summary +++++
                    if (args.enable_sensors
                            and isinstance(frow.Sensor, str)
                            and frow.Sensor in row.index
                            and row[frow.Sensor] != True):
                        print(f"--enable-sensors: setting {project}/{frow.Sensor} = TRUE in {pfilename}")
                        row[frow.Sensor] = True
                        df.loc[project, frow.Sensor] = True
                        psum_modified = True

                    # +++++ Sanity check the row +++++
                    check, site = Rowchecker(flog_fname, frow, row, ProjectInfo, args.historical,
                                             update_checksums=args.update_checksums)

					# +++++ Make the site name +++++
                    df_flog, gitmod =  Sitebuilder(flog_fname, df_flog, index, frow, check, site, project, node, args, repo, gitmod)

                # +++++ Sync the per-site sensor lists in the project yaml +++++
                gitmod = _syncSiteSensors(df_flog, ProjectInfo, project, node, args, repo, gitmod)

        # ========== Persist any sensor changes back to the project summary CSV ==========
        if psum_modified:
            df.to_csv(pfilename)
            print(f"Updated project summary written to {pfilename}")
            if not args.no_git and repo is not None:
                _gitAddRetry(repo, pfilename)
                gitmod = True


    # ========== Do a git commit ==========
    if gitmod:
        if not args.no_git:
            print(f"Code Sucessfull. New files and folders created. Starting git push at {pd.Timestamp.now()}")
            repo.index.commit(f'Commit from python script {__file__}. ')
            repo.git.push()
        else:
            print(f"Code Sucessfull. Git Disabled with command line arguments")
        # breakpoint()
    else:
        print("Code Sucessfull. No new files created")
    
    # breakpoint()

#==============================================================================
def Sitebuilder(flog_fname, df_flog, index, frow, check, site, project, node, args, repo, gitmod):
    """
    Create and organize the folder structure for a site, including sensor and run folders.

    Parameters
    ----------
	flog_fname : str
		Path to the field log CSV file.
    df_flog : pandas.DataFrame
		DataFrame containing the field log information.
    index : int
		Index of the row in the field log DataFrame to update.
    frow : pandas.Series
        Row from the field log DataFrame containing field day information.
    check : float or None
        Checksum value to update in the field log, or None if not needed.
    site : dict
        Dictionary containing site information.
    project : str
        Name of the project.
    node : dict
        Dictionary containing node information.
    args : argparse.Namespace
        Parsed command-line arguments.
    repo : git.Repo
        GitPython Repo object for git operations.
    gitmod : bool
        Flag indicating if the git repo has been modified.

    Returns
    -------
    df_flog : pandas.DataFrame
		Updated field log DataFrame with the checksum.
    gitmod : bool
        Updated git modification flag.

    Notes
    -----
    - Creates sensor and run folders for the site.
    - Optionally creates a FieldNotes.txt file.
    - Updates the field log and adds it to git if required.
    """
    # ========== Make the site name ==========
    sitename = _sitenamemaker(site)
    
	# +++++ Check if there is already a sensor folder +++++
    pymkdir(f"./{node["name"]}/{project}/{sitename}/{frow.Sensor}")
    # NOTE: the per-site sensors list in ProjectSummary.yaml is rebuilt from
    # the field log by _syncSiteSensors() after all rows are processed.
    
	# +++++ Make the field day folder +++++
    dname    = f"{frow.Year:02d}{frow.Month:02d}{frow.Day:02d}"
    folder  = f"./{node["name"]}/{project}/{sitename}/{frow.Sensor}/{dname}"
    for runNo in np.arange(frow.Runs):
        for fldr in ["T0_raw", "T1_proc", "T2_traits"]:
            pymkdir(f"{folder}/run_{runNo:02d}/{fldr}")

        sensors_QC = ["GOBI", "CALVIS"] # Add more sensors with QC data here
        if frow.Sensor in sensors_QC:
            pymkdir(f"{folder}/run_{runNo:02d}/T1_proc/QC_data")
        # +++++ make a Vault folder +++++
        # sensors with a Vault folder in T0_raw. Files placed inside Vault/
        # are excluded from any future programmatic file cleanup/deletion.
        sensors_vault = ["GOBI", "CALVIS"] # Add more sensors with a Vault folder here
        if frow.Sensor in sensors_vault:
            pymkdir(f"{folder}/run_{runNo:02d}/T0_raw/Vault")
    
	# +++++ Make a log file +++++
    # Default behaviour: create the notes file unless MakeNotesFile is explicitly False.
    # Treat True, empty/blank, NaN, or missing values as True.
    make_notes = frow.MakeNotesFile
    if isinstance(make_notes, str):
        make_notes_str = make_notes.strip().lower()
        skip_notes = make_notes_str in ("false", "f", "0", "no", "n")
    elif pd.isna(make_notes):
        skip_notes = False
    else:
        skip_notes = (make_notes is False) or (make_notes == 0)
    make_notes_resolved = not skip_notes
    if make_notes_resolved:
        pathlib.Path(f"{folder}/FieldNotes.txt").touch()

    # +++++ Make a run overview table +++++
    # Same default behaviour as MakeNotesFile: create unless explicitly False.
    make_table = frow.MakeTableFile
    if isinstance(make_table, str):
        make_table_str = make_table.strip().lower()
        skip_table = make_table_str in ("false", "f", "0", "no", "n")
    elif pd.isna(make_table):
        skip_table = False
    else:
        skip_table = (make_table is False) or (make_table == 0)
    make_table_resolved = not skip_table
    if make_table_resolved:
        run_overview_fname = f"{folder}/RunOverview.csv"
        # Don't overwrite existing tables; users may have added extra columns.
        if not os.path.isfile(run_overview_fname):
            run_names = [f"run_{runNo:02d}" for runNo in np.arange(frow.Runs)]
            # Per-run status trigger bools:
            #   RunFailed  — no recoverable data for the entire run
            #   Issues     — something went wrong but recoverable data exists
            #                (partial payload loss, QC failures, degraded quality)
            #   Deviations — no fault, but the capture plan deviated from the
            #                standard full capture (e.g. intended RGB-only run)
            run_cols: dict[str, list] = {
                "RunFailed": [False] * len(run_names),
                "Issues": [False] * len(run_names),
                "Deviations": [False] * len(run_names),
            }
            # FIELDOBS runs need an extra column to record the type of field data captured.
            if frow.Sensor == "FIELDOBS":
                run_cols["FieldDataType"] = [""] * len(run_names)
            df_runs = pd.DataFrame(run_cols, index=run_names)
            df_runs.index.name = "Run"
            df_runs.to_csv(run_overview_fname)
            if not args.no_git:
                _gitAddRetry(repo, run_overview_fname)
                gitmod = True
        else:
            # Propagate newly specced status columns to existing tables,
            # never touching existing values or user-added columns.
            df_runs = pd.read_csv(run_overview_fname, index_col="Run")
            status_cols = ["RunFailed", "Issues", "Deviations"]
            missing_cols = [c for c in status_cols if c not in df_runs.columns]
            if missing_cols:
                for col in missing_cols:
                    df_runs[col] = False
                df_runs.to_csv(run_overview_fname)
                print(f"Added status column(s) {missing_cols} to {run_overview_fname}")
                if not args.no_git:
                    _gitAddRetry(repo, run_overview_fname)
                    gitmod = True

    # +++++ Write the resolved True/False back into the field log +++++
    # Compare against the existing values to detect whether the CSV needs updating.
    flag_updates = {
        "MakeNotesFile": make_notes_resolved,
        "MakeTableFile": make_table_resolved,
    }
    needs_update = False
    for col, resolved in flag_updates.items():
        current_val = df_flog.at[index, col]
        if (
            pd.isna(current_val)
            or not isinstance(current_val, bool)
            or current_val != resolved
        ):
            # Ensure the column can hold bool values (avoids FutureWarning when
            # the column was inferred as float64 due to NaNs in the CSV).
            if df_flog[col].dtype != object:
                df_flog[col] = df_flog[col].astype(object)
            df_flog.at[index, col] = resolved
            needs_update = True
    if needs_update:
        # Recompute checksum since the resolved flags are part of the hashed columns.
        updated_row = df_flog.loc[index].drop("CheckSum")
        check = float(pd.util.hash_pandas_object(updated_row).sum() % 100000000)
    
    # ========== Add the Check Sum and add the file to git ==========
    if not check is None:
        df_flog.loc[index,"CheckSum"] = check
        df_flog.to_csv(flog_fname, index=False)
        # +++++ Add the file to the github repo +++++ 
        if not args.no_git:
            _gitAddRetry(repo, flog_fname)
            gitmod = True
    return df_flog, gitmod

def projBuilder(project, node, colnames, args, repo, gitmod):
    """
    Load project info and ensure required folders and files exist.

    Parameters
    ----------
    project : str
        The project name.
    node : dict
        Dictionary containing node information (e.g., name).
    colnames : list of str
        List of column names for the field log CSV.
    args : argparse.Namespace
        Parsed command-line arguments.
    repo : git.Repo
        GitPython Repo object for git operations.
    gitmod : bool
        Flag indicating if the git repo has been modified.

    Returns
    -------
    df_flog : pandas.DataFrame
        The loaded or newly created field log DataFrame.
    gitmod : bool
        Updated git modification flag.
    flog_fname : str
        Path to the field log CSV file.
    Site_names : list of str
        List of standardized site names.
    ProjectInfo : dict
        Loaded project YAML data.

    Notes
    -----
    - Creates 'Documentation' and 'Code' folders if missing.
    - Creates a project YAML summary if missing.
    - Creates a field log CSV if missing.
    - Adds new files to git if enabled.
    """
    # ========== Check the folders exist ==========
    for fld in ["Documentation", "Code"]:
        pymkdir(f"./{node["name"]}/{project}/{fld}")
    
    # ========== Load a project yaml ==========
    psyl_fname = f"./{node["name"]}/{project}/ProjectSummary.yaml"
    ProjectInfo, gitmod = _projYAML(project, psyl_fname, args, repo, gitmod)

    # Ensure ProjectInfo is treated as a dictionary
    assert isinstance(ProjectInfo, dict), f"ProjectInfo must be a dictionary, got {type(ProjectInfo)}"


    # ========== Check the field log ==========
    flog_fname = f"./{node["name"]}/{project}/FieldLog.csv"
    if not os.path.isfile(flog_fname):
        # +++++ Create and empty field log +++++
        df_flogO = pd.DataFrame(columns=colnames)
        df_flogO.to_csv(flog_fname, index=False)

        # ========== Add the file to the github repo ========== 
        if not args.no_git:
            _gitAddRetry(repo, flog_fname)
            gitmod = True
    
        # +++++ Open the log files +++++
        df_flog = pd.read_csv(flog_fname)
    else:
        # +++++ Load the field log file +++++
        df_flog = pd.read_csv(flog_fname)
        # +++++ Sanity check the file +++++
        df_flog, gitmod = _df_col_check(df_flog, flog_fname, colnames, args, repo, gitmod)

    # ========== Make sure folders exist of all of the sites ==========
    Site_names = []
    project_data = ProjectInfo.get("project", {})
    sites = project_data.get("sites", []) # type: ignore

    for site in sites:
        # Add validation for site structure
        if (isinstance(site, dict) and
            site.get("name", "") != "" and
            site.get("year", -9999) != -9999):
            # ========== To Do Sanity check a log file ==========
            # This will also need to include some ability to edit files
            # breakpoint()
            sitename = _sitenamemaker(site, psyl_fname)
            Site_names.append(sitename)

            # +++++ Check if the site folder exists +++++
            site_root = f"./{node['name']}/{project}/{sitename}"
            pymkdir(site_root)
            pymkdir(f"{site_root}/Code")
            # +++++ Site-level Documentation/ now has two protocol-defined subfolders +++++
            # See DataFolderStructure.md and Plot_Delineation.md for the spec.
            pymkdir(f"{site_root}/Documentation")
            # The YYYYSiteName key used in plot/trial filenames is the site
            # folder name without the _F / _C controlled-environment suffix.
            yyyysite = f"{site['year']}{site['name']}"
            for sub in ("Plot_Layout", "Trial_Info"):
                doc_sub = f"{site_root}/Documentation/{sub}"
                pymkdir(doc_sub)
                gitmod = _seedDocReadme(doc_sub, sub, yyyysite, args, repo, gitmod)

    return df_flog, gitmod, flog_fname, Site_names, ProjectInfo, 


def NodeChecker(args, node, gitmod, repo):
    """
    Ensure node folder and project summary CSV exist, and check for missing columns.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments.
    node : dict
        Dictionary containing node information (e.g., name, SensorPlatforms).
    gitmod : bool
        Flag indicating if the git repo has been modified.
    repo : git.Repo or None
        GitPython Repo object for git operations, or None when --no-git is set.

    Returns
    -------
    df : pandas.DataFrame
        DataFrame of the node's project summary.
    gitmod : bool
        Updated git modification flag.

    Notes
    -----
    - Creates the node folder and project summary CSV if missing.
    - Adds new files to git if enabled.
    - Ensures all required columns are present in the CSV.
    """
    # +++++ Check if the folder exist already +++++
    pymkdir(f"./{node["name"]}")

    # +++++ Check if project csv already exists +++++
    # This file is a bool array of projects vs sensors
    pfilename = f"./{node["name"]}/{node["name"]}_ProjectsSummary.csv"

    # ========= Check if the file already exists and has the correct columns =========
    if not os.path.isfile(pfilename):
        # +++++ Create and empty field log +++++
        df_proj = pd.DataFrame(columns=["Project"]+node["SensorPlatforms"])
        df_proj.to_csv(pfilename, index=False)
        print(f"New Node Project Summary table built:{pfilename}")


        # ========== Add the file to the github repo ========== 
        if not args.no_git and repo is not None:
            _gitAddRetry(repo, pfilename)
            gitmod = True

    # ========== Load the file with projects and fix any missing columns ==========
    # Always read in the csv for consistency check to avoid read issues
    df = pd.read_csv(pfilename, header=0, index_col=0)
    df, gitmod = _df_col_check(df, pfilename, node["SensorPlatforms"], args, repo, gitmod, fill_val=False)

    # ========== Check for project folders that exist but are missing from CSV ==========
    node_path = f"./{node["name"]}"
    if os.path.exists(node_path):
        # Get all directories in the node folder
        all_items = os.listdir(node_path)
        project_folders = []

        # Filter for project folders (start with year pattern like 2025_, 2026_)
        # Exclude non-project items
        exclude_items = ['Documents', 'Code', 'sync.ffs_db', '.DS_Store']
        for item in all_items:
            item_path = os.path.join(node_path, item)
            # Check if it's a directory and matches project naming pattern (starts with year)
            if os.path.isdir(item_path) and not item in exclude_items:
                # Check if folder name starts with a 4-digit year (20XX_)
                if len(item) > 4 and item[:4].isdigit() and item[4] == '_':
                    project_folders.append(item)

        # Compare with projects in the CSV
        csv_projects = set(df.index.tolist())
        folder_projects = set(project_folders)
        missing_from_csv = folder_projects - csv_projects

        if missing_from_csv:
            print(f"WARNING: Found {len(missing_from_csv)} project folder(s) not in CSV {pfilename}: {sorted(missing_from_csv)}")
            print(f"Please manually add these projects to the CSV file if they should be tracked.")

    # +++++ Check if the file has been changed git status +++++
    if not args.no_git:
        gitmod = GitChanged(repo, pfilename, gitmod)

    return df, gitmod


def _syncSiteSensors(df_flog, ProjectInfo, project, node, args, repo, gitmod):
    """
    Rebuild each site's sensors list in ProjectSummary.yaml from the field log.

    Every site entry that has matching field-log rows (same Site name and
    Year) gets its ``sensors`` list replaced with ``[{sensor: count}, ...]``
    where count is the number of field-day rows for that sensor. Sites with
    no matching rows are left untouched so manually entered sensors survive.

    Parameters
    ----------
    df_flog : pandas.DataFrame
        The project field log.
    ProjectInfo : dict
        Loaded project YAML data (mutated in place).
    project : str
        The project name.
    node : dict
        Dictionary containing node information (e.g., name).
    args : argparse.Namespace
        Parsed command-line arguments.
    repo : git.Repo
        GitPython Repo object for git operations.
    gitmod : bool
        Flag indicating if the git repo has been modified.

    Returns
    -------
    gitmod : bool
        Updated git modification flag.
    """
    changed = False
    for site in ProjectInfo["project"]["sites"]:
        if not isinstance(site, dict):
            continue
        # +++++ Count field-day rows per sensor for this site +++++
        mask = (df_flog["Site"] == site.get("name")) & (df_flog["Year"] == site.get("year"))
        counts = df_flog.loc[mask, "Sensor"].value_counts()
        if counts.empty:
            continue  # No log rows for this site; keep any manual entries
        sensors = [{sensor: int(n)} for sensor, n in sorted(counts.items())]
        if site.get("sensors") != sensors:
            site["sensors"] = sensors
            changed = True

    # ========== Write the yaml back if anything changed ==========
    if changed:
        psyl_fname = f"./{node['name']}/{project}/ProjectSummary.yaml"
        with open(psyl_fname, "w") as f:
            yaml.dump(ProjectInfo, f, sort_keys=False)
        print(f"Updated site sensors from field log in {psyl_fname}")
        if not args.no_git:
            _gitAddRetry(repo, psyl_fname)
            gitmod = True
    return gitmod


def _defaultProjectYAML(project):
    """
    Generate a default project YAML structure.

    Returns
    -------
    project_data : dict
        Default project YAML structure.
    """
    project_data = {
        "project": {
            "ShortName": f"{project}",
            "FullName": "",
            "description": "",
            "start_date": "",
            "end_date": "",
            "funding_source": "",
            "status": "",
            "ProjectCode":"",
            "Internal":None,
            "researchers": [
                {
                    "FirstName": "",
                    "LastName": "",
                    "Title": "",
                    "email": "",
                    "institution": "",
                    "role": "Principal Investigator",
                    "orcid": ""
                },
            ],
            "sites": [
                {
                    "name": "",
                    "year": -9999,  # This is a placeholder for the year
                    "season": "",
                    "SubLocation": "",
                    "latitude": np.nan,
                    "longitude": np.nan,
                    "description": "",
                    "ControlledEnvironment":None,
                    "sensors": [],  # "GOBI", "HIRES", "M3M"
                },
            ]
        },
    }
    return project_data

def check_yaml_structure(proj_data, default_structure):
    """
    Check if proj_data has the same structure as the default YAML.

    Parameters
    ----------
    proj_data : dict
        The loaded project YAML data to check.
    default_structure : dict
        The default structure to compare against.

    Returns
    -------
    is_valid : bool
        True if structure matches, False otherwise.
    missing_keys : list
        List of missing key paths.
    """
    missing_keys = []

    def check_nested_dict(actual, expected, path=""):
        for key, value in expected.items():
            current_path = f"{path}.{key}" if path else key

            if key not in actual:
                missing_keys.append(current_path)
            elif isinstance(value, dict) and isinstance(actual[key], dict):
                check_nested_dict(actual[key], value, current_path)
            elif isinstance(value, list) and not isinstance(actual[key], list):
                missing_keys.append(f"{current_path} (should be list)")

    check_nested_dict(proj_data, default_structure)
    return len(missing_keys) == 0, missing_keys

def update_yaml_structure(proj_data, default_structure):
    """
    Update proj_data to match the default structure by adding missing keys.

    Parameters
    ----------
    proj_data : dict
        The project data to update.
    default_structure : dict
        The default structure to use as template.

    Returns
    -------
    updated_data : dict
        The updated project data with missing keys added.
    """
    def create_ordered_dict(actual, expected):
        """Create an OrderedDict with keys in the same order as expected."""
        ordered = OrderedDict()

        # First, add all keys from expected in their original order
        for key, default_value in expected.items():
            if key in actual:
                if isinstance(default_value, dict) and isinstance(actual[key], dict):
                    # Recursively order nested dictionaries
                    ordered[key] = create_ordered_dict(actual[key], default_value)
                else:
                    ordered[key] = actual[key]
            else:
                # Add missing key with default value
                if isinstance(default_value, dict):
                    ordered[key] = create_ordered_dict({}, default_value)
                elif isinstance(default_value, list):
                    ordered[key] = []
                else:
                    ordered[key] = default_value

        # Then, add any extra keys from actual that aren't in expected
        for key, value in actual.items():
            if key not in ordered:
                ordered[key] = value

        return ordered

    def convert_to_dict(obj):
        """Recursively convert OrderedDict to regular dict."""
        if isinstance(obj, OrderedDict):
            return {k: convert_to_dict(v) for k, v in obj.items()}
        elif isinstance(obj, dict):
            return {k: convert_to_dict(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert_to_dict(item) for item in obj]
        else:
            return obj

    updated_data = create_ordered_dict(proj_data, default_structure)
    return convert_to_dict(updated_data)  # Convert to regular dict recursively

def _migrateResearchers(proj_data):
    """
    Convert a legacy single ``researcher`` mapping to the ``researchers`` list.

    Parameters
    ----------
    proj_data : dict
        Loaded project YAML data (mutated in place).

    Returns
    -------
    bool
        True if a migration was performed, False otherwise.
    """
    project_block = proj_data.get("project") if isinstance(proj_data, dict) else None
    if not isinstance(project_block, dict):
        return False
    if "researchers" in project_block or "researcher" not in project_block:
        return False
    legacy = project_block.pop("researcher")
    if isinstance(legacy, dict):
        project_block["researchers"] = [legacy]
    elif isinstance(legacy, list):
        project_block["researchers"] = legacy
    else:
        project_block["researchers"] = []
    return True


def _projYAML(project, pym_fn, args, repo, gitmod):
    """
    Ensure a project YAML file exists and is loaded.

    Parameters
    ----------
    project : str
        The project name.
    pym_fn : str
        Path to the project YAML file.
    args : argparse.Namespace
        Parsed command-line arguments.
    repo : git.Repo
        GitPython Repo object for git operations.
    gitmod : bool
        Flag indicating if the git repo has been modified.

    Returns
    -------
    Proj_data : dict
        Loaded project YAML data.
    gitmod : bool
        Updated git modification flag.

    Notes
    -----
    - Creates a template YAML file if missing.
    - Adds new files to git if enabled.
    """
    # +++++ Check if the project yaml file already exists +++++
    if not os.path.isfile(pym_fn):
        project_data = _defaultProjectYAML(project)
        with open(pym_fn, "w") as f:
            yaml.dump(project_data, f, sort_keys=False)
        print(f"New Project YAML file created: {pym_fn}. Please edit it to add project and site information")
        # +++++ Add the file to the github repo +++++
        if not args.no_git:
            _gitAddRetry(repo, pym_fn)
            gitmod = True   

    # +++++ Load the yaml file +++++
    Proj_data = yaml.safe_load(open(f"{pym_fn}", "r"))

    # ========== Migrate legacy single-researcher files ==========
    migrated = _migrateResearchers(Proj_data)
    if migrated:
        print(f"Migrated 'researcher' mapping to 'researchers' list in {pym_fn}")

    # ========== Check structure and update if needed ==========
    default_structure = _defaultProjectYAML(project)
    is_valid, missing_keys = check_yaml_structure(Proj_data, default_structure)

    if not is_valid or migrated:
        if not is_valid:
            print(f"YAML structure missing keys in {pym_fn}: {missing_keys}")
        # update_yaml_structure also restores the canonical key order
        updated_data = update_yaml_structure(Proj_data, default_structure)
        with open(pym_fn, "w") as f:
            yaml.dump(updated_data, f, sort_keys=False)
        print(f"Updated YAML structure in {pym_fn}")
        if not args.no_git:
            _gitAddRetry(repo, pym_fn)
            gitmod = True
        Proj_data = updated_data

    # breakpoint()
    if not args.no_git:
        gitmod = GitChanged(repo, pym_fn, gitmod)

    # breakpoint()
    return Proj_data, gitmod


def _df_col_check(dfx, fname, colnms, args, repo, gitmod, fill_val=None):
    """
    Ensure DataFrame has required columns and is tracked by git.

    Parameters
    ----------
    dfx : pandas.DataFrame
        DataFrame to check.
    fname : str
        Path to the CSV file.
    colnms : list of str
        Required column names.
    args : argparse.Namespace
        Parsed command-line arguments.
    repo : git.Repo
        GitPython Repo object for git operations.
    gitmod : bool
        Flag indicating if the git repo has been modified.
    fill_val : any, optional
        Value to fill for missing columns (default is None).

    Returns
    -------
    dfx : pandas.DataFrame
        DataFrame with required columns.
    gitmod : bool
        Updated git modification flag.

    Notes
    -----
    - Adds missing columns with fill_val.
    - Adds file to git if enabled.
    """
    # +++++ Do a column name check and a repo check +++++
    #  Do column check and add missing columns if needed.

    if not (dfx.columns.tolist() == colnms) :
        # +++++ Column missmatch +++++
        missing_cols = set(colnms) - set(dfx.columns)
        # breakpoint()
        # +++++ Add the missing columns to the DataFrame with a default value +++++
        if not missing_cols == {}:
            print(f"col missing in: {fname}. Fix applied by adding: {missing_cols}")
            for col in missing_cols:
                dfx[col] = fill_val
            # +++++ Invalidate any existing checksums +++++
            # Adding columns changes the row hash, so previously stored
            # CheckSum values would no longer match. Clearing them forces
            # Rowchecker to recompute the checksum on the next pass instead
            # of tripping the mismatch breakpoint.
            if "CheckSum" in dfx.columns and (missing_cols - {"CheckSum"}):
                dfx["CheckSum"] = np.nan
            # ===== Reorder and save =====
            dfx = dfx[colnms]
            # Preserve the original CSV layout: write the index only when the
            # DataFrame has a named index (e.g. ProjectsSummary uses Project as
            # the index). FieldLog-style frames have no named index and should
            # be written without one to avoid an extra unnamed column.
            dfx.to_csv(fname, index=dfx.index.name is not None)
        if not args.no_git:
            _gitAddRetry(repo, fname)
            gitmod = True
    # +++++ Check if the file is in the repo +++++
    if not args.no_git:
        # +++++ che both the repo and the staged area +++++
        if not (fileInRepo(repo, fname) or is_file_staged(repo, fname)):
            print(f"File: {fname} is not in the git repo. Adding it to the repo")
            _gitAddRetry(repo, fname)
            gitmod = True

    return dfx, gitmod


def _sitenamemaker(site, psyl_fname=""):
    """
    Generate a standardized site name based on year and controlled environment status.

    Parameters
    ----------
    site : dict
        Dictionary containing site information.
    psyl_fname : str, optional
        Path to the project YAML file (for error reporting).

    Returns
    -------
    sitename : str
        Standardized site name.

    Raises
    ------
    ValueError
        If ControlledEnvironment is not True, False, or None.
    """
    sitename = f"{site['year']}{site['name']}"
    # ===== Check the Site is a controlled environment =====
    if not site["ControlledEnvironment"] is None:
        if not site["ControlledEnvironment"] in [True, False]:
            raise ValueError(f"Site: {site['name']} has an invalid ControlledEnvironment: {site['ControlledEnvironment']}. Must be True, False or null. Setting to False. File: {psyl_fname}")
        elif site["ControlledEnvironment"]:
            sitename = f"{sitename}_C" # Controlled environment
        else:
            sitename = f"{sitename}_F" # Field Site
    return sitename

# ==============================================================================
def _seedDocReadme(folder, kind, yyyysite, args, repo, gitmod):
    """
    Seed a README.md stub in a site-level Documentation subfolder.

    Parameters
    ----------
    folder : str
        Path to the Documentation subfolder (e.g. ``.../Documentation/Plot_Layout``).
    kind : str
        Subfolder name; must be ``"Plot_Layout"`` or ``"Trial_Info"``.
    yyyysite : str
        ``{YYYY}{SiteName}`` key used in plot/trial filenames (no
        ``_F`` / ``_C`` controlled-environment suffix).
    args : argparse.Namespace
        Parsed CLI arguments.
    repo : git.Repo or None
        GitPython Repo object (``None`` when ``--no-git`` is set).
    gitmod : bool
        Current git-modification flag.

    Returns
    -------
    gitmod : bool
        Updated git-modification flag.

    Notes
    -----
    Templates follow the Folder README example in the Plot Delineation
    protocol. Existing READMEs are never overwritten — operators may have
    populated them with site-specific context.
    """
    fname = f"{folder}/README.md"
    if os.path.isfile(fname):
        return gitmod

    if kind == "Plot_Layout":
        body = (
            f"# Plot Layout — {yyyysite}\n\n"
            "Site-specific notes for the files in this folder. For the\n"
            "APPN-wide spec see the [Plot Delineation protocol]"
            "(https://github.com/ArdenB/APPN-Aerial-Standard-Operating-Procedures/"
            "blob/main/Protocols/PlotProtocols/PlotDelineation/Plot_Delineation.md).\n\n"
            "## Current main file\n"
            f"- `{yyyysite}_plots.geojson` — fitted YYYY-MM-DD by <name>,\n"
            "  method <FIELDimageR | DPIRD | GPT>.\n\n"
            "## Variants in use\n"
            "- `plots_unbuffered` — used by <pipeline / person> for <reason>.\n"
            "- `plots_{sensor}` — justified because <CRS / portion-of-plot reason>;\n"
            "  approved by EWG on <date>.\n\n"
            "## Sampling campaigns\n"
            "- `sampling_biomass_YYYYMMDD…` — operator, quadrat size, anything odd.\n\n"
            "## Deprecated files\n"
            "| File | Replaced on | Reason | Superseded by |\n"
            "| --- | --- | --- | --- |\n"
            f"| `{yyyysite}_plots_YYYYMMDD_deprecated.geojson` | YYYY-MM-DD | "
            f"<reason> | `{yyyysite}_plots.geojson` |\n\n"
            "## Known issues / quirks\n"
            "- e.g. \"HIRES flight 2025-10-12 had a 7 cm N–S offset — corrected in\n"
            "  re-process.\"\n"
        )
    elif kind == "Trial_Info":
        body = (
            f"# Trial Info — {yyyysite}\n\n"
            "Site-specific notes for the trial-information spreadsheet(s)\n"
            "in this folder. For the APPN-wide spec see the\n"
            "[Plot Delineation protocol — Trial Information]"
            "(https://github.com/ArdenB/APPN-Aerial-Standard-Operating-Procedures/"
            "blob/main/Protocols/PlotProtocols/PlotDelineation/Plot_Delineation.md"
            "#joining-trial-information).\n\n"
            "## Current trial info file\n"
            f"- `{yyyysite}_trial_info.csv` — source: <spreadsheet / contact>,\n"
            "  last updated YYYY-MM-DD.\n\n"
            "## Column definitions\n"
            "- `plot_id` (mandatory) — joins to the plot file in `../Plot_Layout/`.\n"
            "- `<column>` — <definition / units>.\n\n"
            "## Source spreadsheets and contacts\n"
            "- <person / team> — <where the master spreadsheet lives>.\n\n"
            "## Deprecated files\n"
            "| File | Replaced on | Reason | Superseded by |\n"
            "| --- | --- | --- | --- |\n"
            f"| `{yyyysite}_trial_info_YYYYMMDD_deprecated.csv` | YYYY-MM-DD | "
            f"<reason> | `{yyyysite}_trial_info.csv` |\n\n"
            "## Known quirks\n"
            "- e.g. \"Plot 1042 was resown — exclude from emergence stats.\"\n"
        )
    else:
        raise ValueError(f"Unknown Documentation subfolder kind: {kind}")

    with open(fname, "w") as f:
        f.write(body)
    print(f"Seeded README template: {fname}")
    if not args.no_git and repo is not None:
        _gitAddRetry(repo, fname)
        gitmod = True
    return gitmod

# ==============================================================================
def Rowchecker(flog_fname, flrow, prow, ProjectInfo, historical, update_checksums=False, past_date=(pd.Timestamp.now()-pd.Timedelta(days=14))):
    """
    Validate a row from the field log for correctness and consistency.

    Parameters
    ----------
    flog_fname : str
        Path to the field log file. Used for error reporting.
    flrow : pandas.Series
        The field log row to be checked.
    prow : pandas.Series
        The project row to be checked. Used to verify valid sensors.
    ProjectInfo : dict
        Project information dictionary containing project details.
    historical : bool
        Whether to allow dates earlier than the default past_date.
    update_checksums : bool, optional
        Accept rows whose stored CheckSum no longer matches (i.e. rows edited
        after registration) without prompting, and recompute the checksum.
    past_date : pandas.Timestamp, optional
        The earliest allowed date for the log entry. Defaults to 14 days before the current date.

    Returns
    -------
    check : float or None
        The computed checksum for the row if it needs to be updated, or None if already valid.
    site : dict
        The site dictionary matching the row's site.

    Raises
    ------
    ValueError
        If any validation check fails.

    Notes
    -----
    - Checks data types, date validity, sensor validity, and run count.
    - Computes and verifies a checksum for row integrity.
    - Designed to be extended for additional checks.
    """

    def _ErrorMessage(error_message, flog_fname, flrow, nl = '\n'):
        """
        Internal function to make a standard error message
        """
        raise ValueError(
            f"Problem found while checking the Field Log: {flog_fname}{nl}"
            f"Offending row:{nl}{flrow}{nl}{nl}{error_message}")

    # ========== Do the Hashing of the data ==========
    hashes = pd.util.hash_pandas_object(flrow.drop("CheckSum"))
    check  = float(hashes.sum() % 100000000) # Using the % because the numbers were too big and were getting corrupted by the csv
    if not np.isnan(flrow.CheckSum):
        # Make sure the checksum matches
        if not check == flrow.CheckSum:
            print(
                f"\nCheckSum mismatch in {flog_fname} (row index {flrow.name}):\n"
                f"{flrow}\n"
                f"Stored CheckSum: {flrow.CheckSum:.0f}, recomputed: {check:.0f}\n"
                "This row was edited after it was first registered. Accepting it "
                "will update the checksum and (re)build folders for the new "
                "values. Folders built from the OLD values are NOT removed - "
                "clean those up manually if the date/site/sensor changed.")
            if update_checksums:
                print("--update-checksums given: accepting the edited row.")
            elif sys.stdin.isatty():
                resp = input("Accept the edited row and update its checksum? [y/N] ")
                if resp.strip().lower() not in ("y", "yes"):
                    _ErrorMessage(
                        "Edited row rejected by user. Revert the edit in the "
                        "field log (or fix it and re-run) to continue.",
                        flog_fname, flrow)
            else:
                _ErrorMessage(
                    "CheckSum mismatch and no way to confirm (non-interactive "
                    "session). Re-run interactively, or re-run with "
                    "--update-checksums to accept edited rows automatically.",
                    flog_fname, flrow)
        else:
            # Make the check = None to indicate its already done
            check = None

    # ========== Check all the dtypes rows ==========
    for vname in ["Year", "Month", "Day", "Runs"]:
        if not type(flrow[vname]) == int:
            _ErrorMessage(
                f"'{vname}' must be a whole number, but this row has "
                f"{flrow[vname]!r}. A blank or non-numeric cell is the usual "
                f"cause - please put a plain integer in the '{vname}' column.",
                flog_fname, flrow)

    for sname in ["Technician", "Sensor", "Site"]:
        if not type(flrow[sname]) == str:
            _ErrorMessage(
                f"'{sname}' must be text, but this row has {flrow[sname]!r}. "
                f"This usually means the '{sname}' cell was left blank - every "
                f"row needs a value (e.g. Technician cannot be empty).",
                flog_fname, flrow)


    # ========== Check the date of the row ==========
    try:
        date = pd.Timestamp(f"{flrow.Year}-{flrow.Month}-{flrow.Day}")
    except Exception as er:
        _ErrorMessage(
            f"Could not build a valid date from Year={flrow.Year}, "
            f"Month={flrow.Month}, Day={flrow.Day} ({er}). Please check the "
            f"date columns of this row.",
            flog_fname, flrow)

    # +++++ Check if the date is in the future or past +++++
    if not check is None:
        # Skip the datacheck in case of already complete data
        if date > (pd.Timestamp.now() + pd.Timedelta(hours=12)):
            _ErrorMessage(
                f"Row date {date:%Y-%m-%d} is in the future (current time "
                f"{pd.Timestamp.now():%Y-%m-%d %H:%M}). Future dates are not "
                f"allowed - please correct the Year/Month/Day columns.",
                flog_fname, flrow)
        elif date < past_date:
            if not historical:
                _ErrorMessage(
                    f"Row date {date:%Y-%m-%d} is more than 14 days in the "
                    f"past (cutoff {past_date:%Y-%m-%d}). If this is genuine "
                    f"historical data, re-run with: "
                    f"python ProjectBuilder.py --historical",
                    flog_fname, flrow)
    
    # ========== Check if the sensor is valid ==========
    valid_sensors = list(prow[prow == True].index)
    if not flrow.Sensor in valid_sensors:
        _ErrorMessage(
            f"Sensor '{flrow.Sensor}' is not enabled for this project. "
            f"Currently enabled sensors: {valid_sensors}. To fix, either set "
            f"{flrow.Sensor} to TRUE in the {{NodeName}}_ProjectsSummary.csv "
            f"in the node folder, or re-run with --enable-sensors to do it "
            f"automatically.",
            flog_fname, flrow,
        )

    # ========== Check the number of runs ==========
    if flrow.Runs < 1:
        _ErrorMessage(
            f"'Runs' is {flrow.Runs} but must be at least 1 (the number of "
            f"runs/flights collected on this day).",
            flog_fname, flrow)
    # ========== Check the site name and year ==========
    valid_sites = [site["name"] for site in ProjectInfo["project"]["sites"]]
    if not flrow.Site in valid_sites:
        _ErrorMessage(
            f"Site '{flrow.Site}' is not one of this project's sites "
            f"({valid_sites}). Add the site to the ProjectSummary.yaml in the "
            f"project folder, or fix the Site spelling in this row.",
            flog_fname, flrow)
    else:
        # +++++ Check the site year matches row year +++++
        errorlog = "" # COntiner for error messages
        outsite = None
        for site in ProjectInfo["project"]["sites"]:

            if site["name"] == flrow.Site:
                if not site["year"] == flrow.Year:
                    errorlog += (
                        f"Site '{flrow.Site}' is registered with year "
                        f"{site['year']} but this row has year {flrow.Year}. "
                        f"Please fix the row, or update the site's year in "
                        f"the ProjectSummary.yaml.")
                    continue
                else:
                    outsite = site
                    break
            # This whould only be reached if the site is not found
        if outsite is None:
            _ErrorMessage(errorlog, flog_fname, flrow)
        # breakpoint()
    return check, outsite

def pymkdir(path):
    """
    Create a directory if it does not already exist.

    Parameters
    ----------
    path : str
        The directory path to create.

    Returns
    -------
    None

    Notes
    -----
    If the directory already exists, nothing happens.
    """
    if not os.path.exists(path):
        print(path)
        os.makedirs(path)

def _gitAddRetry(repo, fname, attempts=5, delay=0.5):
    """git-add with retries around transient index.lock contention.

    Bulk migrations rewrite many files in a tight loop; any concurrent
    git client (e.g. the VS Code git extension refreshing its status)
    briefly holds ``.git/index.lock`` and makes a plain ``git add`` die
    with exit 128. Those windows are milliseconds wide, so retrying
    with a short backoff is safe; any other git error re-raises
    immediately.

    Parameters
    ----------
    repo : git.Repo
        A GitPython Repo object.
    fname : str
        Path to stage.
    attempts : int, optional
        Maximum tries before re-raising (default 5).
    delay : float, optional
        Base sleep in seconds, scaled linearly per attempt (default 0.5).

    Returns
    -------
    None
    """
    for attempt in range(attempts):
        try:
            repo.git.add(fname)
            return
        except git_exc.GitCommandError as err:
            if "index.lock" not in str(err) or attempt == attempts - 1:
                raise
            print(f"index.lock contention staging {fname} "
                  f"(attempt {attempt + 1}/{attempts}), retrying ...")
            time.sleep(delay * (attempt + 1))

def is_file_staged(repo, filepath):
    """
    Check if a file is staged (added to the index) in the given git repository.

    Parameters
    ----------
    repo : git.Repo
        A GitPython Repo object.
    filepath : str
        Path to the file (relative to repo root).

    Returns
    -------
    bool
        True if the file is staged, False otherwise.
    """
    # Remove leading './' for consistency
    relpath = filepath.replace("./", "")
    # Check if file is in the index but not in HEAD (i.e., staged for commit)
    staged_files = [item.a_path for item in repo.index.diff("HEAD")]
    return relpath in staged_files

def fileInRepo(repo, filePathIN):
    """
    Check if a file exists in the given git repository.

    Parameters
    ----------
    repo : git.Repo
        A GitPython Repo object.
    filePathIN : str
        The file path (relative to the repository root).

    Returns
    -------
    bool
        True if the file exists in the repository, False otherwise.

    Notes
    -----
    The function removes leading './' from the path for compatibility with git.
    """
    filePath = filePathIN.replace("./", '') # git doesnt have the ./ in paths
    pathdir = os.path.dirname(filePath)

    # Build up reference to desired repo path
    rsub = repo.head.commit.tree

    for path_element in pathdir.split(os.path.sep):

        # If dir on file path is not in repo, neither is file. 
        try : 
            rsub = rsub[path_element]

        except KeyError : 
            return False
    return(filePath in rsub)

def GitChanged(repo, fname, gitmod):
    """
    Check if a file has been modified in the git repository and stage it if so.

    Parameters
    ----------
    repo : git.Repo
        A GitPython Repo object.
    fname : str
        The file name to check.
    gitmod : bool
        Current git modification flag.

    Returns
    -------
    gitmod : bool
        Updated git modification flag.
    """
    # +++++ Check if the file is in the repo +++++
    if not fileInRepo(repo, fname):
        print(f"WARNING. File: {fname} is not in the git repo. Adding it to the repo")
        _gitAddRetry(repo, fname)
        gitmod = True

    unstaged_diffs = repo.index.diff(None)
    # ========== Loop over the difs ==========
    for diff in unstaged_diffs:
        # +++++ Check if the dif matches the file +++++
        if diff.a_path == fname.replace("./", '') or diff.b_path == fname.replace("./", ''):
            # File has been modified
            if diff.change_type == 'M':
                _gitAddRetry(repo, fname)
                gitmod = True
            else:
                print(f"WARNING. File: {fname} has been modified but the change type in not M. Change type:{diff.change_type}. File Not added to repo")
            break # Exit the loop as we found the file
    return gitmod

def GitPull(repo):
    """Pulls the latest changes from a given Git repository.

    This function attempts to update the local repository by pulling the latest changes
    from the remote. If the repository is not up to date after the pull, it notifies the user,
    prints the pull log, and exits the script. There is a placeholder for adding an option
    to skip this check in the future.

    Parameters
    ----------
    repo : git.Repo
        The GitPython Repo object representing the repository to pull from.

    Returns
    -------
    None

    Raises
    ------
    SystemExit
        If the repository is not up to date after the pull operation.

    """

    print(f"Starting git pull of the repo at: {pd.Timestamp.now()}")
    pull_log = repo.git.pull()

    # TO DO: Add a way to force skip this check 
    if not pull_log == 'Already up to date.':
        # ========== get the file name of the script ==========
        script_name = os.path.basename(__file__)
        print(f"The remote git repo is not in sync. Log: {pull_log}")
        if script_name in pull_log:
            print(f"WARNING: The script {script_name} has been modified in the remote repo. Please restart the script to use the updated version")
            raise SystemExit
        elif pull_log.startswith("Updating"):
            print(f"WARNING: The repo has been updated. ")
        else:
            print(f"WARNING: The repo has been updated. with unknown chane string.")
            print("Please rerun the script. if there are repeat errors there might be a git or connection issue.  Script can be run with --no-git to skip pull")
            sys.exit()


# ==================================================================================
if __name__ == '__main__':

    # ========== Set the args Description ==========
    description='Optional Command line arguments for script'
    parser = argparse.ArgumentParser(description=description)

    # ========== Add the command line arguments ==========   
    parser = argparse.ArgumentParser(description="Generate dataset folder structure.")
    parser.add_argument("--no-git", action="store_true", help="Disable git operations.")
    # parser.add_argument("--node", type=str, default="Narrabri", help="Node name.")
    # parser.add_argument("--projects-csv", type=str, default="./Projects_Summary.csv", help="Projects summary CSV path.")
    parser.add_argument("--projectsYAML", type=str, default="./NodeSummary.yaml", help="the node yaml file with the sensors.")
    parser.add_argument("-p","--historical", action="store_true", help="Allow historical data")
    parser.add_argument(
        "--update-checksums",
        action="store_true",
        help=("Accept FieldLog rows whose CheckSum no longer matches (rows "
              "edited after they were first registered) without prompting, "
              "and rewrite the checksum. Without this flag an interactive "
              "session prompts y/N per row; a non-interactive session errors."),
    )
    parser.add_argument(
        "--enable-sensors",
        action="store_true",
        help=("If a sensor referenced in a project's FieldLog is currently set "
              "to FALSE in <node>_ProjectsSummary.csv, automatically flip it to "
              "TRUE and save the CSV instead of raising an error. The sensor "
              "must already exist as a column in the CSV."),
    )
    
    args = parser.parse_args()

    # +++++ Check the paths and set exc path to the root of the git folder +++++
    if not args.no_git:
        path = os.getcwd()
        try:
            git_repo = git.Repo(path, search_parent_directories=True)
            git_root = git_repo.git.rev_parse("--show-toplevel")
        except git_exc.InvalidGitRepositoryError as err:
            raise git_exc.InvalidGitRepositoryError(
                f"This script was called from an unknown path ({path}). Must be in a git repo"
            ) from err
        finally:
            sys.path.append(git_root)
            os.chdir(git_root)

        # # +++++ Check if the repo is up to date +++++
        repo = git.Repo(git_root)
    else:
        repo = None
    
    # ========== Parse Args to main function ==========
    main(args, repo)

