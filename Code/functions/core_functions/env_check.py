"""Advisory check that the active Python environment matches environment.yml.

Compares the repo's ``environment.yml`` against the running interpreter
using :func:`importlib.util.find_spec` (no imports are executed, no conda
subprocess is spawned, so the check costs milliseconds). Prints a friendly
note listing anything missing plus the ``conda env update`` command to fix
it. Purely advisory: it never raises and never stops execution.
"""

# ==============================================================================
import pathlib
import re
import sys
from importlib import util as importlib_util
from typing import List, Optional, Union


# ==================================================================================
def check_environment(
        repo_root: Optional[Union[str, pathlib.Path]] = None,
        env_file: str = "environment.yml",
    ) -> List[str]:
    """Warn (non-fatally) when the active env is missing environment.yml packages.

    Parameters
    ----------
    repo_root : str or pathlib.Path, optional
        Repository root containing ``env_file``. Scripts should pass their
        module-level ``_git_root``; defaults to the current working
        directory.
    env_file : str, optional
        Environment spec filename relative to ``repo_root``.
        Default ``"environment.yml"``.

    Returns
    -------
    list of str
        Conda package names from the spec that are not importable in the
        running interpreter (empty when the environment is in sync, when
        the spec file is absent, or when the check itself failed).

    Notes
    -----
    Advisory only — every failure mode (unreadable YAML, missing PyYAML,
    broken package metadata) is caught and reported as a one-line note so
    this can never block a pipeline run.
    """
    missing: List[str] = []
    try:
        env_path = pathlib.Path(repo_root or ".") / env_file
        if not env_path.is_file():
            return missing

        import yaml
        spec = yaml.safe_load(env_path.read_text())

        # +++++ flatten conda deps + the pip: sub-list +++++
        flat: List[str] = []
        for dep in spec.get("dependencies", []):
            if isinstance(dep, dict):
                flat.extend(str(d) for d in dep.get("pip", []))
            elif dep is not None:
                flat.append(str(dep))

        # +++++ conda names that are not importable Python modules +++++
        non_python = {"git", "gh", "eza", "pip"}
        # +++++ conda name -> import name where they differ +++++
        import_names = {
            "pyyaml": "yaml",
            "gitpython": "git",
            "lazrs-python": "lazrs",
            "pvlib-python": "pvlib",
            "ruamel.yaml": "ruamel.yaml",
        }

        py_note = None
        for dep in flat:
            name = re.split(r"[=<>!]", dep, maxsplit=1)[0].strip()
            if not name or name in non_python:
                continue
            if name == "python":
                want = dep.partition("=")[2].strip("= ")
                have = f"{sys.version_info.major}.{sys.version_info.minor}"
                if want and not have.startswith(want) and not want.startswith(have):
                    py_note = f"python is {have}, spec wants {want}"
                continue
            probe = import_names.get(name, name.replace("-", "_"))
            try:
                found = importlib_util.find_spec(probe) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                missing.append(name)

        if missing or py_note:
            env_name = spec.get("name", "datastorage")
            lines = ["-" * 70,
                     f"NOTE: the active Python environment does not match {env_file}:"]
            if missing:
                lines.append(f"  - missing packages: {', '.join(missing)}")
            if py_note:
                lines.append(f"  - {py_note}")
            lines += [
                "This is advisory only - the script will keep running, but may",
                "fail on import. To sync the environment, run:",
                f"    conda env update -n {env_name} -f {env_file} --prune",
                "-" * 70,
            ]
            print("\n".join(lines))
    except Exception as err:  # advisory checker must never break a run
        print(f"(environment check skipped: {err})")
    return missing
