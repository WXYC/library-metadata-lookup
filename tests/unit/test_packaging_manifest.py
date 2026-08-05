"""Repo-root cleanup Phase 3 (LML#1132): the packaging manifest must list
every top-level package.

``pyproject.toml``'s ``[tool.setuptools.packages.find]`` ``include`` list had
drifted from the actual package layout: it named 12 top-level packages while
the repo had grown to 17 non-test top-level directories carrying an
``__init__.py``. Five (``artists``, ``identity``, ``release``, ``streaming``,
``cache``) were missing despite being runtime-imported, so a ``pip install .``
(the path ``Dockerfile`` takes) silently shipped an incomplete package with no
CI signal. This meta-test locks the two lists together -- following the
repo's existing meta-test habit (``tests/unit/test_env_hermeticity.py``,
``tests/integration/test_pg_fixture_guard_adoption.py``) -- so a future new
top-level package can't drift out of ``include`` again unnoticed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _top_level_packages() -> set[str]:
    """Every top-level directory (excluding ``tests/``) containing an
    ``__init__.py``."""
    return {
        entry.name
        for entry in REPO_ROOT.iterdir()
        if entry.is_dir() and entry.name != "tests" and (entry / "__init__.py").is_file()
    }


def _include_patterns() -> list[str]:
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    include: list[str] = manifest["tool"]["setuptools"]["packages"]["find"]["include"]
    return include


def test_every_top_level_package_is_in_packages_find_include():
    """Every top-level ``__init__.py``-carrying directory must appear in
    ``packages.find.include`` (as a bare name or a ``name*`` glob), or ``pip
    install .`` silently drops it from the installed package.
    """
    include = _include_patterns()
    included_names = {pattern.rstrip("*") for pattern in include}
    actual_packages = _top_level_packages()

    missing = actual_packages - included_names
    assert not missing, (
        f"Top-level package(s) {sorted(missing)} have an __init__.py but are "
        "not listed in pyproject.toml's [tool.setuptools.packages.find] "
        "include list. Add them (as `<name>*`, matching the existing entries) "
        "or `pip install .` will silently ship an incomplete package. See "
        "WXYC/library-metadata-lookup#1132."
    )
