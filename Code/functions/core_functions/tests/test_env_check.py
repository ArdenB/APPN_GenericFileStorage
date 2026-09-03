"""Tests for core_functions.env_check.check_environment."""

# ==============================================================================
import pathlib
import sys

import pytest

from Code.functions.core_functions import check_environment


# ==================================================================================
def _write_env(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    """Write an environment.yml fixture and return its folder.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Test temp folder.
    body : str
        YAML content.

    Returns
    -------
    pathlib.Path
        The folder containing the written environment.yml.
    """
    (tmp_path / "environment.yml").write_text(body)
    return tmp_path


# ==================================================================================
def test_in_sync_is_silent(tmp_path, capsys):
    root = _write_env(tmp_path, "name: x\ndependencies:\n  - numpy\n  - pyyaml\n")
    assert check_environment(root) == []
    assert capsys.readouterr().out == ""


def test_missing_package_reported(tmp_path, capsys):
    root = _write_env(
        tmp_path,
        "name: x\ndependencies:\n  - numpy\n  - not-a-real-package-xyz\n")
    assert check_environment(root) == ["not-a-real-package-xyz"]
    out = capsys.readouterr().out
    assert "not-a-real-package-xyz" in out
    assert "conda env update -n x -f environment.yml --prune" in out
    assert "advisory" in out


def test_import_name_mapping_and_non_python_skipped(tmp_path):
    # pyyaml/gitpython import under different names; git/gh/eza are CLIs
    root = _write_env(
        tmp_path,
        "name: x\ndependencies:\n"
        "  - pyyaml\n  - gitpython\n  - git\n  - gh\n  - eza\n  - pip\n")
    assert check_environment(root) == []


def test_pip_sublist_probed(tmp_path):
    root = _write_env(
        tmp_path,
        "name: x\ndependencies:\n  - pip\n  - pip:\n"
        "      - definitely-missing-pip-pkg\n")
    assert check_environment(root) == ["definitely-missing-pip-pkg"]


def test_python_version_mismatch_noted(tmp_path, capsys):
    root = _write_env(tmp_path, "name: x\ndependencies:\n  - python=3.99\n")
    assert check_environment(root) == []      # version note, not a missing pkg
    out = capsys.readouterr().out
    assert "spec wants 3.99" in out


def test_matching_python_version_silent(tmp_path, capsys):
    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    root = _write_env(tmp_path, f"name: x\ndependencies:\n  - python={have}\n")
    assert check_environment(root) == []
    assert capsys.readouterr().out == ""


def test_absent_file_is_silent(tmp_path, capsys):
    assert check_environment(tmp_path) == []
    assert capsys.readouterr().out == ""


def test_malformed_yaml_never_raises(tmp_path, capsys):
    root = _write_env(tmp_path, "dependencies: [unclosed\n  - ::::\n")
    assert check_environment(root) == []
    assert "environment check skipped" in capsys.readouterr().out


def test_repo_environment_yml_matches_import_map():
    """The real spec must resolve cleanly in the dev env (name-map guard)."""
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    if not (repo_root / "environment.yml").is_file():
        pytest.skip("no environment.yml at repo root")
    assert check_environment(repo_root) == []
