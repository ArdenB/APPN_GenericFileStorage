"""Provenance metadata for derived data files (YAML sidecars).

Shared by the DS03 plot-extraction scripts (and any other script that
writes derived tables) to record who/what/when/where provenance next to
each output file.
"""

import sys
import pathlib
import getpass
import platform
from datetime import datetime, date
from typing import Dict, Any, Optional

import git
import yaml
import numpy as np
import pandas as pd


# ==================================================================================
def to_yaml_compatible(value: Any) -> Any:
    """Convert Python objects into YAML-serializable primitives.

    Parameters
    ----------
    value : Any
        Input object to convert. Supports nested containers and common
        scientific Python objects (for example ``pathlib.Path``,
        ``pandas.Timestamp``, ``numpy`` scalars/arrays, and
        ``pandas.Series``).

    Returns
    -------
    Any
        YAML-compatible representation of ``value`` where non-serializable
        objects are converted to plain Python primitives.

    Notes
    -----
    Conversion is recursive for dictionaries and sequence-like containers.
    Unsupported objects are converted to ``str`` as a fallback.
    """
    if isinstance(value, pathlib.Path):
        return value.as_posix()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, pd.Series):
        return {str(k): to_yaml_compatible(v) for k, v in value.items()}
    if isinstance(value, dict):
        return {
            str(to_yaml_compatible(k)): to_yaml_compatible(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [to_yaml_compatible(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# ==================================================================================
def build_run_metadata(
        data_dict: Dict[str, Any],
        script_path: str,
        repo: Optional[git.Repo] = None,
    ) -> Dict[str, Any]:
    """Build provenance metadata for one processing run in YAML-safe form.

    Parameters
    ----------
    data_dict : dict of str to Any
        Metadata describing the inputs/outputs of the processing run.
    script_path : str
        The producing script's ``__file__`` (only the name is recorded).
    repo : git.Repo, optional
        Git repository handle used to append repository state. If ``None``,
        git fields are omitted.

    Returns
    -------
    dict of str to Any
        YAML-compatible metadata dictionary containing runtime context,
        system/user information, input metadata, and optional git state.
    """
    metadata: Dict[str, Any] = {
        "script_name": pathlib.Path(script_path).name,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "user": getpass.getuser(),
        "system": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
        },
        "data": to_yaml_compatible(data_dict),
    }

    if repo is not None:
        try:
            try:
                active_branch = repo.active_branch.name
            except TypeError:  # detached HEAD
                active_branch = None
            metadata["git"] = {
                "repo_root": repo.working_tree_dir,
                "commit_hash": repo.head.commit.hexsha,
                "short_hash": repo.git.rev_parse("--short", "HEAD"),
                "branch": active_branch,
                "is_dirty": repo.is_dirty(untracked_files=True),
                "remotes": {
                    remote.name: [url for url in remote.urls]
                    for remote in repo.remotes
                },
            }
        except (git.GitError, ValueError) as exc:
            metadata["git"] = {"error": f"Unable to collect git metadata: {exc}"}

    return to_yaml_compatible(metadata)


# ==================================================================================
def write_metadata_yaml(
        metadata: Dict[str, Any],
        outpath: pathlib.Path,
    ) -> None:
    """Write a metadata dictionary to a YAML sidecar file.

    Parameters
    ----------
    metadata : dict of str to Any
        YAML-compatible metadata (from :func:`build_run_metadata`).
    outpath : pathlib.Path
        Destination ``.yaml`` path.

    Returns
    -------
    None
    """
    with open(outpath, "w", encoding="utf-8") as fh:
        yaml.safe_dump(metadata, fh, sort_keys=False)
