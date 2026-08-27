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
- **NodeSummary.yaml:** YAML file listing nodes and their sensor platforms.
- **{NodeName}_ProjectsSummary.csv:** CSV file summarizing projects and their associated sensors (auto-created in the node folder).
- **ProjectSummary.yaml:** YAML file containing detailed project, researcher, and site information (auto-created in each project folder).
- **FieldLog.csv:** Per-project log of field collection events; rows here drive the creation of sensor/date/run folders (auto-created in each project folder).
- **README.md:** This documentation file.


## License

[MIT License](LICENSE)

## Contact

For questions or contributions, please contact the repository maintainer.
