"""Discovery net for LML#891 — every ``entity/*.py`` schema bootstrap must be
wired into ``main.py``'s lifespan.

``main.py``'s lifespan runs the ``lml_cache.*`` bootstraps from an inline
``bootstraps`` tuple (LML#883's review explicitly rejected a registry
indirection for this — settings coupling isn't worth it for six call sites).
That leaves nothing to stop a new ``entity/*.py`` cache module from shipping
with its ``set_up_*_schema`` never called: the deployed app would simply lack
the table until someone notices downstream. This is a static, PG-free source
check, mirroring the fixture-file discovery net in
``tests/integration/test_pg_fixture_guard_adoption.py``
(``test_every_lml_cache_dropping_suite_is_registered``): glob every
``entity/*.py`` module for a module-level ``set_up_*_schema`` coroutine, and
require ``main.py`` to both import it and reference its name — the same style
of "a hit sits outside the roster and must be registered" check the fixture
net runs for lml_cache-dropping test files.

LML#1204 item 7 added a second assertion over the same discovery: every
discovered bootstrap module must also be a key of the sidecar-generator
registry (``scripts/regenerate_lml_cache_sql._MODULES``), minus
:data:`_SIDECAR_GENERATOR_EXEMPT`, and each registered sidecar ``.sql`` is
byte-pinned to its generated reference — the one hand-maintained per-entity
registry previously had no net, and failed live inside the #1192 stack
(#1194 shipped the attempt sidecar hand-written and unregistered; #1196
retrofitted it two branches later).
"""

from __future__ import annotations

import re
from pathlib import Path

_ENTITY_DIR = Path(__file__).resolve().parent.parent.parent / "entity"
_MAIN_PY = Path(__file__).resolve().parent.parent.parent / "main.py"

# Module-level bootstrap coroutine: no leading indent, ``async def
# set_up_..._schema(``. Excludes nested/test-local helpers of the same shape
# (e.g. fixture-local ``set_up_entity_schema`` in tests/integration/*) because
# this only globs ``entity/*.py``.
_BOOTSTRAP_DEF = re.compile(r"^async def (set_up_\w+_schema)\(", re.MULTILINE)

# Deliberate exceptions: entity/*.py modules that define a `set_up_*_schema`
# coroutine but are NOT called from main.py's lifespan (e.g. a schema bootstrap
# only ever invoked from a script or a per-test fixture). Empty today — every
# entity/*.py bootstrap is a real lifespan-registered lml_cache.* table.
_LIFESPAN_EXEMPT: frozenset[str] = frozenset()


def _discover_bootstraps() -> dict[str, str]:
    """Map bootstrap function name -> owning module name for every
    ``entity/*.py`` file that defines one."""
    found: dict[str, str] = {}
    for path in sorted(_ENTITY_DIR.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for match in _BOOTSTRAP_DEF.finditer(src):
            found[match.group(1)] = path.stem
    return found


def test_discovery_finds_the_known_bootstraps():
    # Guards the discovery regex itself: if this drifts to zero (or an
    # unexpected count), the totality check below would vacuously pass.
    found = _discover_bootstraps()
    assert found, "no entity/*.py set_up_*_schema coroutines discovered — regex drifted?"
    assert "set_up_streaming_catalog_schema" in found
    assert "set_up_api_keys_schema" in found


def test_every_entity_schema_bootstrap_is_registered_in_main_lifespan():
    """Every non-exempt ``set_up_*_schema`` coroutine in ``entity/*.py`` must
    be both imported and referenced by name in ``main.py`` — the two
    ingredients the lifespan's inline ``bootstraps`` tuple needs to actually
    call it. A module whose author forgets the tuple entry (or the import)
    fails here instead of shipping a silently-missing table."""
    main_src = _MAIN_PY.read_text(encoding="utf-8")
    found = _discover_bootstraps()

    missing = []
    for name, module in sorted(found.items()):
        if name in _LIFESPAN_EXEMPT:
            continue
        # Registered means: main.py imports FROM the owning module (guards
        # against a same-named coincidental import from elsewhere), and the
        # bootstrap name itself appears a second time beyond that import line
        # (the bootstraps-tuple call site) — imports may be single-line or
        # parenthesized multi-line, so this checks co-occurrence rather than
        # anchoring a single regex across the import's exact shape.
        imports_from_module = f"from entity.{module} import" in main_src
        if not imports_from_module or main_src.count(name) < 2:
            missing.append(f"{name} (entity/{module}.py)")

    assert not missing, (
        f"{missing} define a set_up_*_schema coroutine that main.py does not both import "
        "and invoke from the lifespan's inline bootstraps tuple — register it (or add it "
        "to _LIFESPAN_EXEMPT with a comment explaining why it's intentionally uncalled)"
    )


# ---------------------------------------------------------------------------
# LML#1204 item 7: totality net over the sidecar _MODULES registry
# ---------------------------------------------------------------------------

_SIDECAR_GENERATOR_EXEMPT: frozenset[str] = frozenset({"streaming_catalog"})
"""``entity/*.py`` bootstrap modules whose ``.sql`` sidecar has its OWN
generator (``scripts/regenerate_streaming_catalog_sql.py``, byte-pinned by
its richer parity test) rather than an entry in the shared
``scripts/regenerate_lml_cache_sql._MODULES`` registry."""


def _unregistered_sidecar_modules(registry_keys) -> list[str]:
    """Every discovered ``entity/*.py`` bootstrap module that is neither a
    key of the sidecar registry nor generator-exempt."""
    discovered = set(_discover_bootstraps().values())
    return sorted(discovered - set(registry_keys) - _SIDECAR_GENERATOR_EXEMPT)


def test_every_bootstrap_module_is_a_sidecar_registry_key():
    """The one hand-maintained per-entity-module registry with no discovery
    net — and it failed live inside the LML#1192 stack (#1194 shipped the
    attempt sidecar hand-written and unregistered; #1196 retrofitted it two
    branches later). Every ``entity/*.py`` module defining a
    ``set_up_*_schema`` bootstrap must be a ``_MODULES`` key (its ``.sql``
    sidecar is generated), minus :data:`_SIDECAR_GENERATOR_EXEMPT`."""
    from scripts.regenerate_lml_cache_sql import _MODULES

    missing = _unregistered_sidecar_modules(_MODULES.keys())
    assert not missing, (
        f"{missing} define a set_up_*_schema bootstrap but have no entry in "
        "scripts/regenerate_lml_cache_sql._MODULES — add a SidecarSpec (and regenerate the "
        ".sql) so the sidecar stays generated, or add the module to _SIDECAR_GENERATOR_EXEMPT "
        "with a comment naming its own generator."
    )


def test_sidecar_net_fails_against_a_synthetically_unregistered_module():
    """Non-vacuity probe (the LML#751 style): drop one real registry key and
    the check above must flag exactly that module — proving the net binds to
    the discovered tree rather than passing vacuously."""
    from scripts.regenerate_lml_cache_sql import _MODULES

    registry_without_api_keys = set(_MODULES) - {"api_keys"}
    assert _unregistered_sidecar_modules(registry_without_api_keys) == ["api_keys"]


def test_each_registered_sidecar_file_is_byte_identical_to_its_generated_reference():
    """LML#1204 item 7 (upgrade clause): the simple sidecars get the same
    byte-for-byte ``build_reference(spec)`` pin ``streaming_catalog`` already
    has — a hand-edit to a generated ``.sql``, or a spec edit without a
    regenerate, fails here instead of surviving until the next regeneration
    silently reverts it."""
    from scripts.regenerate_lml_cache_sql import _MODULES, build_reference

    stale = [
        name
        for name, spec in _MODULES.items()
        if (_ENTITY_DIR / f"{name}.sql").read_text(encoding="utf-8") != build_reference(spec)
    ]
    assert not stale, (
        f"{stale} sidecar .sql files differ from their generated reference — regenerate via "
        "`uv run python -m scripts.regenerate_lml_cache_sql` (never hand-edit a generated file)."
    )
