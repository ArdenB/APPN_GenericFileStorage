"""Core functions module for APPN dataset utilities.

This module provides core utility functions for working with APPN dataset
folder structures, including path parsing and metadata extraction.
"""

__version__ = "1.0.0"
__author__ = "Arden Burrell"

from .parse_APPN_dataset_path import parse_APPN_dataset_path
from .outputs_up_to_date import outputs_up_to_date
from .run_palette import run_sort_key, resolve_run_palette
from .reporting import (resolve_qareports_dir, safe_filename_component,
                        markdown_table, scope_label)
from .run_metadata import to_yaml_compatible, build_run_metadata, write_metadata_yaml
from .band_wavelengths import band_wavelengths
from .group_stats import group_value_stats, group_value_percentiles

__all__ = ['parse_APPN_dataset_path', 'outputs_up_to_date',
           'run_sort_key', 'resolve_run_palette',
           'resolve_qareports_dir', 'safe_filename_component',
           'markdown_table', 'scope_label',
           'to_yaml_compatible', 'build_run_metadata', 'write_metadata_yaml',
           'band_wavelengths',
           'group_value_stats', 'group_value_percentiles']
