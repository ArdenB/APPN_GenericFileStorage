"""Threshold-config loader for the DS02 QC/QA scripts.

Implements the DS02 threshold pattern (design record: retired QC
pipeline plan §5, git history): all thresholds live
in spec YAMLs under ``reference/thresholds/`` (repo-shipped, never
wiki-fetched — §5d). The loader returns the parsed spec plus the
provenance snapshot (path + sha256) that the reporting contract embeds
in every detail JSON, so a report is only reproducible against the
exact spec file it was graded with.
"""

import hashlib
import pathlib
from typing import Any, Dict, Optional

import git
import yaml


# ==================================================================================
def default_thresholds_dir() -> pathlib.Path:
    """Return the canonical thresholds folder for the enclosing repo.

    Resolves the git root from this file's location and appends
    ``reference/thresholds`` (section 5d layout).

    Returns
    -------
    pathlib.Path
        ``<repo_root>/reference/thresholds`` (not guaranteed to exist).
    """
    repo = git.Repo(pathlib.Path(__file__).resolve().parent,
                    search_parent_directories=True)
    return pathlib.Path(repo.git.rev_parse("--show-toplevel")) / "reference" / "thresholds"


# ==================================================================================
def load_thresholds(
        name: str,
        thresholds_dir: Optional[pathlib.Path] = None,
    ) -> Dict[str, Any]:
    """Load a threshold spec YAML with its provenance snapshot.

    Parameters
    ----------
    name : str
        Spec filename, with or without extension (``"flightcal_spec"``,
        ``"flightcal_spec.yml"``). ``.yml`` then ``.yaml`` are tried when
        no extension is given.
    thresholds_dir : pathlib.Path, optional
        Folder holding the spec YAMLs. Defaults to
        :func:`default_thresholds_dir`.

    Returns
    -------
    dict
        ``{"spec": <parsed YAML>, "path": <posix str>, "sha256": <hex>}``.
        The ``path``/``sha256`` pair is the section-2 config snapshot to
        embed in the detail JSON.

    Raises
    ------
    FileNotFoundError
        If no spec file matches *name* in *thresholds_dir*.
    """
    folder = pathlib.Path(thresholds_dir) if thresholds_dir is not None \
        else default_thresholds_dir()
    if pathlib.Path(name).suffix in (".yml", ".yaml"):
        candidates = [folder / name]
    else:
        candidates = [folder / f"{name}.yml", folder / f"{name}.yaml"]
    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        tried = ", ".join(c.as_posix() for c in candidates)
        raise FileNotFoundError(
            f"No threshold spec for {name!r} — tried: {tried}.")

    raw = path.read_bytes()
    spec = yaml.safe_load(raw)
    return {
        "spec": spec,
        "path": path.as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
