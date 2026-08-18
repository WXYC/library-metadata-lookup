"""Discovery net for LML#1217 — every detached-task PostHog counter must be
classified in ``docs/env-vars.md``'s ``POSTHOG_RATELIMIT_EXEMPT_EVENTS`` entry.

``POSTHOG_RATELIMIT_EXEMPT_EVENTS`` is a hand-maintained Railway variable, and
the doc entry is the only place its membership rule (degradation counters only
-- see the entry itself) is written down. Nothing connected the two: LML#1192
added ``wikipedia_bio_fetch_ok``/``_reject`` and neither name reached the
exempt list nor an exclusion rationale, so for months the divergence was
unreadable -- a reader could not tell whether the absence was deliberate or an
oversight. That is the defect LML#1217 reports, and a paragraph alone would
decay the same way: the doc was correct when LML#1169 wrote it.

So: discover every event name that reaches
``core.observability.capture_unsampled_counter`` and require each one to
appear verbatim somewhere in that doc entry -- in the recorded comma list if
exempt, in the prose if deliberately excluded. Substring presence rather than
a parsed format is deliberate: the entry is a 300-word prose line, and a
format contract over it would be brittle without catching anything this
doesn't. A ninth counter fails here until its author writes the sentence.

Sibling net: ``tests/unit/test_lifespan_bootstrap_totality.py`` (same
glob/discover/assert-roster shape, same mandatory vacuity guard per
``docs/testing.md``). Disjoint from LML#1216, which guards the *emitter
idiom* -- that a module pairing ``get_posthog_client`` with ``.capture(``
must be ``core/observability.py``. That net catches a hand-rolled copy of the
mechanics; this one catches an undocumented event NAME, which is how LML#1217
actually happened (LML#1192 hand-rolled nothing after the consolidation).
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_VARS_DOC = _ROOT / "docs" / "env-vars.md"
_EMITTER = "capture_unsampled_counter"
_DOC_ENTRY_MARKER = "- `POSTHOG_RATELIMIT_EXEMPT_EVENTS`"

# Trees that cannot contain a production emit site. ``tests`` is excluded so a
# fixture's stub event name never counts as a real counter needing docs.
_SKIP_PARTS = frozenset({".venv", ".git", "tests", "__pycache__", "node_modules"})

# How far the resolver will chase an event name through local wrapper calls.
# One hop covers today's only indirect site (``wikipedia_warm``'s private
# ``_capture_fetch_outcome(event)``); the cap keeps a cyclic helper from
# recursing forever.
_MAX_RESOLVE_HOPS = 2


def _source_files() -> list[Path]:
    return [
        path
        for path in sorted(_ROOT.rglob("*.py"))
        if not _SKIP_PARTS & set(path.relative_to(_ROOT).parts)
    ]


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings (annotated or not)."""
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found[target.id] = value.value
    return found


def _calls_to(tree: ast.Module, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def _enclosing_function(tree: ast.Module, target: ast.expr) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and any(sub is target for sub in ast.walk(node)):
            return node
    return None


def _resolve(expr: ast.expr, tree: ast.Module, consts: dict[str, str], hops: int = 0) -> set[str]:
    """Every string an event-name expression can evaluate to.

    Handles the three shapes that occur: a bare literal, a module-level
    constant, and a parameter of a local wrapper (resolved by looking at that
    wrapper's own call sites). ``IfExp`` yields BOTH branches -- the Wikipedia
    warm's single call site picks its counter with a conditional, so taking
    one branch would silently under-report by half.
    """
    if hops > _MAX_RESOLVE_HOPS:
        return set()
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) else set()
    if isinstance(expr, ast.IfExp):
        return _resolve(expr.body, tree, consts, hops) | _resolve(expr.orelse, tree, consts, hops)
    if not isinstance(expr, ast.Name):
        return set()
    if expr.id in consts:
        return {consts[expr.id]}
    function = _enclosing_function(tree, expr)
    if function is None:
        return set()
    params = [arg.arg for arg in function.args.args]
    if expr.id not in params:
        return set()
    index = params.index(expr.id)
    resolved: set[str] = set()
    for call in _calls_to(tree, function.name):
        if len(call.args) > index:
            resolved |= _resolve(call.args[index], tree, consts, hops + 1)
        for keyword in call.keywords:
            if keyword.arg == expr.id:
                resolved |= _resolve(keyword.value, tree, consts, hops + 1)
    return resolved


def _discover_counters() -> tuple[dict[str, str], list[str]]:
    """``({event name: "path:line"}, [unresolved site descriptions])``."""
    events: dict[str, str] = {}
    unresolved: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports_emitter = any(
            isinstance(node, ast.ImportFrom) and any(alias.name == _EMITTER for alias in node.names)
            for node in ast.walk(tree)
        )
        if not imports_emitter:
            continue
        consts = _module_string_constants(tree)
        for call in _calls_to(tree, _EMITTER):
            site = f"{path.relative_to(_ROOT)}:{call.lineno}"
            if len(call.args) < 2:
                unresolved.append(f"{site} (event name is not the 2nd positional arg)")
                continue
            names = _resolve(call.args[1], tree, consts)
            if not names:
                unresolved.append(f"{site} (could not resolve the event-name expression)")
            for name in names:
                events.setdefault(name, site)
    return events, unresolved


def _doc_entry() -> str:
    for line in _ENV_VARS_DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith(_DOC_ENTRY_MARKER):
            return line
    raise AssertionError(f"no {_DOC_ENTRY_MARKER} entry found in {_ENV_VARS_DOC}")


def test_discovery_finds_the_known_unsampled_counters():
    """Vacuity guard (mandatory per ``docs/testing.md``): a discovery step that
    silently matches nothing would make the classification check below pass
    forever while guarding nothing. Also fails on a call site whose event name
    the resolver cannot follow -- an unreadable site is exactly as undocumented
    as a missing one, and must not be skipped quietly."""
    events, unresolved = _discover_counters()
    assert not unresolved, f"unresolved {_EMITTER} event names: {unresolved}"
    assert len(events) >= 8, f"expected at least 8 counters, found {sorted(events)}"
    # Spot-check one exempt incumbent and one deliberate exclusion, so a
    # resolver that drifts to finding only half the sites still fails here.
    assert "discogs_rate_gate_fail_open" in events
    assert "wikipedia_bio_fetch_reject" in events


def test_every_unsampled_counter_event_is_classified_in_env_vars_doc():
    """Every discovered counter must be named in the
    ``POSTHOG_RATELIMIT_EXEMPT_EVENTS`` doc entry -- listed if it is exempt,
    explained if it is deliberately not. Silence is the failure mode LML#1217
    reports: an absent name reads identically whether it was excluded on
    purpose or simply forgotten."""
    events, _ = _discover_counters()
    entry = _doc_entry()
    undocumented = sorted(name for name in events if name not in entry)
    assert not undocumented, (
        f"detached-task counters missing from {_ENV_VARS_DOC.name}'s "
        f"POSTHOG_RATELIMIT_EXEMPT_EVENTS entry: {undocumented}. Add each to the "
        "recorded exempt list, or write why it is deliberately excluded. Sites: "
        + ", ".join(f"{name} at {events[name]}" for name in undocumented)
    )
