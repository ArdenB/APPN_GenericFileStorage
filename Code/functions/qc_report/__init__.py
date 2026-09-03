"""Shared QC/QA reporting contract for the DS02 scripts.

One implementation of the DS02 pipeline's cross-cutting pieces (live
spec: ``Code/DS02_DatasetQA/README.md``; design record: the retired QC
pipeline plan, in this repo's git history — whose §-numbers the
notes below cite):

- status vocabulary + worst-wins collapse (§3)
- JSON-first report writer + YAML-summary projector (§2/§4)
- report reader tolerant of legacy filenames/schemas (§6)
- threshold-config loader (§5/§5d)
- QC_report.md section fragments + assembly (``markdown.py``)
"""

__version__ = "1.1.0"
__author__ = "Arden Burrell"

from .status import (check_levels, script_levels, collapse, worst,
                     derive_status)
from .report import (schema_version, new_report, add_check, report_paths,
                     summarize, write_report)
from .reader import (legacy_report_globs, read_report, report_is_current,
                     version_key)
from .thresholds import default_thresholds_dir, load_thresholds
from .markdown import (update_qc_report, status_glyph, checks_table,
                       figure_embeds, artifact_links)

__all__ = ['check_levels', 'script_levels', 'collapse', 'worst',
           'derive_status',
           'schema_version', 'new_report', 'add_check', 'report_paths',
           'summarize', 'write_report',
           'legacy_report_globs', 'read_report', 'report_is_current',
           'version_key',
           'default_thresholds_dir', 'load_thresholds',
           'update_qc_report', 'status_glyph', 'checks_table',
           'figure_embeds', 'artifact_links']
