"""Parse metadata encoded in an APPN dataset folder structure."""

import re
import pathlib
from typing import Dict, Any, Tuple, Optional

import pandas as pd


def parse_APPN_dataset_path(
        path: pathlib.Path,
        path_level: str = "auto",
    ) -> Dict[str, Any]:
    """Parse metadata encoded in an APPN dataset folder structure.

    Parameters
    ----------
    path : pathlib.Path
        Path to parse. May point to a file or directory at any level in the
        dataset structure.
    path_level : {'auto', 'root', 'node', 'project', 'site', 'sensor', 'date', 'run', 'tier', 'sub_tier'}, optional
        Level represented by ``path``. If ``"auto"`` (default), the function
        attempts to infer the level from the path contents.

    Returns
    -------
    dict of str to Any
        Parsed metadata with keys:

        - ``root`` (str or None)
        - ``node`` (str or None)
        - ``project`` (str or None)
        - ``site_folder`` (str or None)
        - ``site`` (str or None)
        - ``year`` (int or None)
        - ``sensor`` (str or None)
        - ``date`` (pd.Timestamp or None)
        - ``run_folder`` (str or None)
        - ``run`` (int or None)
        - ``tier`` (str or None)
        - ``sub_tier`` (str or None)
        - ``stem`` (str or None) — path relative to ``sub_tier`` when the input is deeper than sub_tier
        - ``valid`` (bool)
        - ``errors`` (list of str)
        - ``path_level`` (str)
        - ``input_path`` (str)

    Raises
    ------
    ValueError
        If ``path_level`` is invalid.

    Notes
    -----
    Parsing is pure string manipulation. Filesystem checks (confirming date
    folders exist at the expected depth for levels above ``date``, and the
    root/node auto-detection glob) only run when ``path`` exists on disk;
    nonexistent paths are validated by name shape alone.
    """
    valid_levels = {"auto", "root", "node", "project", "site", "sensor", "date", "run", "tier", "sub_tier"}
    if path_level not in valid_levels:
        raise ValueError(f"Invalid path_level '{path_level}'. Must be one of {sorted(valid_levels)}")

    pth = pathlib.Path(path)
    # Only strip a trailing component when the path is an existing file.
    # Folders whose name contains a '.' (e.g. ``X.graw``, ``Y.gpro``) must
    # not be treated as files just because their name has a suffix.
    if pth.suffix and pth.is_file():
        pth = pth.parent
    elif pth.suffix and not pth.exists():
        # Path does not exist on disk: fall back to the historical
        # behaviour (treat trailing dotted component as a file suffix)
        # only when the suffix does not look like a known APPN folder
        # extension.
        if pth.suffix.lower() not in {".graw", ".gpro"}:
            pth = pth.parent

    sensor_names = ({
        "GOBI", "HIRES", "M3M", "CALVIS", "PHENOMATE", "MOLE", "TEMS",
        "PTEMS", "MPROBES", "LITERAL", "H30T", "RHIZO", "MAXAR", "JILIN",
        "FIELDOBS", "IRT", "M3T", "SVC", "RGB", "FIELDCAMS", "ITRES",
    })
    date_regex = re.compile(r"^\d{8}$|^\d{4}-\d{2}-\d{2}$")
    run_regex = re.compile(r"^[A-Za-z]+_?(\d+)$")
    tier_regex = re.compile(r"^T\d+_.+$")
    site_regex = re.compile(r"^(\d{4})(.+)$")
    project_regex = re.compile(r"^\d{4}_.+$")

    def _parse_date(name: str) -> pd.Timestamp:
        return pd.to_datetime(name.replace("-", ""), format="%Y%m%d", errors="coerce") # pyright: ignore[reportReturnType]

    def _site_parts(site_folder: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
        if site_folder is None:
            return None, None
        match = site_regex.match(site_folder)
        if match is None:
            return None, site_folder
        return int(match.group(1)), match.group(2)

    def _run_number(run_folder: Optional[str]) -> Optional[int]:
        if run_folder is None:
            return None
        match = run_regex.match(run_folder)
        if match is None:
            return None
        return int(match.group(1))

    parsed: Dict[str, Any] = {
        "root": None,
        "node": None,
        "project": None,
        "site_folder": None,
        "site": None,
        "year": None,
        "sensor": None,
        "date": None,
        "run_folder": None,
        "run": None,
        "tier": None,
        "sub_tier": None,
        "stem": None,
        "valid": True,
        "errors": [],
        "path_level": path_level,
        "input_path": pth.as_posix(),
    }

    _fields_populated = False  # True when auto mode already populated all fields

    # Auto-detect the closest date folder in the path ancestry.
    if path_level == "auto":
        date_node = None
        for candidate in [pth] + list(pth.parents):
            if date_regex.match(candidate.name):
                date_node = candidate
                break

        if date_node is not None:
            path_level = "date"
            parsed["date"] = _parse_date(date_node.name)
            parsed["sensor"] = date_node.parent.name if date_node.parent is not None else None
            parsed["site_folder"] = date_node.parent.parent.name if date_node.parent.parent is not None else None
            parsed["project"] = date_node.parent.parent.parent.name if date_node.parent.parent.parent is not None else None
            parsed["node"] = date_node.parent.parent.parent.parent.name if date_node.parent.parent.parent.parent is not None else None
            parsed["root"] = date_node.parent.parent.parent.parent.parent.as_posix() if date_node.parent.parent.parent.parent.parent is not None else None

            # If path sits below date level, first child is treated as run folder.
            try:
                rel_parts = pth.relative_to(date_node).parts
                if len(rel_parts) > 0:
                    parsed["run_folder"] = rel_parts[0]
                    parsed["run"] = _run_number(parsed["run_folder"])
                if len(rel_parts) > 1 and tier_regex.match(rel_parts[1]):
                    parsed["tier"] = rel_parts[1]
                if len(rel_parts) > 2:
                    parsed["sub_tier"] = rel_parts[2]
                if len(rel_parts) > 3:
                    parsed["stem"] = "/".join(rel_parts[3:])
            except ValueError:
                pass
            _fields_populated = True
        else:
            # Fallback heuristic when no date folder is present.
            # Project (YYYY_Name) is checked before site so '2026_APEx'
            # is classified as project, not site (site_regex is more
            # permissive and would otherwise match first).
            if tier_regex.match(pth.name):
                path_level = "tier"
            elif pth.name in sensor_names:
                path_level = "sensor"
            elif project_regex.match(pth.name):
                path_level = "project"
            elif site_regex.match(pth.name):
                path_level = "site"
            else:
                # Use glob to distinguish 'node' (dates 4 levels deep) from 'root' (dates 5 levels deep).
                # Also accepts tier folders at the corresponding depths as a secondary signal.
                # Hierarchy: root / node / project / site / sensor / YYYYMMDD / run / tier
                # From node:  project / site / sensor / YYYYMMDD = 4 levels → */*/*/????????
                # From root:  node / project / site / sensor / YYYYMMDD = 5 levels → */*/*/*/????????
                _node_match = (
                    any(date_regex.match(p.name) for p in pth.glob("*/*/*/????????") if p.is_dir())
                    or any(date_regex.match(p.name) for p in pth.glob("*/*/*/??????????") if p.is_dir())
                    or any(tier_regex.match(p.name) for p in pth.glob("*/*/*/????????/*/*") if p.is_dir())
                    or any(tier_regex.match(p.name) for p in pth.glob("*/*/*/??????????/*/*") if p.is_dir())
                )
                _root_match = (
                    any(date_regex.match(p.name) for p in pth.glob("*/*/*/*/????????") if p.is_dir())
                    or any(date_regex.match(p.name) for p in pth.glob("*/*/*/*/??????????") if p.is_dir())
                    or any(tier_regex.match(p.name) for p in pth.glob("*/*/*/*/????????/*/*") if p.is_dir())
                    or any(tier_regex.match(p.name) for p in pth.glob("*/*/*/*/??????????/*/*") if p.is_dir())
                )
                if _root_match:
                    path_level = "root"
                elif _node_match:
                    path_level = "node"
                else:
                    # Depth cannot be determined; default to 'node' — validation will flag as invalid.
                    path_level = "root"

    parsed["path_level"] = path_level

    # Ordered field names walking upward from path for each explicit level.
    _level_fields = {
        "sub_tier": ["sub_tier", "tier", "run_folder", "date", "sensor", "site_folder", "project", "node", "root"],
        "tier":     ["tier", "run_folder", "date", "sensor", "site_folder", "project", "node", "root"],
        "run":      ["run_folder", "date", "sensor", "site_folder", "project", "node", "root"],
        "date":     ["date", "sensor", "site_folder", "project", "node", "root"],
        "sensor":   ["sensor", "site_folder", "project", "node", "root"],
        "site":     ["site_folder", "project", "node", "root"],
        "project":  ["project", "node", "root"],
        "node":     ["node", "root"],
        "root":     ["root"],
    }

    if not _fields_populated and path_level in _level_fields:
        # For sub_tier, walk up to find the true anchor (first node whose parent is a tier folder).
        # This correctly handles paths passed at any depth below sub_tier.
        anchor = pth
        if path_level == "sub_tier":
            for candidate in [pth] + list(pth.parents):
                if candidate.parent.name and tier_regex.match(candidate.parent.name):
                    anchor = candidate
                    stem_parts = pth.relative_to(anchor).parts
                    if stem_parts:
                        parsed["stem"] = "/".join(stem_parts)
                    break

        node = anchor
        for field in _level_fields[path_level]:
            if field == "root":
                parsed["root"] = node.as_posix()
            elif field == "date":
                parsed["date"] = _parse_date(node.name)
            else:
                parsed[field] = node.name
            node = node.parent

    parsed["year"], parsed["site"] = _site_parts(parsed["site_folder"])
    if parsed["run"] is None:
        parsed["run"] = _run_number(parsed["run_folder"])

    # ========== Validate predictable fields ==========
    valid = True
    errors = []

    # Date: if populated, must not be NaT
    if parsed["date"] is not None and pd.isna(parsed["date"]):
        valid = False
        errors.append(f"Date field is not a valid date (got '{parsed['date']}').")

    # Run folder: if populated, must match expected pattern (e.g. run_00)
    if parsed["run_folder"] is not None and not run_regex.match(parsed["run_folder"]):
        valid = False
        errors.append(f"Run folder '{parsed['run_folder']}' does not match expected pattern (e.g. 'run_00').")

    # Tier: if populated, must match expected pattern (e.g. T0_raw)
    if parsed["tier"] is not None and not tier_regex.match(parsed["tier"]):
        valid = False
        errors.append(f"Tier folder '{parsed['tier']}' does not match expected pattern (e.g. 'T0_raw').")

    # ----- Hierarchy alignment (sensor / site / project / node) -----
    # Detect missing or inserted layers between the date folder and the
    # repository root, e.g. a project folder placed directly under the
    # node with no site folder in between.
    def _shape(name):
        """Classify a folder name's structural shape."""
        if not name:
            return None
        if tier_regex.match(name):
            return "tier"
        if run_regex.match(name):
            return "run"
        if date_regex.match(name):
            return "date"
        if name in sensor_names:
            return "sensor"
        # Project (YYYY_Name) is checked before site so '2025_Merinda'
        # is classified as project, not site.
        if re.match(r"^\d{4}_.+$", name):
            return "project"
        if re.match(r"^\d{4}[^_].*$", name):
            return "site"
        return "other"

    _hierarchy = [
        ("sensor",      "sensor",  "a sensor folder (e.g. 'GOBI', 'CALVIS')"),
        ("site_folder", "site",    "a site folder (e.g. '2025IAWatson')"),
        ("project",     "project", "a project folder (e.g. '2025_Chickpea')"),
        ("node",        "other",   "a node folder (e.g. 'USYD_Narrabri')"),
    ]
    # When every upper layer is populated (input at or below sensor level)
    # the full chain can be checked for missing/inserted layers. For
    # shallower path_levels only the shape of each populated name can be
    # validated individually.
    _all_populated = all(parsed[lvl] is not None for lvl, _, _ in _hierarchy)
    _populated = [
        (lvl, exp, desc, parsed[lvl], _shape(parsed[lvl]))
        for lvl, exp, desc in _hierarchy if parsed[lvl] is not None
    ]
    _actual = [s for *_, s in _populated]
    _expected = [exp for _, exp, _ in _hierarchy]
    _layout_hint = ("Expected layout: "
                    ".../<node>/<project>/<site>/<sensor>/<date>/<run>/<tier>/<sub_tier>")

    def _seq_match(actual, expected):
        if len(actual) > len(expected):
            return False
        return all(a == b for a, b in zip(actual, expected))

    if _all_populated and not _seq_match(_actual, _expected):
        diagnosed = False
        # Single missing layer: actual chain looks like expected with one
        # element dropped, then everything shifts root-ward (an extra
        # 'other' appears at the node end).
        for i in range(len(_expected)):
            shifted = _expected[:i] + _expected[i + 1:] + ["other"]
            if _seq_match(_actual, shifted):
                missing_desc = _hierarchy[i][2]
                # Use neighbouring populated names to anchor the message.
                above = _populated[i - 1][3] if i > 0 and i - 1 < len(_populated) else None
                below = _populated[i][3] if i < len(_populated) else None
                where = []
                if above is not None:
                    where.append(f"above '{above}'")
                if below is not None:
                    where.append(f"below '{below}'")
                where_str = (" " + " and ".join(where)) if where else ""
                errors.append(
                    f"Path is missing {missing_desc}{where_str}. {_layout_hint}."
                )
                valid = False
                diagnosed = True
                break

        # Single inserted (extra) layer: actual is expected with an
        # 'other' spliced in at some position.
        if not diagnosed:
            for i in range(len(_expected) + 1):
                inserted = (_expected[:i] + ["other"] + _expected[i:])[:len(_actual)]
                if _seq_match(_actual, inserted):
                    extra_name = _populated[i][3] if i < len(_populated) else "?"
                    errors.append(
                        f"Path appears to have an unexpected extra folder "
                        f"'{extra_name}' inserted into the hierarchy. "
                        f"{_layout_hint}."
                    )
                    valid = False
                    diagnosed = True
                    break

        # Generic per-slot mismatch fallback.
        if not diagnosed:
            for lvl, exp, desc, name, actual_shape in _populated:
                if actual_shape == exp:
                    continue
                if exp == "other":
                    continue
                # Fault tolerance: an unrecognised all-caps token at the
                # sensor position is treated as a new sensor, not an error.
                if exp == "sensor" and re.match(r"^[A-Z][A-Z0-9]+$", name):
                    continue
                hint = (f" (looks like a {actual_shape} folder)"
                        if actual_shape and actual_shape not in ("other", exp) else "")
                errors.append(
                    f"Expected {desc} at the '{lvl}' position but got '{name}'{hint}."
                )
                valid = False

    elif not _all_populated:
        # Partial chain (path_level above sensor): validate each populated
        # name's shape individually.
        for lvl, exp, desc, name, actual_shape in _populated:
            if actual_shape == exp or exp == "other":
                continue
            hint = (f" (looks like a {actual_shape} folder)"
                    if actual_shape and actual_shape not in ("other", exp) else "")
            errors.append(
                f"Expected {desc} at the '{lvl}' position but got '{name}'{hint}."
            )
            valid = False

    # Levels that require certain predictable fields to be present
    if path_level in ("sub_tier", "tier", "run", "date") and (parsed["date"] is None or pd.isna(parsed["date"])):
        valid = False
        errors.append(f"path_level='{path_level}' requires a valid date folder in the path ancestry, but none was found.")
    if path_level in ("sub_tier", "tier", "run") and parsed["run_folder"] is None:
        valid = False
        errors.append(f"path_level='{path_level}' requires a run folder, but none was found.")
    if path_level in ("sub_tier", "tier") and parsed["tier"] is None:
        valid = False
        errors.append(f"path_level='{path_level}' requires a tier folder (e.g. 'T0_raw'), but none was found.")
    if path_level == "sub_tier" and parsed["sub_tier"] is None:
        valid = False
        errors.append("path_level='sub_tier' requires a sub_tier folder, but none was found.")

    # For levels above date, check that at least one valid date folder exists at the expected depth.
    # Structure: root / node / project / site / sensor / date
    # Only possible when the path exists on disk; nonexistent paths are
    # validated by name shape alone so parsing stays machine-independent.
    _date_depth = {"sensor": 1, "site": 2, "project": 3, "node": 4, "root": 5}
    if valid and path_level in _date_depth and pth.is_dir():
        depth = _date_depth[path_level]
        _prefix = ["*"] * (depth - 1)
        glob_pattern_8 = "/".join(_prefix + ["????????"])
        glob_pattern_10 = "/".join(_prefix + ["??????????"])
        has_date = (
            any(date_regex.match(p.name) for p in pth.glob(glob_pattern_8) if p.is_dir())
            or any(date_regex.match(p.name) for p in pth.glob(glob_pattern_10) if p.is_dir())
        )
        if not has_date:
            valid = False
            errors.append(f"path_level='{path_level}': no valid date folders (YYYYMMDD) found at expected depth ({depth} level(s) below '{pth.as_posix()}').")

    # Clear garbage values when validation fails
    if not valid:
        for key in parsed:
            if key not in ("path_level", "input_path", "valid", "errors"):
                parsed[key] = None

    parsed["valid"] = valid
    parsed["errors"] = errors

    return parsed
