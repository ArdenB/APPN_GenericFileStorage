"""Shared QC/QA reporting contract for the DS02 scripts.

One implementation of the DS02 pipeline plan's cross-cutting pieces
(``Code/DS02_DatasetQA/QC_PIPELINE_PLAN.md``):

- status vocabulary + worst-wins collapse (section 3)
- JSON-first report writer + YAML-summary projector (section 2/4)
- report reader tolerant of legacy filenames/schemas (section 6)
- threshold-config loader (section 5/5d)
"""

__version__ = "1.0.0"
__author__ = "Arden Burrell"

from .status import (check_levels, script_levels, collapse, worst,
                     derive_status)
from .report import (schema_version, new_report, add_check, report_paths,
                     summarize, write_report)
from .reader import legacy_report_globs, read_report
from .thresholds import default_thresholds_dir, load_thresholds

__all__ = ['check_levels', 'script_levels', 'collapse', 'worst',
           'derive_status',
           'schema_version', 'new_report', 'add_check', 'report_paths',
           'summarize', 'write_report',
           'legacy_report_globs', 'read_report',
           'default_thresholds_dir', 'load_thresholds']
