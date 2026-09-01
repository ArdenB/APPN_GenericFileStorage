"""Panel DHR reference library access + physical-set identification (§5b).

Resolves the manufacturer DHR curves in ``reference/panels/<NODE>/`` for
the QC02 observed-vs-expected comparison, implementing the
``QC_PIPELINE_PLAN.md`` §5b rules:

1. The gpro pipeline YAML is the primary pin for ELM tables — nominal
   ``Panel_ref`` signatures cannot identify hardware (24005 and 25005
   share the 11/30/56/82 signature but differ by ~3 pp in SWIR).
2. The gpro pin applies to ELM tables only; VAL panels are different
   hardware and resolve by node + signature.
3. Ambiguity is a hard error, never warn-and-pick-newest (the
   manufacture-date tie-break processed a CaliWeek dataset against the
   wrong set: SWIR biases ±4.5 % → ±2 % on fix).
4. There is no cross-node fallback: a signature resolves only within its
   node's folder; a missing node library is ``not_checked``, never a
   borrowed curve. (A gpro pin names exact hardware, so pins resolve
   library-wide by serial.)
"""

import json
import pathlib
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import git
import pandas as pd


# ==================================================================================
def panels_root(repo_root: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Return the canonical panel-library folder for the enclosing repo.

    Parameters
    ----------
    repo_root : pathlib.Path, optional
        Repo root override (defaults to the git root of this file).

    Returns
    -------
    pathlib.Path
        ``<repo_root>/reference/panels`` (not guaranteed to exist).
    """
    if repo_root is None:
        repo = git.Repo(pathlib.Path(__file__).resolve().parent,
                        search_parent_directories=True)
        repo_root = pathlib.Path(repo.git.rev_parse("--show-toplevel"))
    return pathlib.Path(repo_root) / "reference" / "panels"


# ==================================================================================
def node_library_dir(
        node_name: str,
        root: Optional[pathlib.Path] = None,
    ) -> Optional[pathlib.Path]:
    """Map a store node name onto its panel-library folder.

    The library folders are short node codes (``AU``, ``USYD``, ...);
    store node names are longer (``USYD_Narrabri``). The match is the
    longest library code that prefixes the node name, case-insensitive.

    Parameters
    ----------
    node_name : str
        The parsed store node name.
    root : pathlib.Path, optional
        Library root override (defaults to :func:`panels_root`).

    Returns
    -------
    pathlib.Path or None
        The node's library folder, or None when no code matches or the
        library is absent (caller grades ``not_checked``, §5b rule 4).
    """
    root = root if root is not None else panels_root()
    if not root.is_dir() or not node_name:
        return None
    name = str(node_name).upper()
    codes = sorted((d for d in root.iterdir() if d.is_dir()),
                   key=lambda d: len(d.name), reverse=True)
    for code_dir in codes:
        if name.startswith(code_dir.name.upper()):
            return code_dir
    return None


# ==================================================================================
def gpro_panel_set(t1_proc: pathlib.Path) -> Tuple[Optional[str], List[str]]:
    """Read the panel-set serial prefix ELM actually used from the gpro.

    Scans ``<T1_proc>/*.gpro/pipelines/*.yml`` for panel target-file
    references (``.../UF200-24005-11.json``) and extracts the set prefix
    (``UF200-24005``).

    Parameters
    ----------
    t1_proc : pathlib.Path
        The run's ``T1_proc`` folder.

    Returns
    -------
    tuple
        ``(set_key, prefixes)`` — the single pinned prefix (or None when
        zero or multiple were found) plus every prefix seen, so the
        caller can grade the ``panel_set_pinned`` check (multiple
        prefixes = sets crossed over between concurrent flights).
    """
    prefixes: Set[str] = set()
    for pipe in sorted(t1_proc.glob("*.gpro/pipelines/*.yml")):
        text = pipe.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"([A-Z]+\d*-\d+)-\d+\.json", text):
            prefixes.add(m.group(1))
    found = sorted(prefixes)
    return (found[0] if len(found) == 1 else None), found


# ==================================================================================
def resolve_panel_set(
        node_name: str,
        signature: Set[str],
        set_key: Optional[str] = None,
        exclude_key: Optional[str] = None,
        root: Optional[pathlib.Path] = None,
    ) -> Tuple[pathlib.Path, Dict[str, Any]]:
    """Resolve one physical panel-set folder per the §5b rules.

    Resolution order (each rung records its ``method``):

    1. ``pin`` — a gpro pin names exact hardware and resolves
       library-wide by serial.
    2. ``signature`` — the observed nominal codes match exactly one set
       in the node's folder.
    3. ``elimination`` — every node fields two 4-panel sets plus a
       2-panel set, and the gpro identifies the ELM set; excluding it
       leaves exactly one candidate for the VAL set.
    4. ``identical_candidates`` — multiple candidates remain but their
       DHR curves are numerically identical for the needed codes (the
       2024 batch shares one calibration curve), so the choice cannot
       change a verdict; the first (sorted) set is used and every
       candidate serial is recorded.

    Only genuinely differing candidates (e.g. AU's 24005 vs 25005,
    ~3 pp apart in SWIR) remain a hard error — never pick-newest.

    Parameters
    ----------
    node_name : str
        The run's parsed store node name.
    signature : set of str
        Observed nominal ``Panel_ref`` codes (e.g. ``{"11","30","56","82"}``).
    set_key : str, optional
        The gpro pin (``UF200-24005``).
    exclude_key : str, optional
        A set known NOT to be this target (the gpro-pinned ELM set,
        when resolving a VAL target).
    root : pathlib.Path, optional
        Library root override.

    Returns
    -------
    tuple
        ``(set_dir, resolution)`` — the resolved folder plus
        ``{"method", "candidates"}`` provenance for the report.

    Raises
    ------
    FileNotFoundError
        When the pin names a set absent from the library (never fall
        back silently), or the node has no library folder.
    LookupError
        When zero sets match, or multiple *numerically differing* sets
        remain after elimination (§5b rule 3).
    """
    root = root if root is not None else panels_root()
    if set_key is not None:
        hits = sorted(root.glob(f"*/{set_key}"))
        if not hits:
            raise FileNotFoundError(
                f"Panel set {set_key} is pinned by the gpro but absent from "
                f"the library at {root} — refusing to substitute another "
                "set (§5b rule 3).")
        return hits[0], {"method": "pin", "candidates": [hits[0].name]}

    node_dir = node_library_dir(node_name, root=root)
    if node_dir is None:
        raise FileNotFoundError(
            f"No panel library folder for node {node_name!r} under {root} "
            "(no cross-node fallback, §5b).")
    wanted = {str(code) for code in signature}
    matches = [d for d in sorted(node_dir.iterdir()) if d.is_dir()
               and wanted <= _set_codes(d)]
    method = "signature"
    if exclude_key is not None and len(matches) > 1:
        remaining = [d for d in matches if d.name != exclude_key]
        if len(remaining) < len(matches):
            matches, method = remaining, "elimination"
    if len(matches) == 1:
        return matches[0], {"method": method,
                            "candidates": [matches[0].name]}
    if len(matches) > 1 and _candidates_identical(matches, wanted):
        return matches[0], {"method": "identical_candidates",
                            "candidates": [d.name for d in matches]}
    names = ", ".join(d.name for d in matches) or "(none)"
    raise LookupError(
        f"Signature {sorted(wanted)} matches {len(matches)} numerically "
        f"differing set(s) in {node_dir.name}: {names}. Ambiguity is a "
        "hard error — pin the set via the gpro or resolve the library "
        "(§5b rule 3).")


# ==================================================================================
def _candidates_identical(
        candidates: List[pathlib.Path],
        codes: Set[str],
    ) -> bool:
    """True when every candidate's DHR curves match for the needed codes.

    The 2024 fleet batch (24006-24013) shares one calibration curve per
    panel, so nominally-ambiguous candidates are often numerically
    identical and the choice cannot change a verdict.

    Parameters
    ----------
    candidates : list of pathlib.Path
        Matching set folders.
    codes : set of str
        Nominal codes the comparison will actually use.

    Returns
    -------
    bool
        True when all candidates carry identical ``dhr`` arrays for
        every code (files missing anywhere count as differing).
    """
    for code in codes:
        curves = []
        for set_dir in candidates:
            hits = sorted(set_dir.glob(f"*-{code}.json"))
            if not hits:
                return False
            curves.append(json.loads(
                hits[0].read_text(encoding="utf-8"))["dhr"])
        if any(c != curves[0] for c in curves[1:]):
            return False
    return True


# ==================================================================================
def load_panel_dhr(
        set_dir: pathlib.Path,
        panel_ref: str,
    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Load one panel's manufacturer DHR curve from a resolved set.

    Parameters
    ----------
    set_dir : pathlib.Path
        Resolved set folder from :func:`resolve_panel_set`.
    panel_ref : str
        Nominal reflectance code (``"11"``, ``"82"``, ...).

    Returns
    -------
    tuple
        ``(dhr, provenance)`` — DataFrame with ``wavelength_nm`` and
        ``reflectance`` (fraction 0-1, 1 nm grid) plus a provenance dict
        (file, serial, manufacture-date, optional tail-fill marker).

    Raises
    ------
    FileNotFoundError
        When the set has no file for *panel_ref*.
    """
    hits = sorted(set_dir.glob(f"*-{panel_ref}.json"))
    if not hits:
        raise FileNotFoundError(
            f"{set_dir.name} has no panel file for nominal code "
            f"{panel_ref!r} in {set_dir}.")
    ref = json.loads(hits[0].read_text(encoding="utf-8"))
    dhr = pd.DataFrame(ref["dhr"])
    provenance = {
        "file": hits[0].relative_to(panels_root().parent.parent).as_posix()
                if hits[0].is_relative_to(panels_root().parent.parent)
                else str(hits[0]),
        "serial": ref.get("serial", hits[0].stem),
        "manufacture_date": ref.get("manufacture-date"),
        "dhr_tail_provenance": ref.get("dhr_tail_provenance"),
    }
    return dhr, provenance


# ==================================================================================
def _set_codes(set_dir: pathlib.Path) -> Set[str]:
    """Return the nominal codes available in one set folder.

    Parameters
    ----------
    set_dir : pathlib.Path
        A ``UF200-<serial>`` library folder.

    Returns
    -------
    set of str
        Nominal codes parsed from the panel filenames.
    """
    codes = set()
    for f in set_dir.glob("*.json"):
        m = re.search(r"-(\d+)$", f.stem)
        if m:
            codes.add(m.group(1))
    return codes
