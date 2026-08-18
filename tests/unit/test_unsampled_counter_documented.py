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
appear in that doc entry as a whole token -- in the recorded comma list if
exempt, in the prose if deliberately excluded. Whole-token mention rather than
a schema over the paragraph is deliberate: the entry is a 300-word prose line,
and a format contract across all of it would be brittle without catching
anything this doesn't. One narrow exception is load-bearing:
:func:`_recorded_exempt_names` does pin the literal "Prod + staging value:"
fragment, because the reverse/stale check ``docs/testing.md`` requires has to
know which names the doc claims are live. Reword that fragment and this net
fails pointing at the doc.

Sibling net: ``tests/unit/test_lifespan_bootstrap_totality.py`` -- same
glob/discover/assert-roster shape, and the same accessories ``docs/testing.md``
requires of this family. Scope: this net checks that an event NAME is
classified; it does not check that the emit mechanics route through the shared
helper, which is LML#1216.
"""

from __future__ import annotations

import ast
import functools
import os
import re
from pathlib import Path
from typing import NamedTuple

_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_VARS_DOC = _ROOT / "docs" / "env-vars.md"
_EMITTER = "capture_unsampled_counter"
_DOC_ENTRY_MARKER = "- `POSTHOG_RATELIMIT_EXEMPT_EVENTS`"

# Directory names never descended into. ``tests`` is skipped so a fixture's
# stub event name never counts as a real counter needing docs; the rest are
# vendored or build trees.
_SKIP_DIRS = frozenset(
    {
        ".venv",
        "venv",
        "env",
        ".tox",
        "build",
        "dist",
        ".git",
        "tests",
        "__pycache__",
        "node_modules",
    }
)

# Names allowed on the recorded exempt list with no emit site in this repo.
# Empty today. The real case this exists for: ``wxyc_fastapi``'s own
# ``telemetry_rate_limited`` report event is emitted by the limiter itself, so
# exempting it would be legitimate and would never resolve to an LML call site.
_STALE_LIST_EXEMPT: frozenset[str] = frozenset()

# How far the resolver will chase an event name through local wrapper calls.
# One hop covers today's only indirect site (``wikipedia_warm``'s private
# ``_capture_fetch_outcome(event)``); the cap is a cycle guard, so a self- or
# mutually-recursive wrapper terminates instead of recursing forever.
_MAX_RESOLVE_HOPS = 2


class _Discovery(NamedTuple):
    events: dict[str, str]
    """Event name -> the ``path:line`` of the first call site that emits it."""
    unresolved: list[str]
    """Call sites whose event name could not be read. Never silently dropped:
    an unreadable site is exactly as undocumented as a missing one."""


def _source_files() -> list[Path]:
    """Every candidate source file.

    ``os.walk`` with in-place pruning rather than ``rglob`` + filter: the
    filter form yields every path under ``.venv`` and discards it afterwards,
    which is ~95% of the paths and ~17x the wall time.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        found.extend(Path(dirpath) / name for name in filenames if name.endswith(".py"))
    return sorted(found)


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


def _emitter_local_names(tree: ast.Module) -> set[str]:
    """Local names the emitter is bound to by ``from ... import`` in this
    module, honouring ``as`` aliases. An aliased import that this ignored
    would make every one of its call sites invisible."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == _EMITTER
    }


def _calls_by_name(tree: ast.Module, names: set[str]) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names
    ]


def _emitter_calls(tree: ast.Module) -> list[ast.Call]:
    """Every call to the emitter, by any of its reachable spellings.

    Three forms have to be caught or the totality claim is false: the bare
    imported name, an ``as``-aliased one, and attribute access on the module
    (``observability.capture_unsampled_counter``, which ``from core import
    observability`` produces). The attribute arm matches on the attribute
    alone rather than resolving the receiver -- over-matching here is
    harmless (it can only demand that a name be documented), while
    under-matching silently reopens LML#1217.
    """
    calls = _calls_by_name(tree, _emitter_local_names(tree))
    calls += [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == _EMITTER
    ]
    return calls


def _enclosing_function(
    tree: ast.Module, target: ast.expr
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            sub is target for sub in ast.walk(node)
        ):
            return node
    return None


def _resolve(expr: ast.expr, tree: ast.Module, consts: dict[str, str], hops: int = 0) -> set[str]:
    """Every string an event-name expression can evaluate to.

    Handles the shapes that occur: a bare literal, a module-level constant,
    and a parameter of a local wrapper (resolved from that wrapper's own call
    sites). ``IfExp`` yields BOTH branches -- the Wikipedia warm's single call
    site picks its counter with a conditional, so taking one branch would
    silently under-report by half.

    The enclosing function's parameters are checked BEFORE module constants:
    a module-level ``event = "..."`` that happens to share a wrapper
    parameter's name would otherwise shadow it and resolve the site to a
    fabricated name while missing every real one.
    """
    if hops > _MAX_RESOLVE_HOPS:
        return set()
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) else set()
    if isinstance(expr, ast.IfExp):
        return _resolve(expr.body, tree, consts, hops) | _resolve(expr.orelse, tree, consts, hops)
    if not isinstance(expr, ast.Name):
        return set()
    function = _enclosing_function(tree, expr)
    if function is None or expr.id not in _parameter_names(function):
        return {consts[expr.id]} if expr.id in consts else set()
    # Pair names to arguments with zip rather than an index: a keyword-only
    # name is absent from this list so it can never be matched positionally,
    # and a call passing fewer arguments than the signature simply truncates.
    positional = [arg.arg for arg in (*function.args.posonlyargs, *function.args.args)]
    resolved: set[str] = set()
    for call in _calls_by_name(tree, {function.name}):
        for param, argument in zip(positional, call.args, strict=False):
            if param == expr.id:
                resolved |= _resolve(argument, tree, consts, hops + 1)
        for keyword in call.keywords:
            if keyword.arg == expr.id:
                resolved |= _resolve(keyword.value, tree, consts, hops + 1)
    return resolved


def _parameter_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every parameter name, positional or keyword-only. Membership only --
    ordering lives at the one place that needs it, in :func:`_resolve`."""
    args = function.args
    return {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}


@functools.cache
def _discover_counters() -> _Discovery:
    """Sweep the source tree for every event name reaching the emitter.

    Cached: the sweep reads and parses ~240 modules for a result that cannot
    change within a session, and each test below would otherwise repeat it.
    """
    events: dict[str, str] = {}
    unresolved: list[str] = []
    for path in _source_files():
        source = path.read_text(encoding="utf-8")
        if _EMITTER not in source:
            # Cheap pre-filter, and the reason this net stays sub-second: a
            # call to the emitter -- direct, ``as``-aliased, or by attribute
            # access -- must spell its name somewhere in the file, so a module
            # that never mentions it cannot hold a call site. Skips parsing
            # (and a full ``ast.walk``, ~2s across the tree) for the ~97% of
            # modules that can't match. Cannot produce a false negative.
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            # A vendored or generated file that does not parse is not this
            # net's business; failing here would red a PostHog-counter test
            # for an unrelated reason.
            continue
        calls = _emitter_calls(tree)
        if not calls:
            continue
        consts = _module_string_constants(tree)
        for call in calls:
            site = f"{path.relative_to(_ROOT)}:{call.lineno}"
            if len(call.args) < 2:
                unresolved.append(f"{site} (event name is not the 2nd positional arg)")
                continue
            names = _resolve(call.args[1], tree, consts)
            if not names:
                unresolved.append(f"{site} (could not resolve the event-name expression)")
            for name in names:
                events.setdefault(name, site)
    return _Discovery(events, unresolved)


@functools.cache
def _doc_entry() -> str:
    for line in _ENV_VARS_DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith(_DOC_ENTRY_MARKER):
            return line
    raise AssertionError(f"no {_DOC_ENTRY_MARKER} entry found in {_ENV_VARS_DOC}")


def _recorded_exempt_names(entry: str) -> list[str]:
    """The comma list the doc records as the live prod+staging value."""
    match = re.search(r"Prod \+ staging value: `([a-z0-9_,]+)`", entry)
    assert match, "could not find the recorded `Prod + staging value` list in the doc entry"
    return [name for name in match.group(1).split(",") if name]


def _is_named_in(entry: str, name: str) -> bool:
    """Whether ``entry`` mentions ``name`` as a whole token.

    Substring containment would let a new counter free-ride on a prefix
    collision with a documented sibling -- ``wikipedia_bio_fetch`` sits
    inside ``wikipedia_bio_fetch_ok`` -- and report itself documented while
    no sentence about it exists.
    """
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", entry) is not None


def test_discovery_finds_the_known_unsampled_counters():
    """Vacuity guard (mandatory per ``docs/testing.md``): a discovery step that
    silently matches nothing would make the checks below pass forever while
    guarding nothing. Also fails on a call site whose event name the resolver
    cannot follow -- an unreadable site is exactly as undocumented as a
    missing one, and must not be skipped quietly."""
    events, unresolved = _discover_counters()
    assert not unresolved, f"unresolved {_EMITTER} event names: {unresolved}"
    assert len(events) >= 8, f"expected at least 8 counters, found {sorted(events)}"
    # Spot-check one exempt incumbent and one deliberate exclusion, so a
    # resolver that drifts to finding only half the sites still fails here.
    assert "discogs_rate_gate_fail_open" in events
    assert "wikipedia_bio_fetch_reject" in events


def test_the_doc_mention_check_rejects_a_name_the_entry_omits():
    """Negative control for :func:`_is_named_in`, the predicate both checks
    below rest on.

    Without this, an ``_is_named_in`` that returned True unconditionally would
    leave every other test in this file green -- the vacuity guard only probes
    discovery, and the stale check never calls it. The net's central claim
    would then be asserted but untested. Mirrors
    ``test_lifespan_bootstrap_totality``'s synthetic-unregistration control.
    """
    entry = _doc_entry()
    assert _is_named_in(entry, "wikipedia_bio_fetch_reject"), "a documented name must be found"
    assert not _is_named_in(entry, "counter_the_entry_never_mentions")
    # A prefix of a documented name must not free-ride on it.
    assert not _is_named_in(entry, "wikipedia_bio_fetch")


def test_every_unsampled_counter_event_is_classified_in_env_vars_doc():
    """Every discovered counter must be named in the
    ``POSTHOG_RATELIMIT_EXEMPT_EVENTS`` doc entry -- listed if it is exempt,
    explained if it is deliberately not. Silence is the failure mode LML#1217
    reports: an absent name reads identically whether it was excluded on
    purpose or simply forgotten.

    Deliberately has no exemption roster. The escape hatch for a counter that
    should not be exempt already exists and is the point of the ticket: name
    it in the entry's prose and say why."""
    events = _discover_counters().events
    entry = _doc_entry()
    undocumented = sorted(name for name in events if not _is_named_in(entry, name))
    assert not undocumented, (
        f"detached-task counters missing from {_ENV_VARS_DOC.name}'s "
        f"POSTHOG_RATELIMIT_EXEMPT_EVENTS entry: {undocumented}. Add each to the "
        "recorded exempt list, or write why it is deliberately excluded. Sites: "
        + ", ".join(f"{name} at {events[name]}" for name in undocumented)
    )


def test_no_stale_names_in_the_recorded_exempt_list():
    """Reverse/stale check (mandatory per ``docs/testing.md``): every name the
    doc records as live must still resolve to a real emit site.

    Without this the map decays in the other direction -- rename or delete a
    counter and the doc keeps advertising it while the Railway variable keeps
    exempting an event nothing emits. That is the same silent drift LML#1217
    reports, pointing the other way."""
    events = _discover_counters().events
    stale = sorted(
        name
        for name in _recorded_exempt_names(_doc_entry())
        if name not in _STALE_LIST_EXEMPT and name not in events
    )
    assert not stale, (
        f"{_ENV_VARS_DOC.name} records these as exempt but nothing in this repo emits "
        f"them: {stale}. Drop them from the recorded value (and from the live Railway "
        f"variable), or add them to _STALE_LIST_EXEMPT if the emitter is upstream."
    )
