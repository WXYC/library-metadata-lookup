"""Repo-root cleanup Phase 3 (LML#1132): the packaging manifest must match what
the deploy image actually needs.

Two guards, one failure mode: ``Dockerfile``'s ``pip install --no-cache-dir .``
installs the packages in ``packages.find.include`` and the distributions in
``[project].dependencies`` -- nothing else. Anything a runtime module reaches
for that is missing from either list is absent in production, with the local
dev install (``.[dev]``, plus whatever extras a developer happens to have) and
the whole unit suite staying green over the hole.

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

import ast
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Directories whose imports run in the deployed service. Deliberately excludes
#: ``scripts/`` (operator tooling, run with ``uv run --extra <name>``) and
#: ``tests/`` -- an extra-only import is legitimate in both.
_RUNTIME_PACKAGE_DIRS: tuple[str, ...] = (
    "artists",
    "cache",
    "clients",
    "config",
    "core",
    "discogs",
    "entity",
    "generated",
    "identity",
    "library",
    "lookup",
    "release",
    "routers",
    "services",
    "storage",
    "streaming",
)


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


def _dist_to_module(requirement: str) -> str:
    """Top-level module name a requirement string installs, close enough for this.

    Strips the version specifier and any extras, then applies the PEP 503
    ``-`` -> ``_`` normalization. Exact for every distribution this repo
    depends on today; a future dependency whose import name diverges from its
    distribution name (``PyYAML`` -> ``yaml``) needs a mapping here rather
    than a weaker assertion.
    """
    name = re.split(r"[<>=!~\[;]", requirement, maxsplit=1)[0].strip()
    return name.replace("-", "_")


def _imported_top_level_modules() -> set[str]:
    """Every top-level module name imported anywhere under the runtime packages.

    Walks the AST rather than the text so a lazy (function-body) import counts
    exactly the same as a module-level one -- which is the entire point: a
    lazy import is invisible at boot and fails only when the code path first
    runs, in production, under a feature flag.
    """
    found: set[str] = set()
    for package in _RUNTIME_PACKAGE_DIRS:
        for path in (REPO_ROOT / package).rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    found.add(node.module.split(".", 1)[0])
    return found


def test_no_runtime_module_imports_an_extra_only_dependency():
    """A distribution a runtime package imports must be a RUNTIME dependency.

    An optional extra is installed by ``uv run --extra <name>`` and by a
    developer's ``.[dev]``; it is NOT installed by ``Dockerfile``'s ``pip
    install --no-cache-dir .``. So an extra-only distribution that any module
    under the runtime packages imports is simply absent in production.

    The symptom is worse than a boot failure, because the import is typically
    lazy and behind a flag: the service starts fine, and the ``ModuleNotFoundError``
    arrives only on the first request that reaches the feature. Unit tests
    cannot see it either -- they inject a fake at the seam the lazy import
    sits behind, which is exactly why that seam exists.

    LML#1103 shipped this: ``ytmusicapi`` was a ``drain``-extra distribution
    whose lazy import moved onto the ``/lookup`` warm path. ``boto3``'s
    comment in ``pyproject.toml`` records the same lesson from LML#835.
    """
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    runtime = {_dist_to_module(req) for req in manifest["project"]["dependencies"]}
    imported = _imported_top_level_modules()

    offenders: dict[str, str] = {}
    for extra, requirements in manifest["project"]["optional-dependencies"].items():
        if extra == "dev":
            continue  # test-only tooling; never imported by a runtime package
        for requirement in requirements:
            module = _dist_to_module(requirement)
            if module in imported and module not in runtime:
                offenders[module] = extra

    assert not offenders, (
        "Runtime package(s) import "
        f"{sorted(offenders)}, which pyproject.toml declares ONLY under the "
        f"optional extra(s) {sorted(set(offenders.values()))}. The Dockerfile "
        "installs no extras, so this is a ModuleNotFoundError in production "
        "the moment the code path runs. Move the distribution into "
        "[project].dependencies (see boto3's comment there for the precedent), "
        "or keep the import out of the runtime packages."
    )
