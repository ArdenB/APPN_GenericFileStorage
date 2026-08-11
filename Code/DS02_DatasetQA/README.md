# ELM Validation Panel Extraction

## Overview

This script automates the extraction and quality control (QC) of spectral data from validation panels in hyperspectral imaging datasets. It crawls the dataset file structure to locate QC panel vector files and their corresponding raster orthomosaics, extracts pixel values from the panels, and generates visualization plots for quality assessment. It is designed to work in directorys that follow the APPN folder structure.

QC panel files must follow the official [AerialDataQC naming convention](https://github.com/aus-plant-phenomics-network/APPN-Field-Protocols-and-Pipelines/blob/main/Protocols/QA/QAprocess/AerialDataQC.md): `QC_{ELM|VAL}[_{TargetIdentifier}]_Panels[_{Extra}].geojson` (GeoJSON preferred, shapefile accepted), stored in `<run>/T1_proc/QC_data/`.

**Version:** v1.0 (03.03.2026)  
**Author:** Arden Burrell

## What It Does

1. **Searches** for QC panel files matching the official convention `QC_{ELM|VAL}[_{TargetIdentifier}]_Panels[_{Extra}].geojson` (or `.shp`) under `T1_proc/QC_data/`
2. **Locates** corresponding VNIR and SWIR orthomosaic raster files
3. **Extracts** spectral reflectance values from pixels within the panel geometries
4. **Saves** extracted data as CSV or Parquet files
5. **Generates** visualization plots showing spectral curves across different panels and dates
6. **Supports** data sharing between nodes via save/load directories

## Prerequisites

### Required Python Packages
```bash
- numpy
- pandas
- xarray
- rioxarray
- geopandas
- shapely
- matplotlib
- seaborn
- tqdm
- GitPython
```

**Tested versions**:
- numpy (2.2.6)
- pandas (2.3.2)
- xarray (2025.9.0)
- rioxarray (0.19.0)
- geopandas (1.1.1)
- shapely (2.1.1)
- matplotlib (3.10.6)
- seaborn (0.13.2)
- tqdm (4.67.1)
- GitPython (3.1.45)


### Setting Up a Conda Environment

To create a conda environment with all required dependencies, use the following commands:

```bash
# Create a new conda environment named 'elm-qa' with Python 3.13
conda create -n elm-qa python=3.13 -c conda-forge

# Activate the environment
conda activate elm-qa

# Install required packages with specific versions
conda install -c conda-forge \
    numpy=2.2.6 \
    pandas=2.3.2 \
    xarray=2025.9.0 \
    rioxarray=0.19.0 \
    geopandas=1.1.1 \
    shapely=2.1.1 \
    matplotlib=3.10.6 \
    seaborn=0.13.2 \
    tqdm=4.67.1 \
    gitpython=3.1.45

```


### System Requirements
- Python 3.13 (or at least 3.12+)
- Must be run from within an APPN folder structure git repository or with the `--path` argument specified
- Dataset must follow the APPN folder structure (see below)

## Command-Line Arguments

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--path` | str | (git root) | Path to search for QA shapefiles. Defaults to git repository root. |
| `-f, --force` | flag | False | Force re-creation of output files, overwriting existing ones. |
| `--type` | str | csv | Output file format: `csv` or `parquet`. Parquet is more efficient but requires additional dependencies. |
| `-s, --skipplot` | flag | False | Skip plot generation (only extract data tables). |
| `--skip-processing` | flag | False | Skip raster processing; only load existing output files for plotting. |
| `--save-dir` | str | None | Save copies of extracted spectra to this directory for sharing/archiving. |
| `--load-dir` | str | None | Load previously extracted spectra from this directory (e.g., from other nodes). |
| `-v, --verbose` | flag | False | Enable detailed output for debugging. |

## Usage Examples

### Suggested Usage for Sharing files between APPN nodes
This workflow extracts spectra from your local datasets and saves copies to a shared location, making it easy for other nodes to access and combine with their own data.

```bash
python QA00_ELMvaliditation.py --path /path/to/APPNfolderstructure --save-dir /path/to/shared/spectra
```

**Parameters:**
- `--path`: Point to your local APPN dataset root (e.g., `/mnt/d/APPN-42-datastorage/USYD_Narrabri`)
- `--save-dir`: A local directory that can be easily compressed (zip or tar) then shared with other nodes via filesender or globus

This creates standardized spectral files that other nodes can then load using `--load-dir` to combine all data for comprehensive QC analysis.  

### Basic Usage
Run from within the git repository:
```bash
python QA00_ELMvaliditation.py
```

### Specify Custom Path
Search a specific directory:
```bash
python QA00_ELMvaliditation.py --path /path/to/dataset
```

### Extract Only (Skip Plotting)
Create data tables without generating plots:
```bash
python QA00_ELMvaliditation.py -s
```

### Force Regeneration
Overwrite existing output files:
```bash
python QA00_ELMvaliditation.py --force
```

### Use Parquet Format
More efficient for large datasets:
```bash
python QA00_ELMvaliditation.py --type parquet
```

### Save Data for Sharing
Extract spectra and save copies to a central directory:
```bash
python QA00_ELMvaliditation.py --save-dir /path/to/shared/spectra
```

### Load External Data
Combine local data with spectra from other nodes:
```bash
python QA00_ELMvaliditation.py --load-dir /path/to/external/spectra --type csv
```

### Quick Re-plotting
Skip processing, just regenerate plots from existing files:
```bash
python QA00_ELMvaliditation.py --skip-processing
```

## Expected Folder Structure

The script expects the APPN dataset structure:
```
workspace_root/
└── node_name/
    └── project_name/
        └── site_name/
            └── sensor_name/          # e.g., GOBI, CALVIS
                └── YYYYMMDD/         # date folder
                    └── run_name/     # e.g., Run01
                        └── T1_proc/
                            ├── QC_data/
                            │   ├── QC_ELM_Panels.geojson  # Panel file (official naming)
                            │   └── QC_Spectral_Tables/    # Output directory (created by script)
                            └── *.gpro/
                                └── products/
                                    ├── *_VNIR_Orthomosaic.bin
                                    └── *_SWIR_Orthomosaic.bin (CALVIS only)
```

### Required Files
- **Panel File** (GeoJSON preferred, shapefile accepted): Must contain:
  - `geometry` column (polygon geometries)
  - `Panel_ref` column (reference reflectance values, 0-1 scale)
- **Orthomosaic Rasters**: VNIR and SWIR (CALVIS only) `.bin` files

## Output Files

### Spectral Tables
Located in `QC_Spectral_Tables/` subdirectories alongside panel files.

**Filename Format:**
```
{sensor_type}{gpro_num}_{panel_name}_{ortho_name}.{csv|parquet}
```

**Columns:**
- `band`: Band number
- `value`: Extracted reflectance value (0-100 scale for GOBI/CALVIS)
- `Panel_ref`: Reference panel reflectance (0-1 scale)
- `node`, `project`, `site`, `sensor`, `date`, `run`: Metadata fields
- `panel_name`: Name of the QC panel
- `EM_Region`: Electromagnetic region (VNIR or SWIR)
- `gpro_nu`: GoPro/acquisition number (if multiple)

### Visualization Plots
Interactive matplotlib/seaborn plots showing:
- Spectral curves grouped by panel and date
- Separate plots for each sensor, panel type, and EM region
- Error bars showing percentile intervals
- Residual plots (measured - reference)
- Optional bad-band removal views

## Supported Sensors

- **GOBI**: VNIR only
- **CALVIS**: VNIR + SWIR

Bad bands are automatically identified and can be excluded from plots:
- **GOBI VNIR**: Bands 1-5
- **CALVIS VNIR**: Bands 1-5, 170-172
- **CALVIS SWIR**: Bands 1-5, 39-45, 76-89, 130-139

## Workflow

1. **Initialization**: Determine git root or use provided `--path`
2. **Discovery**: Recursively search for QC panel files under `T1_proc/QC_data/`
3. **Validation**: Check panel file structure and locate orthomosaics
4. **Processing**: For each panel:
   - Load panel geometries
   - Clip raster to panel boundaries
   - Extract pixel values with spatial join
   - Save to CSV/Parquet
5. **Loading**: Read extracted spectra from output files
6. **External Data**: Optionally load spectra from `--load-dir`
7. **Sharing**: Optionally save copies to `--save-dir`
8. **Visualization**: Generate plots grouped by sensor/panel/EM region

## Troubleshooting

### Common Issues

**"No QC panel files found"**
- Check that files match the official convention `QC_{ELM|VAL}[_{TargetIdentifier}]_Panels[_{Extra}].geojson` (or `.shp`) and live under `T1_proc/QC_data/`
- Verify you're in the correct directory or provided the right `--path`

**"Panel file does not have expected columns"**
- Ensure `Panel_ref` column exists (case-sensitive) — fix the file, the script does not auto-repair non-standard files

**"No VNIR/SWIR orthomosaic found"**
- Check that `.gpro/products/` folders contain orthomosaic `.bin` files
- Verify file naming: `*_VNIR_Orthomosaic.bin` or `*_SWIR_Orthomosaic.bin`

**"Maximum value falls in the radiance range (0-50 int)"**
- GOBI/CALVIS reflectance values should be either int 0-10000 or float 0-1
- Values between 1 and 50 suggest raw radiance rather than reflectance
- Check if raster preprocessing applied the correct reflectance conversion

**"Could not read output file"**
- Match `--type` argument to existing file format
- Check file permissions
- Re-run with `--force` to regenerate

### Verbose Mode
Use `-v` or `--verbose` for detailed diagnostic output:
```bash
python QA00_ELMvaliditation.py --verbose
```

## Notes

- Processing large rasters may take several minutes per file
- Use `--skip-processing` for faster re-runs when data is already extracted
- Parquet format is recommended for large datasets (faster I/O, smaller files)
- The script automatically handles case-sensitivity issues in older shapefiles
- A breakpoint() is included at the end of plotting for interactive inspection

## Future Enhancements

Planned features (see TODO in code):
- Add reference panel reflectance curves to plots for comparison
- Additional QA metrics and statistics
- Automated outlier detection

## Contact

For questions or issues, contact: arden.burrell@sydney.edu.au
