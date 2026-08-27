"""Collect log/config files from CALVIS and GOBI ``.graw``/``.gpro`` folders.

This one-time script walks a search directory, locates ``.graw`` and
``.gpro`` folders that sit beneath a sensor folder whose name contains
``CALVIS`` or ``GOBI``, and copies a curated set of log/configuration
files into a single output directory so they can be sent for review.

Output layout
-------------
Source folders are grouped by the run they belong to (the ``run_XX``
folder above ``.graw``/``.gpro``). Each group becomes a labelled
folder named ``<SENSOR>_RUN_<NN>`` (e.g. ``CALVIS_RUN_00``,
``GOBI_RUN_03``). Inside the group folder, each source ``.graw`` /
``.gpro`` keeps its original folder name (e.g. ``EM_4cm.graw/``,
``EM_4cm.gpro/``) so that paired ``.graw`` and ``.gpro`` siblings from
the same run land next to each other. Files (with their relative
subpaths inside the original folder) are copied beneath their
respective ``<original>.graw`` / ``<original>.gpro`` subfolder.

A ``manifest.csv`` is written at the root of the output directory.
In addition to identifying each collected folder, it records whether
the source folder follows the APPN folder structure (see
https://github.com/ArdenB/APPN_GenericFileStorage/wiki) using
:func:`parse_APPN_dataset_path`. Columns:

``sensor, kind, group_label, group_number, label, source_path,
output_path, appn_valid, appn_errors, node, project, site_folder, year,
sensor_folder, date, run_folder, run, tier, sub_tier``.

In addition the script also writes:

* ``manifest_graw.csv`` / ``manifest_gpro.csv`` -- per-kind audit
  manifests with one row per source folder and one column per
  expected pattern holding the file count in raw (``0`` means absent
  in raw, ``>=1`` means the collection script should have copied it).
  When ``--missing-report`` is supplied, three extra columns
  (``reported_missing``, ``confirmed_missing_in_raw``,
  ``present_in_raw_not_collected``) cross-check a recipient's
  missing-files report against the raw data.
* ``<group_label>/<source.name>/_contents.txt`` -- a per-folder
  ``Path.rglob('*')`` listing of every entry inside the raw
  ``.graw``/``.gpro`` source folder. Suppress with ``--no-listings``.

Files collected
---------------
From each ``*.graw`` folder:

* ``mission_data.yaml``
* ``targets.yaml``
* ``elm_coefficients.json``
* ``*HP-*/**/settings.txt`` (e.g. ``nHP-929``, ``cAHP-191``)
* ``uVS-*/**/settings.txt``
* ``SBG/**/export_*.txt``
* ``APX-15/**/export_*.txt``

From each ``*.gpro`` folder:

* ``products.yaml``
* ``pipelines/*.yml``
* ``processing_logs/*.txt``

Command line
------------
``--path PATH [PATH ...]``
    One or more root directories to search. May be repeated or given
    as a space-separated list. Defaults to the current git repository
    root. When multiple paths are supplied the results are merged into
    a single output directory with a single ``manifest.csv``.
``--dest PATH``
    Output directory for the collected logs and ``manifest.csv``.
    Defaults to ``<root>/GRYFN_logs`` where ``<root>`` is the first
    ``--path`` if provided, otherwise the git repository root.
``--dry-run``
    List candidate copies without performing them.
``-y``, ``--yes``
    Skip the interactive confirmation prompt and proceed automatically.

The destination directory is automatically excluded from the search so
previously collected log bundles are never re-scanned.
"""

import argparse
import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path
import os
from git import Repo

from tqdm import tqdm

# Make the repository root importable so we can use the shared APPN path
# parser. The script lives in ``Code/OT00_OneTimeScripts``; the repo root
# is two levels above.
repo_root = Repo(".", search_parent_directories=True).working_tree_dir

# _REPO_ROOT = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
    os.chdir(str(repo_root))

from Code.functions.core_functions.parse_APPN_dataset_path import (  # noqa: E402
    parse_APPN_dataset_path,
)

class CollectionConfig:
    """Bundle every configurable constant used by the collection run.

    Holding the patterns, sensor keywords and regexes on a single
    object lets callers pass one ``cfg`` argument around instead of
    relying on module-level globals, which makes the data flow
    explicit and the script easier to test or reuse.

    Attributes
    ----------
    sensor_keywords : tuple of str
        Substrings looked for (case-insensitive) in ancestor folder
        names to decide which sensor a ``.graw``/``.gpro`` belongs to.
    run_folder_re : re.Pattern
        Regex matching APPN run folder names (``run_00``, ``Run-3`` ...).
    graw_patterns, gpro_patterns : list of (str, str, str)
        ``(column_name, subdir_glob, file_glob)`` triples describing
        which files to collect from each ``.graw`` / ``.gpro`` folder.
        An empty ``subdir_glob`` targets the folder root itself.
    report_to_columns : dict of {str: list of str}
        Maps free-form labels from a recipient missing-files report to
        the pattern column names above. Keys are lowercased. A
        reported item is treated as truly missing only when *every*
        mapped column has zero matches in the raw source folder.
    report_line_re : re.Pattern
        Regex matching one line of the recipient missing-files report.
    """

    def __init__(self):
        self.sensor_keywords = ('CALVIS', 'GOBI')
        self.run_folder_re = re.compile(r'^run[_\-\.]?\d+$', re.IGNORECASE)
        self.graw_patterns = [
            ('mission_data.yaml',     '',         'mission_data.yaml'),
            ('targets.yaml',          '',         'targets.yaml'),
            ('elm_coefficients.json', '',         'elm_coefficients.json'),
            ('nHP/cAHP_settings.txt', '*HP-*',    '**/settings.txt'),
            ('uVS_settings.txt',      'uVS-*',    '**/settings.txt'),
            ('SBG_export.txt',        'SBG',      '**/export_*.txt'),
            ('APX-15_export.txt',     'APX-15',   '**/export_*.txt'),
        ]
        self.gpro_patterns = [
            ('products.yaml',         '',                 'products.yaml'),
            ('pipelines_yml',         'pipelines',        '*.yml'),
            ('processing_logs_txt',   'processing_logs',  '*.txt'),
        ]
        self.report_to_columns = {
            'mission_data.yaml':                              ['mission_data.yaml'],
            'targets.yaml':                                   ['targets.yaml'],
            'elm_coefficients.json':                          ['elm_coefficients.json'],
            'nhp-###/**/settings.txt':                        ['nHP/cAHP_settings.txt'],
            'uvs-###/**/settings.txt':                        ['uVS_settings.txt'],
            'sbg/**/export_*.txt or apx-15/**/export_*.txt':  ['SBG_export.txt',
                                                               'APX-15_export.txt'],
            'products.yaml':                                  ['products.yaml'],
            'pipelines/*.yml':                                ['pipelines_yml'],
            'processing_logs/*.txt':                          ['processing_logs_txt'],
        }
        self.report_line_re = re.compile(
            r'^Missing files in \.(?P<kind>graw|gpro) folder:\s*'
            r'(?P<path>.+?)\s+Missing:\s*(?P<items>.+)$'
        )

    def patterns_for(self, kind):
        """Return the pattern list for ``'graw'`` or ``'gpro'``.

        Parameters
        ----------
        kind : str
            Either ``'graw'`` or ``'gpro'``.

        Returns
        -------
        list of (str, str, str)
            The matching ``(column, subdir_glob, file_glob)`` list.
        """
        return self.graw_patterns if kind == 'graw' else self.gpro_patterns


def main():
    """Run the log-collection workflow.

    Parses command-line arguments, scans one or more search roots for
    CALVIS/GOBI ``.graw`` and ``.gpro`` folders, builds a copy plan,
    prompts for confirmation (unless ``--dry-run`` is set) and copies
    the curated log/config files to the destination, writing a
    ``manifest.csv`` alongside.

    Returns
    -------
    None
        Side-effecting entry point. Exits the process via
        :func:`sys.exit` on argument or filesystem errors.
    """
    args = _parse_args()
    _print_banner()

    cfg = CollectionConfig()

    root_paths = _resolve_search_roots(args.path)
    dest_root = _resolve_dest_root(args.dest, root_paths)
    print(f"Destination: {dest_root}")

    folders = _scan_for_target_folders(root_paths, dest_root, cfg)
    if not folders:
        print("No matching .graw/.gpro folders found.")
        return

    _annotate_folders(folders, dest_root, cfg)
    _print_scan_summary(folders)

    copies = _build_copy_plan(folders, cfg)
    if not copies:
        print("No matching files found inside any folders.")
        return

    _print_copy_preview(copies)

    if args.dry_run:
        _print_dry_run_summary(folders)
        return

    if not args.yes and not _confirm(f"\nProceed with copying {len(copies)} file(s) "
                                       f"to {dest_root}? (y/N): "):
        print("Operation cancelled.")
        return

    dest_root.mkdir(parents=True, exist_ok=True)
    success_count, failure_count = _execute_copies(copies)

    manifest_path = dest_root / 'manifest.csv'
    write_manifest(manifest_path, folders)

    # Per-source rglob('*') listings so reviewers can see exactly what
    # lives inside each .graw / .gpro folder in the raw data.
    listing_count = 0
    if not args.no_listings:
        listing_count = _write_folder_listings(folders, dest_root)

    # Audit each folder against the expected patterns and write the
    # per-kind manifests, optionally cross-checked against a recipient
    # missing-files report.
    missing_map = {}
    if args.missing_report:
        report_path = Path(args.missing_report).expanduser().resolve()
        missing_map = _parse_missing_report(report_path, cfg)
        print(f"\nParsed {len(missing_map)} entries from "
              f"missing-report {report_path}")

    audit_rows = _audit_folders(folders, missing_map, cfg)
    graw_manifest = dest_root / 'manifest_graw.csv'
    gpro_manifest = dest_root / 'manifest_gpro.csv'
    _write_audit_manifest(graw_manifest, audit_rows, 'graw', cfg.graw_patterns)
    _write_audit_manifest(gpro_manifest, audit_rows, 'gpro', cfg.gpro_patterns)
    _print_audit_summary(audit_rows, missing_map, cfg)

    _print_completion(success_count, failure_count, dest_root, manifest_path,
                      graw_manifest, gpro_manifest, listing_count)


def _parse_args():
    """Parse command-line arguments for :func:`main`.

    Returns
    -------
    argparse.Namespace
        Parsed arguments with attributes ``path``, ``dest`` and
        ``dry_run``.
    """
    parser = argparse.ArgumentParser(
        description=("Collect log/config files from CALVIS and GOBI "
                     ".graw and .gpro folders into a single directory."),
    )
    parser.add_argument('--path', type=str, default=None, nargs='+',
                        action='extend',
                        help=('One or more root directories to search. '
                              'May be repeated. Defaults to the current '
                              'git repository root.'))
    parser.add_argument('--dest', type=str, default=None,
                        help=('Output directory for collected logs. '
                              'Defaults to <root>/GRYFN_logs.'))
    parser.add_argument('--dry-run', action='store_true',
                        help='List what would be copied without copying.')
    parser.add_argument('-y', '--yes', action='store_true',
                        help='Skip confirmation prompt and proceed automatically.')
    parser.add_argument('--missing-report', type=str, default=None,
                        help=('Optional text file containing a recipient '
                              "missing-files list. Each line: "
                              "'Missing files in .<kind> folder: <path>  "
                              "Missing: a, b, c'. Used to populate the "
                              'cross-check columns in manifest_graw.csv / '
                              'manifest_gpro.csv.'))
    parser.add_argument('--no-listings', action='store_true',
                        help=('Skip writing per-folder _contents.txt rglob '
                              'listings of every .graw/.gpro source folder.'))
    return parser.parse_args()


def _print_banner():
    """Print the script banner to stdout."""
    print("=" * 60)
    print("Collect CALVIS / GOBI logs from .graw and .gpro folders")
    print("=" * 60)


def _resolve_search_roots(raw_paths):
    """Resolve and de-duplicate the user's ``--path`` argument.

    Parameters
    ----------
    raw_paths : list of str or None
        Raw values from ``args.path``. If empty/``None`` the function
        falls back to the git repository root.

    Returns
    -------
    list of pathlib.Path
        Existing, resolved, de-duplicated search roots in the order
        the user provided them.
    """
    if raw_paths:
        roots = []
        for raw in raw_paths:
            p = Path(raw).expanduser().resolve()
            if not p.is_dir():
                print(f"Error: Search path is not a directory: {p}")
                sys.exit(1)
            roots.append(p)
        seen = set()
        roots = [p for p in roots if not (p in seen or seen.add(p))]
        print("Search paths (provided):")
        for p in roots:
            print(f"  {p}")
        return roots

    git_root = get_git_root()
    if git_root is None:
        sys.exit(1)
    print(f"Search path (git root): {git_root}")
    return [git_root]


def _resolve_dest_root(raw_dest, root_paths):
    """Determine the output directory.

    Parameters
    ----------
    raw_dest : str or None
        Raw value from ``args.dest``.
    root_paths : list of pathlib.Path
        Resolved search roots; the first is used to build the default
        when ``raw_dest`` is ``None``.

    Returns
    -------
    pathlib.Path
        Resolved destination directory (not necessarily existing yet).
    """
    if raw_dest is not None:
        return Path(raw_dest).expanduser().resolve()
    return root_paths[0] / 'GRYFN_logs'


def _scan_for_target_folders(root_paths, dest_root, cfg):
    """Scan every search root for ``.graw``/``.gpro`` folders.

    Records found across multiple roots are de-duplicated by their
    resolved source path so the same folder is never collected twice.

    Parameters
    ----------
    root_paths : list of pathlib.Path
        Search roots to walk.
    dest_root : pathlib.Path
        Output directory; excluded from the walk so previously
        collected logs are never re-scanned.
    cfg : CollectionConfig
        Run configuration providing the sensor keywords used to
        identify CALVIS/GOBI ancestors.

    Returns
    -------
    list of dict
        Folder records as produced by :func:`find_target_folders`,
        each annotated with a ``search_root`` key naming the root that
        discovered it.
    """
    exclude = frozenset([dest_root.resolve()])
    print("\nSearching for CALVIS/GOBI '.graw' and '.gpro' folders...")
    folders = []
    seen_sources = set()
    for root_path in root_paths:
        print(f"  scanning {root_path} ...")
        for rec in find_target_folders(root_path, cfg, exclude):
            src = rec['source'].resolve()
            if src in seen_sources:
                continue
            seen_sources.add(src)
            rec['search_root'] = root_path
            folders.append(rec)
    return folders


def _annotate_folders(folders, dest_root, cfg):
    """Sort, group, label and APPN-validate each folder record in-place.

    ``.graw`` and ``.gpro`` folders that share a run (the ``run_XX``
    folder two levels above the source) are placed in the same group
    folder named ``<SENSOR>_RUN_<NN>``. Within a group, each source
    folder keeps its original folder name (e.g. ``EM_4cm.graw``) so
    paired ``.graw``/``.gpro`` siblings land next to each other.

    Each record gains the keys ``group_key``, ``group_number``,
    ``group_label``, ``label``, ``out_dir`` and ``appn``. Group
    numbers are zero-padded to a width of at least 2 digits (or wider
    if needed for the largest sensor bucket).

    Parameters
    ----------
    folders : list of dict
        Folder records produced by :func:`_scan_for_target_folders`.
    dest_root : pathlib.Path
        Output directory used to compute each record's ``out_dir``.
    cfg : CollectionConfig
        Run configuration providing the run-folder regex.

    Returns
    -------
    None
        The records are mutated in place.
    """
    # Compute a stable group key per record (the run folder if we can
    # find it, otherwise the immediate parent folder).
    for rec in folders:
        rec['group_key'] = _run_group_key(rec['source'], cfg)

    folders.sort(key=lambda r: (r['sensor'], str(r['group_key']),
                                r['kind'], str(r['source'])))

    # Assign group numbers per sensor in first-seen order.
    group_numbers = {}        # (sensor, group_key) -> number
    sensor_counters = {}      # sensor -> next number
    for rec in folders:
        key = (rec['sensor'], rec['group_key'])
        if key not in group_numbers:
            group_numbers[key] = sensor_counters.get(rec['sensor'], 0)
            sensor_counters[rec['sensor']] = group_numbers[key] + 1
        rec['group_number'] = group_numbers[key]

    max_count = max(sensor_counters.values()) if sensor_counters else 1
    pad = max(2, len(str(max_count - 1)))
    for rec in folders:
        rec['group_label'] = (
            f"{rec['sensor']}_RUN_{rec['group_number']:0{pad}d}"
        )
        # Inside the group folder, keep the original folder name
        # (which already carries the .graw/.gpro extension) so paired
        # siblings group visually.
        rec['label'] = f"{rec['group_label']}/{rec['source'].name}"
        rec['out_dir'] = dest_root / rec['group_label'] / rec['source'].name
        rec['appn'] = validate_appn_path(rec['source'])


def _run_group_key(source, cfg):
    """Return a grouping key identifying the run a folder belongs to.

    The key is the resolved path of the nearest ancestor folder whose
    name looks like an APPN run folder (matches ``cfg.run_folder_re``,
    case-insensitive — accepts ``run_00``, ``run00``, ``Run-3`` etc.).
    Sibling ``.graw`` and ``.gpro`` folders from the same run share
    that ancestor and therefore the same key.

    If no run-shaped ancestor is found the source's *own* resolved
    path is returned, which means the folder ends up in a group of its
    own (no false pairing with unrelated folders).

    Parameters
    ----------
    source : pathlib.Path
        Path to a ``.graw`` or ``.gpro`` folder.
    cfg : CollectionConfig
        Run configuration providing ``run_folder_re``.

    Returns
    -------
    pathlib.Path
        Resolved path used as the group key. Either an ancestor run
        folder, or the source path itself when no run folder is found.
    """
    resolved = source.resolve()
    # Search a small window of ancestors. The canonical layout is
    # .../<run>/<tier>/<X.graw>, so the run is parents[1]; we allow a
    # couple of extra levels for slight schema drift.
    for ancestor in resolved.parents[:5]:
        if cfg.run_folder_re.match(ancestor.name):
            return ancestor
    return resolved


def _print_scan_summary(folders):
    """Print a count of discovered folders, broken down by kind.

    Parameters
    ----------
    folders : list of dict
        Annotated folder records (must have ``kind`` and ``appn`` keys).
    """
    n_invalid = sum(1 for r in folders if not r['appn'].get('valid'))
    if n_invalid:
        print(f"  WARNING: {n_invalid} folder(s) do not match the "
              "APPN folder structure (see manifest.csv).")

    n_graw = sum(1 for r in folders if r['kind'] == 'graw')
    n_gpro = sum(1 for r in folders if r['kind'] == 'gpro')
    print(f"  .graw folders: {n_graw}")
    print(f"  .gpro folders: {n_gpro}")


def _build_copy_plan(folders, cfg):
    """Build the full ``(src, dst)`` list for every folder record.

    Parameters
    ----------
    folders : list of dict
        Annotated folder records.
    cfg : CollectionConfig
        Run configuration providing the per-kind pattern lists.

    Returns
    -------
    list of tuple of (pathlib.Path, pathlib.Path)
        Concatenated ``(src, dst)`` pairs across every record.
    """
    print("\nCollecting file list...")
    copies = []
    for rec in folders:
        patterns = cfg.patterns_for(rec['kind'])
        copies.extend(collect_from_folder(rec['source'], rec['out_dir'],
                                          patterns))
    return copies


def _print_copy_preview(copies, limit=20):
    """Print up to ``limit`` planned copy pairs.

    Parameters
    ----------
    copies : list of tuple of (pathlib.Path, pathlib.Path)
        Planned ``(src, dst)`` copy pairs.
    limit : int, optional
        Maximum number of pairs to display. Default is 20.
    """
    print(f"Found {len(copies)} file(s) to copy.")
    for src, dst in copies[:limit]:
        print(f"  - {src}")
        print(f"      -> {dst}")
    if len(copies) > limit:
        print(f"  ... ({len(copies) - limit} more)")


def _print_dry_run_summary(folders):
    """Print the per-folder APPN validation result for a dry run.

    Parameters
    ----------
    folders : list of dict
        Annotated folder records.
    """
    print("\nDry run requested. No changes made.")
    for rec in folders:
        status = 'OK' if rec['appn'].get('valid') else 'INVALID'
        print(f"  [{status}] {rec['label']}: {rec['source']}")
        for err in rec['appn'].get('errors') or []:
            print(f"      - {err}")


def _confirm(prompt):
    """Prompt the user for a y/N confirmation.

    Parameters
    ----------
    prompt : str
        Prompt text shown to the user.

    Returns
    -------
    bool
        ``True`` if the user answered ``y`` or ``yes``
        (case-insensitive), ``False`` otherwise.
    """
    return input(prompt).strip().lower() in ('y', 'yes')


def _execute_copies(copies):
    """Copy every planned file with a progress bar.

    Parameters
    ----------
    copies : list of tuple of (pathlib.Path, pathlib.Path)
        Planned ``(src, dst)`` copy pairs.

    Returns
    -------
    success_count : int
        Number of files copied successfully.
    failure_count : int
        Number of files that failed to copy.
    """
    print("\nCopying files...")
    success_count = 0
    failure_count = 0
    progress = tqdm(copies, desc="Copying", unit="file")
    for src, dst in progress:
        progress.set_postfix_str(src.name)
        ok, message = copy_file(src, dst)
        if ok:
            success_count += 1
        else:
            failure_count += 1
            tqdm.write(f"\u2717 {message}")
    return success_count, failure_count


def _print_completion(success_count, failure_count, dest_root, manifest_path,
                      graw_manifest, gpro_manifest, listing_count):
    """Print the final completion summary block.

    Parameters
    ----------
    success_count, failure_count : int
        Copy success / failure counts.
    dest_root : pathlib.Path
        Output directory.
    manifest_path : pathlib.Path
        Path to the primary manifest CSV.
    graw_manifest, gpro_manifest : pathlib.Path
        Paths to the per-kind audit manifests.
    listing_count : int
        Number of ``_contents.txt`` listing files written (``0`` when
        ``--no-listings`` is set).
    """
    print("\n" + "=" * 60)
    print("Copy completed!")
    print(f"Successfully copied: {success_count}")
    print(f"Failed: {failure_count}")
    print(f"Output directory:    {dest_root}")
    print(f"Manifest:            {manifest_path}")
    print(f"Graw audit manifest: {graw_manifest}")
    print(f"Gpro audit manifest: {gpro_manifest}")
    if listing_count:
        print(f"Per-folder listings: {listing_count} _contents.txt files")
    print("=" * 60)


def get_git_root():
    """Return the root directory of the current git repository.

    Runs ``git rev-parse --show-toplevel`` in the current working
    directory.

    Returns
    -------
    pathlib.Path or None
        Resolved path to the repository root, or ``None`` if the
        current directory is not inside a git repository or the
        ``git`` executable is not available. An error message is
        printed to stdout in the failure cases.
    """
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True, text=True, check=True,
        )
        return Path(result.stdout.strip())
    except subprocess.CalledProcessError:
        print("Error: Not in a git repository or git not available")
        return None
    except FileNotFoundError:
        print("Error: Git command not found")
        return None


def has_sensor_ancestor(path, cfg):
    """Return the matched sensor keyword for ``path``, or ``None``.

    Walks ``path``'s ancestors and returns the first keyword from
    ``cfg.sensor_keywords`` found in an ancestor folder name
    (case-insensitive).

    Parameters
    ----------
    path : pathlib.Path
        Path whose ancestors should be inspected.
    cfg : CollectionConfig
        Run configuration providing ``sensor_keywords``.

    Returns
    -------
    str or None
        The matched sensor keyword (e.g. ``'CALVIS'`` or ``'GOBI'``),
        or ``None`` if no ancestor folder name contains a known sensor
        keyword.
    """
    for parent in path.parents:
        upper = parent.name.upper()
        for keyword in cfg.sensor_keywords:
            if keyword in upper:
                return keyword
    return None


def find_target_folders(root_path, cfg, exclude_paths=frozenset()):
    """Find ``.graw`` and ``.gpro`` folders under CALVIS/GOBI sensors.

    Uses :func:`os.walk` and prunes traversal once a target folder is
    found (so the potentially huge contents of ``.graw``/``.gpro`` are
    never descended into) and skips Synology metadata folders.

    Parameters
    ----------
    root_path : pathlib.Path
        Directory to scan recursively.
    cfg : CollectionConfig
        Run configuration providing the sensor keywords.
    exclude_paths : collections.abc.Container of pathlib.Path, optional
        Resolved paths to skip during traversal.

    Returns
    -------
    list of dict
        One record per discovered target folder. Each record has keys:

        - ``sensor`` (str): sensor keyword from ``cfg.sensor_keywords``
          matched in an ancestor folder name.
        - ``kind`` (str): either ``'graw'`` or ``'gpro'``.
        - ``source`` (:class:`pathlib.Path`): absolute path to the
          discovered folder.
    """
    import os

    records = []
    skip_dirs = {'@eaDir', '.git', '#recycle', 'Vault'}
    for dirpath, dirnames, _ in os.walk(root_path):
        # Prune obvious noise and excluded directories.
        dirnames[:] = [
            d for d in dirnames
            if d not in skip_dirs
            and Path(dirpath, d).resolve() not in exclude_paths
        ]

        keep = []
        for name in dirnames:
            current = Path(dirpath) / name
            if name.endswith('.graw'):
                kind = 'graw'
            elif name.endswith('.gpro'):
                kind = 'gpro'
            else:
                keep.append(name)
                continue
            sensor = has_sensor_ancestor(current, cfg)
            if sensor is not None:
                records.append({'sensor': sensor, 'kind': kind,
                                'source': current})
            # Do not descend into .graw / .gpro folders.
        dirnames[:] = keep
    return records


def collect_from_folder(folder, out_dir, patterns):
    """Build ``(src, dst)`` pairs for files inside ``folder``.

    For each ``(subdir_glob, file_glob)`` pattern, expands
    ``subdir_glob`` against ``folder`` (an empty string means
    ``folder`` itself), then matches ``file_glob`` against each
    resulting subdirectory and emits a copy pair preserving the
    relative path inside ``folder``.

    Parameters
    ----------
    folder : pathlib.Path
        A ``.graw`` or ``.gpro`` folder.
    out_dir : pathlib.Path
        Labelled output folder for this source folder.
    patterns : list of tuple of (str, str)
        ``(subdir_glob, file_glob)`` pairs to look for inside
        ``folder``. An empty ``subdir_glob`` targets ``folder``
        itself.

    Returns
    -------
    list of tuple of (pathlib.Path, pathlib.Path)
        ``(src, dst)`` pairs ready to pass to :func:`copy_file`.
    """
    pairs = []
    for _col, subdir_glob, file_glob in patterns:
        if subdir_glob == '':
            sub_iter = [folder]
        else:
            sub_iter = [p for p in folder.glob(subdir_glob) if p.is_dir()]
        for sub in sub_iter:
            for src in sub.glob(file_glob):
                if not src.is_file():
                    continue
                try:
                    rel_in_folder = src.relative_to(folder)
                except ValueError:
                    rel_in_folder = Path(src.name)
                dst = out_dir / rel_in_folder
                pairs.append((src, dst))
    return pairs


def copy_file(src, dst):
    """Copy ``src`` to ``dst`` creating parent directories as needed.

    Uses :func:`shutil.copy2` so file metadata (mtime, permissions) is
    preserved.

    Parameters
    ----------
    src : pathlib.Path
        Source file to copy.
    dst : pathlib.Path
        Destination path. Parent directories are created if missing.

    Returns
    -------
    ok : bool
        ``True`` if the copy succeeded, ``False`` otherwise.
    message : str
        Human-readable status string suitable for logging.
    """
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True, f"Copied: {src} -> {dst}"
    except (OSError, shutil.Error) as exc:
        return False, f"Failed to copy {src} -> {dst}: {exc}"


def validate_appn_path(folder):
    """Validate a ``.graw``/``.gpro`` folder against the APPN structure.

    Wraps :func:`parse_APPN_dataset_path` with ``path_level="sub_tier"``
    and converts any unexpected exception into an invalid result rather
    than aborting the run. A ``.graw``/``.gpro`` folder is expected to
    sit at the ``sub_tier`` level
    (``.../<sensor>/<YYYYMMDD>/run_XX/<T0_raw|T1_proc>/<X.graw>``).

    Parameters
    ----------
    folder : pathlib.Path
        Source folder to validate.

    Returns
    -------
    dict
        Result dictionary with at least the following keys:

        - ``valid`` (bool): whether the path matches the APPN layout.
        - ``errors`` (list of str): human-readable error messages.
        - parsed metadata fields (``node``, ``project``,
          ``site_folder``, ``year``, ``sensor``, ``date``,
          ``run_folder``, ``run``, ``tier``, ``sub_tier``).
    """
    try:
        parsed = parse_APPN_dataset_path(folder, path_level="sub_tier")
    except Exception as exc:  # noqa: BLE001 - report any parser failure
        return {
            'valid': False,
            'errors': [f'parse_APPN_dataset_path raised {type(exc).__name__}: {exc}'],
        }
    return parsed


def _fmt_appn(value):
    """Format a parsed APPN value for inclusion in the CSV.

    Parameters
    ----------
    value : object
        Value from a :func:`parse_APPN_dataset_path` result. May be
        ``None``, a :class:`pandas.Timestamp`, or any object with a
        meaningful ``str()`` representation.

    Returns
    -------
    str
        Empty string for ``None``; ``YYYYMMDD`` for date-like values
        with a ``strftime`` method; ``str(value)`` otherwise.
    """
    if value is None:
        return ''
    # pandas Timestamp has isoformat / strftime; fall back to str.
    try:
        # pd.Timestamp
        return value.strftime('%Y%m%d')
    except AttributeError:
        return str(value)


def write_manifest(manifest_path, folders):
    """Write the manifest CSV mapping labels to source/output paths.

    Each row records identification fields, source and destination
    paths, the APPN-validation outcome and parsed metadata. See the
    module docstring for the full column list.

    Parameters
    ----------
    manifest_path : pathlib.Path
        Output CSV path. Parent directories are created if missing.
    folders : list of dict
        Folder records as produced by :func:`find_target_folders` and
        further annotated in :func:`main` (``number``, ``label``,
        ``out_dir``, ``appn``).

    Returns
    -------
    None
        Writes the manifest to disk as a side effect.
    """
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        'sensor', 'kind', 'group_label', 'group_number', 'label',
        'source_path', 'output_path',
        'appn_valid', 'appn_errors',
        'node', 'project', 'site_folder', 'year', 'sensor_folder',
        'date', 'run_folder', 'run', 'tier', 'sub_tier',
    ]
    with open(manifest_path, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for rec in folders:
            appn = rec.get('appn', {}) or {}
            errors = appn.get('errors') or []
            writer.writerow([
                rec['sensor'], rec['kind'],
                rec['group_label'], rec['group_number'], rec['label'],
                str(rec['source']), str(rec['out_dir']),
                bool(appn.get('valid')),
                ' | '.join(errors),
                appn.get('node') or '',
                appn.get('project') or '',
                appn.get('site_folder') or '',
                _fmt_appn(appn.get('year')),
                appn.get('sensor') or '',
                _fmt_appn(appn.get('date')),
                appn.get('run_folder') or '',
                _fmt_appn(appn.get('run')),
                appn.get('tier') or '',
                appn.get('sub_tier') or '',
            ])

def _write_folder_listings(folders, dest_root):
    """Write a ``_contents.txt`` rglob('*') listing per source folder.

    The listing files are placed at the root of each output folder
    (``<dest_root>/<group_label>/<source.name>/_contents.txt``) and
    contain one POSIX-style relative path per line so reviewers can
    see exactly what lives inside the raw ``.graw`` / ``.gpro``
    folder. A trailing ``/`` marks directories.

    Parameters
    ----------
    folders : list of dict
        Annotated folder records (must have ``source`` and ``out_dir``).
    dest_root : pathlib.Path
        Output directory (used only for the introductory log line).

    Returns
    -------
    int
        Number of listing files successfully written.
    """
    print(f"\nWriting per-folder _contents.txt rglob listings under {dest_root}...")
    written = 0
    for rec in tqdm(folders, desc="Listing", unit="folder"):
        src = rec['source']
        if not src.is_dir():
            continue
        out_file = rec['out_dir'] / '_contents.txt'
        out_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            entries = sorted(src.rglob('*'))
        except OSError as exc:
            tqdm.write(f"\u2717 listing {src}: {exc}")
            continue
        with open(out_file, 'w', encoding='utf-8') as fh:
            fh.write(f"# rglob('*') listing of {src}\n")
            fh.write(f"# total entries: {len(entries)}\n")
            for sub in entries:
                try:
                    rel = sub.relative_to(src).as_posix()
                except ValueError:
                    rel = sub.as_posix()
                suffix = '/' if sub.is_dir() else ''
                fh.write(f"{rel}{suffix}\n")
        written += 1
    return written


def _parse_missing_report(path, cfg):
    """Parse a recipient missing-files report into a label-keyed dict.

    Each line is expected in the form::

        Missing files in .<kind> folder: <abs path>  Missing: a, b, c

    The path may use Windows or POSIX separators. The last two path
    components (``GROUP_LABEL/<source folder name>``) are used as the
    lookup key so it can be matched against each record's ``label``.

    Parameters
    ----------
    path : pathlib.Path
        Text file containing the report.
    cfg : CollectionConfig
        Run configuration providing ``report_line_re``.

    Returns
    -------
    dict of {str: list of str}
        ``{label_key_lower: [reported_item, ...]}``.
    """
    from pathlib import PureWindowsPath, PurePosixPath
    if not path.is_file():
        print(f"Warning: missing-report file not found: {path}")
        return {}
    out = {}
    with open(path, encoding='utf-8') as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            m = cfg.report_line_re.match(line)
            if not m:
                print(f"  skipped unparsable line: {line[:100]}")
                continue
            raw_path = m.group('path').strip()
            if '\\' in raw_path:
                parts = PureWindowsPath(raw_path).parts
            else:
                parts = PurePosixPath(raw_path).parts
            if len(parts) < 2:
                continue
            key = f'{parts[-2]}/{parts[-1]}'.lower()
            items = [s.strip() for s in m.group('items').split(',') if s.strip()]
            out[key] = items
    return out


def _scan_folder_counts(folder, patterns):
    """Return ``{column: file_count}`` matching each pattern in ``folder``.

    Parameters
    ----------
    folder : pathlib.Path
        Source ``.graw`` or ``.gpro`` folder.
    patterns : list of (str, str, str)
        ``(column, subdir_glob, file_glob)`` triples.

    Returns
    -------
    dict of {str: int}
        File count per pattern column. ``0`` means the file is absent
        in raw.
    """
    counts = {col: 0 for col, _, _ in patterns}
    if not folder.is_dir():
        return counts
    for col, subdir_glob, file_glob in patterns:
        sub_iter = ([folder] if subdir_glob == ''
                    else [p for p in folder.glob(subdir_glob) if p.is_dir()])
        n = 0
        for sub in sub_iter:
            try:
                for hit in sub.glob(file_glob):
                    if hit.is_file():
                        n += 1
            except OSError:
                pass
        counts[col] = n
    return counts


def _audit_folders(folders, missing_map, cfg):
    """Build per-folder audit rows with pattern counts and cross-check.

    Each row contains the source identification fields, one count
    column per expected pattern, and three cross-check columns derived
    from ``missing_map`` (the recipient's report):

    * ``reported_missing`` -- raw items the report flagged.
    * ``confirmed_missing_in_raw`` -- reported items that really are
      absent in the raw data (count == 0 across every mapped column).
    * ``present_in_raw_not_collected`` -- reported items that exist in
      raw but were not copied: a code/data bug to investigate.

    Parameters
    ----------
    folders : list of dict
        Annotated folder records.
    missing_map : dict of {str: list of str}
        Output of :func:`_parse_missing_report`. Empty dict disables
        the cross-check (those columns will be blank).
    cfg : CollectionConfig
        Run configuration providing the per-kind patterns and the
        report-label-to-column map.

    Returns
    -------
    list of dict
        One audit row per source folder, in the same order as
        ``folders``.
    """
    print("\nAuditing source folders against expected patterns...")
    rows = []
    for rec in tqdm(folders, desc="Auditing", unit="folder"):
        kind = rec['kind']
        patterns = cfg.patterns_for(kind)
        counts = _scan_folder_counts(rec['source'], patterns)
        row = {
            'kind':          kind,
            'sensor':        rec['sensor'],
            'group_label':   rec['group_label'],
            'label':         rec['label'],
            'source_path':   str(rec['source']),
            'output_path':   str(rec['out_dir']),
            'source_exists': rec['source'].is_dir(),
        }
        for col, _, _ in patterns:
            row[col] = counts[col]
        reported = missing_map.get(rec['label'].lower(), [])
        row['reported_missing'] = '; '.join(reported)
        confirmed, false_positives = [], []
        for item in reported:
            cols = cfg.report_to_columns.get(item.strip().lower())
            if not cols:
                false_positives.append(f'?{item}')
                continue
            raw_total = sum(counts.get(c, 0) for c in cols)
            if raw_total == 0:
                confirmed.append(item)
            else:
                false_positives.append(
                    f'{item} (raw has {raw_total} file(s) in '
                    f'{"/".join(cols)})')
        row['confirmed_missing_in_raw'] = '; '.join(confirmed)
        row['present_in_raw_not_collected'] = '; '.join(false_positives)
        rows.append(row)
    return rows


def _write_audit_manifest(path, audit_rows, kind, patterns):
    """Write the per-kind audit manifest CSV (``manifest_<kind>.csv``).

    Parameters
    ----------
    path : pathlib.Path
        Output CSV path.
    audit_rows : list of dict
        All audit rows produced by :func:`_audit_folders`.
    kind : str
        Either ``'graw'`` or ``'gpro'``; rows are filtered by this.
    patterns : list of (str, str, str)
        Pattern definitions for ``kind``; only their column names are
        used here.
    """
    pattern_cols = [c for c, _, _ in patterns]
    header = (['sensor', 'group_label', 'label', 'source_path',
               'output_path', 'source_exists']
              + pattern_cols
              + ['reported_missing', 'confirmed_missing_in_raw',
                 'present_in_raw_not_collected'])
    rows = [r for r in audit_rows if r['kind'] == kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, '') for k in header})


def _print_audit_summary(audit_rows, missing_map, cfg):
    """Print a per-pattern absence summary and any report discrepancies.

    Parameters
    ----------
    audit_rows : list of dict
        All audit rows produced by :func:`_audit_folders`.
    missing_map : dict
        Parsed recipient report (may be empty).
    cfg : CollectionConfig
        Run configuration providing the per-kind pattern lists.
    """
    for kind, patterns in (('graw', cfg.graw_patterns),
                           ('gpro', cfg.gpro_patterns)):
        rows = [r for r in audit_rows if r['kind'] == kind]
        if not rows:
            continue
        print(f"\n=== {kind} audit ({len(rows)} folders) ===")
        for col, _, _ in patterns:
            absent = sum(1 for r in rows if r.get(col, 0) == 0)
            print(f"  {col:30s}  absent in raw: {absent:4d} / {len(rows)}")

    if not missing_map:
        return

    matched = sum(1 for r in audit_rows if r.get('reported_missing'))
    unmatched = (set(missing_map.keys())
                 - {r['label'].lower() for r in audit_rows})
    print("\n=== Cross-check vs recipient report ===")
    print(f"Manifest rows that match a report entry: {matched}")
    if unmatched:
        print(f"Report entries with no matching manifest row "
              f"({len(unmatched)}):")
        for k in sorted(unmatched):
            print(f"  - {k}")
    discrepancies = [r for r in audit_rows
                     if r.get('present_in_raw_not_collected')]
    if discrepancies:
        print(f"\nFolders where the report flagged items that DO exist in "
              f"raw (collection script bug?) -- {len(discrepancies)}:")
        for r in discrepancies:
            print(f"  {r['label']}")
            print(f"    -> {r['present_in_raw_not_collected']}")
    else:
        print("\nNo discrepancies: every reported-missing item is also "
              "absent in raw.")


if __name__ == '__main__':
    main()
