# APPN Generic File Storage

📖 **[Project Wiki](https://github.com/ArdenB/APPN_GenericFileStorage/wiki)**

This repository provides a data structure and automation scripts for managing data storage across APPN nodes. It is designed to streamline and automate the creation of folders, project logs, and metadata files for research projects, sites, and sensor platforms. It is tailored for the field MPU infrastructure.

## Features

- **Automated Folder Creation:** Scripts to generate and organize folders for projects, sites, and sensors.
- **YAML/JSON Metadata:** Templates and tools for maintaining project, researcher, and site metadata in YAML and JSON formats.
- **Git Integration:** Optional git version control for tracking changes to folders and metadata.
- **Customizable Structure:** Easily adapt the folder and metadata structure to suit different research needs.


## Quick Start: Create Your First Project

`ProjectBuilder.py` works in three passes. The first pass creates the node-level
project table, the second creates project metadata templates, and the third
builds site, sensor, date, and run folders from the completed metadata.

Run every command below from the repository root.

### 1. Create and clone your repository

For a new APPN node or data store, open the
[template repository](https://github.com/ArdenB/APPN_GenericFileStorage) and
select **Use this template** → **Create a new repository**. Choose the owner,
repository name, and visibility appropriate for your node. This creates an
independent repository with its own history and remote, which is the recommended
setup for an operational deployment.

Fork the repository instead when you intend to contribute changes back to the
generic template.

Clone the repository you created, then enter it:

```bash
git clone https://github.com/<owner>/<repository-name>.git
cd <repository-name>
```

### 2. Create the builder environment

The folder builder requires only Python, NumPy, pandas, PyYAML, GitPython, and
Git:

```bash
conda create -n datastorage -c conda-forge \
      python=3.12 numpy pandas pyyaml gitpython git
conda activate datastorage
```

The processing and analysis scripts require additional geospatial and
scientific packages. Install those when you need to run a pipeline:

```bash
conda install -n datastorage -c conda-forge \
      geopandas rioxarray rasterio pyarrow laspy lazrs-python tqdm \
      matplotlib seaborn spyndex
```

### 3. Configure the node

Edit `NodeSummary.yaml`. Each node needs a unique name and a list of the sensor
platforms available there. For example:

```yaml
nodes:
   - name: "USYD_Narrabri"
      university: "University of Sydney"
      location: "Narrabri, NSW, Australia"
      SensorPlatforms:
         - GOBI
         - HIRES
         - GroundTruth
```

Sensor names are identifiers: spelling and capitalization must remain
consistent in every metadata file.

### 4. Pass 1: generate the project table

```bash
python ProjectBuilder.py
```

This creates:

```text
USYD_Narrabri/
└── USYD_Narrabri_ProjectsSummary.csv
```

Open that CSV and add one row per project. The `Project` value must follow the
naming convention in `FolderStructureInfo.txt`; sensor columns contain `TRUE`
or `FALSE`.

```csv
Project,GOBI,HIRES,GroundTruth
2026_WheatTrial_I_Smith,TRUE,FALSE,TRUE
```

### 5. Pass 2: generate the project templates

```bash
python ProjectBuilder.py
```

This creates the project folder and its two editable metadata files:

```text
USYD_Narrabri/2026_WheatTrial_I_Smith/
├── FieldLog.csv
└── ProjectSummary.yaml
```

Edit `ProjectSummary.yaml`. At minimum, replace the placeholder site `name` and
`year`. Add the project and researcher details that are known. A completed site
might look like this:

```yaml
project:
   ShortName: 2026_WheatTrial_I_Smith
   FullName: 2026 Wheat Trial
   description: Compare wheat varieties under field conditions.
   start_date: 2026-08-01
   end_date: 2026-12-31
   funding_source: APPN
   status: active
   ProjectCode: APPN-WHEAT-2026
   Internal: true
   researchers:
      - FirstName: Alex
         LastName: Smith
         Title: Dr
         email: alex.smith@example.edu.au
         institution: University of Sydney
         role: Principal Investigator
         orcid: ""
   sites:
      - name: Narrabri
         year: 2026
         season: Winter
         SubLocation: Llara Farm
         latitude: -30.28
         longitude: 149.80
         description: Main field trial.
         ControlledEnvironment: false
         sensors:
            - GOBI
            - GroundTruth
```

`ControlledEnvironment` accepts `true`, `false`, or `null`. The example above
produces the site folder `2026Narrabri_F`; `true` produces the `_C` suffix, and
`null` produces no suffix.

Next, add collection events to `FieldLog.csv`. Keep its generated header and
add one row per site, sensor, and collection date:

```csv
Year,Month,Day,Sensor,Technician,Runs,Site,MakeNotesFile,MakeTableFile,CheckSum
2026,8,27,GOBI,A. Technician,2,Narrabri,,,
```

The required values are:

- `Year`, `Month`, `Day`: collection date as whole numbers.
- `Sensor`: an enabled sensor from the node project table.
- `Technician`: required text; it cannot be blank.
- `Runs`: number of runs to create, as a whole number of at least 1.
- `Site`: must exactly match a site `name` in `ProjectSummary.yaml`, including
   capitalization; its year must also match.
- `MakeNotesFile`, `MakeTableFile`: optional; blank creates both files, while
   `FALSE` suppresses the corresponding file.
- `CheckSum`: leave blank. The builder manages it.

### 6. Pass 3: build the collection folders

```bash
python ProjectBuilder.py
```

Rows more than 14 days old require the historical-data flag:

```bash
python ProjectBuilder.py --historical
```

If a `FieldLog.csv` sensor is valid for the node but is still `FALSE` in the
project table, either change the table to `TRUE` or allow the builder to update
it:

```bash
python ProjectBuilder.py --enable-sensors
```

For the examples above, verify that the builder created:

```text
USYD_Narrabri/2026_WheatTrial_I_Smith/2026Narrabri_F/GOBI/20260827/
├── FieldNotes.txt
├── RunOverview.csv
├── run_00/
│   ├── T0_raw/
│   │   └── Vault/
│   ├── T1_proc/
│   │   └── QC_data/
│   └── T2_traits/
└── run_01/
   ├── T0_raw/
   │   └── Vault/
   ├── T1_proc/
   │   └── QC_data/
   └── T2_traits/
```

The builder is safe to run again: it checks the existing structure and creates
or updates only what is needed.

## Adopting an Existing Data Store

Use this workflow when you already have data organised in the APPN folder
format (see `FolderStructureInfo.txt`) that was **not** built by
`ProjectBuilder.py` — for example a hand-assembled archive or a copy
received from another node. The repository is placed *around* the existing
data, the tree is audited for naming compliance, and the ProjectBuilder
metadata files are reconstructed so the store becomes a normal managed one.

### 1. Create your repository from the template

Open the [template repository](https://github.com/ArdenB/APPN_GenericFileStorage),
select **Use this template** → **Create a new repository**, and choose the
owner, name, and visibility for your store (private is fine). Do not add any
files to it yet.

### 2. Put the repository at the root of the data tree

Run these inside the top-level folder of your existing data (the folder that
contains — or will contain — your node folder):

```bash
cd /path/to/your/data/root
git init -b main
git remote add origin git@github.com:<owner>/<repository-name>.git
git fetch origin
git checkout main
```

If a file such as `NodeSummary.yaml` already exists in the data root,
`git checkout` will refuse to overwrite it: move it aside first
(`mv NodeSummary.yaml NodeSummary.local.yaml`), check out, then merge your
local content back into the checked-out file.

The repository `.gitignore` ignores everything except code and the
ProjectBuilder-maintained metadata files, so the collected data itself can
never be committed — `git status` should stay clean of data files.

### 3. Configure the node and audit the tree

Edit `NodeSummary.yaml` so the node `name` matches your existing node folder
exactly and `SensorPlatforms` lists every sensor folder in use. Then run the
audit (read-only):

```bash
python Code/DS00_DataManagement/DM01_StructureAdopter.py
```

This writes `{Node}/DM01_AdoptionReport.md` grading every folder against the
naming convention:

- **fail** — folders the metadata cannot be inferred from (bad project /
  site / date / run names, sensors missing from `NodeSummary.yaml`). Rename
  the folders (or fix `NodeSummary.yaml`) and re-run until no fails remain.
- **warn** — non-blocking issues (missing tier folders, non-contiguous run
  numbers, misplaced files). Review, fix what matters.
- **info** — placeholders and disagreements to resolve later.

The script exits nonzero while fail-class findings exist, so it can be used
as a hand-over gate in scripts.

### 4. Reconstruct the metadata and hand over to ProjectBuilder

```bash
python Code/DS00_DataManagement/DM01_StructureAdopter.py --apply
```

This prints the planned writes and asks for confirmation, then reconstructs
the three ProjectBuilder input files from the tree: the node
`{Node}_ProjectsSummary.csv`, each project's `ProjectSummary.yaml` (sites
inverted from the folder names) and `FieldLog.csv` (one row per site /
sensor / date, `Technician = Unknown`, checksums left blank). Existing
metadata files are merged append-only — hand-entered rows are never
modified. Then let ProjectBuilder create everything derived:

```bash
python ProjectBuilder.py --historical --enable-sensors --no-git
```

This fills the FieldLog checksums and creates `RunOverview.csv`,
`FieldNotes.txt`, missing tier folders, and the site `Documentation/`
templates. Finally, work through the TODO checklist at the bottom of
`DM01_AdoptionReport.md` (real technician names, project/site metadata),
review, and publish:

```bash
git status          # metadata files only -- no data
git add -A
git commit -m "Adopt existing data store"
git push -u origin main
```

## Git Behavior

By default, `ProjectBuilder.py` pulls before making changes and commits and
pushes files that it creates or updates. Use `--no-git` to build locally
without any Git pull, commit, or push:

```bash
python ProjectBuilder.py --no-git
```

Review the generated changes before publishing them when using `--no-git`:

```bash
git status
git diff
```

## File Descriptions

- **ProjectBuilder.py:** Main script for automating folder and metadata creation.
- **Code/DS00_DataManagement/DM01_StructureAdopter.py:** Audits an existing APPN-format tree and reconstructs the ProjectBuilder metadata files so the tree can be adopted (see *Adopting an Existing Data Store*).
- **NodeSummary.yaml:** YAML file listing nodes and their sensor platforms.
- **{NodeName}_ProjectsSummary.csv:** CSV file summarizing projects and their associated sensors (auto-created in the node folder).
- **ProjectSummary.yaml:** YAML file containing detailed project, researcher, and site information (auto-created in each project folder).
- **FieldLog.csv:** Per-project log of field collection events; rows here drive the creation of sensor/date/run folders (auto-created in each project folder).
- **README.md:** This documentation file.


## License

[MIT License](LICENSE)

## Contact

For questions or contributions, please contact the repository maintainer.
