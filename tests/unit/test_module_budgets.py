"""Module-budget guardrail for ``lookup/`` and the pipeline's ``core/`` surface (LML#731, LML#751).

The orchestrator decomposition (LML#722) took ``lookup/orchestrator.py`` from a
4,485-line god module to a documented spine plus one module per concern. This
test is the regrowth guardrail: every ``lookup/**/*.py`` file carries an
explicit per-file line ceiling, and any new file under ``lookup/`` must be
given one deliberately before it can ship.

LML#751 extended the perimeter to ``core/search.py``: with every ``lookup/``
file capped, the strategy runner was the uncapped path of least resistance for
new pipeline behavior. Rather than globbing all of ``core/`` (most of it isn't
pipeline surface), out-of-package files are opted in explicitly via
``EXTRA_BUDGET_FILES`` — a new ``core/`` module stays unguarded until someone
deliberately adds it there, same as the docs/architecture.md key-files map.

Calibration (2026-07-06, post-LML#731 respine): each ceiling is the smallest
multiple of 50 at or above 1.3x the file's measured size (minimum 50) — about
30% headroom for ordinary maintenance, not for a new concern. When a file hits
its ceiling, the answer is to extract a module, not to append; raising a
ceiling is a deliberate, reviewed decision, never a drive-by edit.

Test files are outside this perimeter, deliberately: their size pressure comes
from one-test-per-behavior granularity (already reviewed at PR time), not from
the "everything lands in the file that already has everything" regrowth this
guardrail targets, and a budget on test files would fight normal TDD growth
instead of a real concern accumulating unnoticed.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOOKUP_ROOT = REPO_ROOT / "lookup"

#: Files outside lookup/ that this guardrail also covers (LML#751). Opted in
#: explicitly, one at a time — see the module docstring for why this isn't a
#: second directory glob.
EXTRA_BUDGET_FILES: tuple[str, ...] = ("core/search.py",)

#: Repo-relative POSIX path -> maximum physical line count (``wc -l``).
MODULE_BUDGETS: dict[str, int] = {
    "lookup/__init__.py": 50,
    # LML#930 PR2: shed policy only (predicate + env resolvers + telemetry
    # projection + orchestration). 1.3x its ~152-line measured size -> 200.
    "lookup/admission.py": 200,
    "lookup/artist_resolution.py": 550,
    "lookup/artwork.py": 500,
    "lookup/caller_reason.py": 100,
    "lookup/candidate_memo.py": 150,
    "lookup/concurrency.py": 200,
    "lookup/endpoint_family.py": 100,
    "lookup/enrichment/__init__.py": 350,
    "lookup/enrichment/background.py": 100,
    # LML#1098: the inline Bandcamp live probe, extracted out of item.py to keep
    # that file under its ceiling (same posture streaming_status.py took for
    # LML#1053) — a self-contained concern with no dependency on enrich_one's
    # control flow. Recalibrated 2026-08 (LML#1106 review, FIXes 2-4: the
    # is_top1 gate, the probe_owns_bandcamp_leg predicate, and the
    # live_resolved mint all landed here rather than in the already-at-ceiling
    # item.py) — smallest multiple of 50 at or above 1.3x its new ~266-line
    # measured size.
    "lookup/enrichment/bandcamp_probe.py": 350,
    "lookup/enrichment/context.py": 150,
    "lookup/enrichment/item.py": 850,
    # LML#1053: the per-service streaming-status merge, extracted out of
    # item.py to stay under its own ceiling above — a pure function with no
    # dependency on enrich_one's control flow. 1.3x its ~70-line measured
    # size -> 100.
    "lookup/enrichment/streaming_status.py": 100,
    "lookup/enrichment/top1.py": 100,
    "lookup/external_search.py": 350,
    # Recalibrated 2026-08-01 (LML#1026, supersedes the 2026-07-31
    # transparent-fold calibration): the module gained the empty-vs-degraded
    # contract (probe/rank/join failures deliberately PROPAGATE — never
    # collapse to [], which the strategy would read as an authoritative
    # index miss — with every await site owning its own degrade policy) and
    # the three location_union_index_* stat keys. Smallest multiple of 50 at
    # or above the new measured size.
    "lookup/location_union.py": 300,
    "lookup/matching.py": 550,
    "lookup/models.py": 150,
    # Recalibrated 2026-08-01 (LML#1026, supersedes the 2026-07-31
    # transparent-fold calibration): the location-union task is now threaded
    # through `_step_search_pipeline` into the TRACK_ON_COMPILATION execute
    # partial (the recall index is authoritative for comp-track resolution on
    # the union-active path), the task-creation gate grew the
    # `discogs_cache_pg is not None` leg so "union unavailable" can never
    # read as an authoritative index miss, and the review pass added
    # `_task_resolved_with_renderable_locations` (step 3a's index-hit skip)
    # plus `_fold_locations_into_results` (shared happy/degraded fold with
    # song_not_found-aware positioning and the LML#717 overlap signal). The
    # strategy-side logic itself lives in
    # `lookup/strategies/track_on_compilation.py` per this module's
    # extraction policy. Same tight-on-purpose posture as every prior
    # recalibration: smallest multiple of 50 at or above the new measured
    # size, not a re-derived 1.3x (which would reopen hundreds of lines of
    # headroom). Third consecutive recalibration in three days — the
    # documented extraction blocker (the reconciliation needs
    # build_context_message, orchestrator-only) is the debt to pay down
    # before the next one.
    "lookup/orchestrator.py": 1650,
    "lookup/release_resolution.py": 550,
    # Recalibrated 2026-07-27 (LML#944): unrelated changes since the 2026-07-06
    # calibration had already carried this file to exactly its old 950-line
    # ceiling (zero headroom) before this ticket's two mandated Sentry-tag call
    # sites landed. The new endpoint-family concern itself was extracted to
    # `lookup/endpoint_family.py` per this module's own policy; the residual
    # growth here (an import plus the two irreducible call sites) is
    # recalibrated with the same formula as every other entry: smallest
    # multiple of 50 at or above 1.3x the post-change 959-line size.
    "lookup/router.py": 1250,
    "lookup/rowless.py": 450,
    "lookup/server_timing_legs.py": 100,
    "lookup/spine_deadline.py": 250,
    "lookup/strategies/__init__.py": 150,
    "lookup/strategies/artist_plus_album.py": 300,
    "lookup/strategies/library_miss.py": 200,
    "lookup/strategies/song_as_artist.py": 250,
    "lookup/strategies/song_as_track.py": 150,
    "lookup/strategies/swapped_interpretation.py": 300,
    # Recalibrated 2026-07-29 (LML#752): the 443-line
    # ``search_compilations_for_track`` was decomposed into named phase
    # helpers (candidate discovery, tracklist cross-reference, artist
    # verification, result shaping), each carrying its own docstring — the
    # decomposition itself grew the file to 854 lines even though no
    # function now exceeds ~100 lines. Same formula as every other entry:
    # smallest multiple of 50 at or above 1.3x the post-change size.
    "lookup/strategies/track_on_compilation.py": 1150,
    "lookup/strategies/track_release_matching.py": 550,
    "lookup/strategies/va_rescue.py": 300,
    # Recalibrated 2026-08-05 (LML#1121 review): the warm-executor extraction
    # documented immediately below (`lookup/streaming_warm.py`) pulled the
    # concurrency semaphore, the in-flight dedup set, the background-task
    # registry, the enqueue, the warm coroutine itself, and its
    # release-identity mint out of this file, which now only *decides whether
    # to warm*. That took the file from the prior 2026-08-04 recalibration's
    # ~771 measured lines down to 573. Smallest multiple of 50 at or above the
    # new measured size, not a re-derived 1.3x (which would reopen hundreds of
    # lines of headroom), matching this table's own recalibration convention
    # (see e.g. track_on_compilation.py above): 600.
    "lookup/streaming_url_postprocess.py": 600,
    # LML#1121 review: the warm *executor* — the concurrency semaphore, the
    # in-flight dedup set, the background-task registry, the enqueue, the warm
    # coroutine itself and its release-identity mint — extracted out of
    # streaming_url_postprocess.py, which keeps *deciding whether to warm*.
    # The review's F1/F3 pass left that file at 888 against its 800 ceiling
    # (a later prose dedupe brought it to 870) with only +23 executable lines,
    # so 800 was arithmetically unreachable (it measured 781 on main);
    # extracting rather than raising follows this table's convention and the
    # streaming_warm_admission.py precedent directly below. 1.3x its
    # ~334-line measured size -> smallest multiple of 50 at or above that, 450.
    "lookup/streaming_warm.py": 450,
    # LML#1108: pending-warm depth-bound policy, extracted out of
    # streaming_url_postprocess.py to stay under its own ceiling above — a
    # small, self-contained concern (constant + two pure/best-effort
    # functions). 1.3x its ~51-line measured size -> smallest multiple of 50
    # at or above that, 100.
    "lookup/streaming_warm_admission.py": 100,
    "lookup/tail_deadline.py": 150,
    "lookup/timeouts.py": 100,
    # Recalibrated 2026-07-29 (LML#750): the step-3b validation cascade
    # (``apply_track_validation_cascade``) and the LML#717 song-as-album-title
    # promotion moved in from the spine, carrying this file from 243 to ~395
    # lines. Smallest multiple of 50 at or above 1.3x the post-change size.
    "lookup/validation.py": 550,
    # LML#751: strategy runner, SearchState, and budget/timeout machinery —
    # the pipeline's other regrowth attractor once every lookup/ file was
    # capped. 1033 measured lines (2026-07-29); smallest multiple of 50 at or
    # above 1.3x that size.
    "core/search.py": 1350,
}


def _budgeted_files(extra: tuple[str, ...] = EXTRA_BUDGET_FILES) -> list[str]:
    """Every file this guardrail covers: ``lookup/**/*.py`` plus ``extra``.

    ``extra`` entries are dropped if the file no longer exists, same as
    ``lookup/`` files — a renamed-away ``core/`` module falls out of the
    fileset and its now-stale MODULE_BUDGETS entry gets caught by
    ``test_no_stale_budget_entries`` instead of silently lingering.
    """
    # Dot-prefixed components (Emacs lock files and the like) can never be valid
    # modules, so they are skipped; untracked non-dot scratch files deliberately
    # keep failing (pre-push signal).
    lookup_files = (
        p.relative_to(REPO_ROOT).as_posix()
        for p in LOOKUP_ROOT.rglob("*.py")
        if not any(part.startswith(".") for part in p.relative_to(LOOKUP_ROOT).parts)
    )
    existing_extra = (rel_path for rel_path in extra if (REPO_ROOT / rel_path).is_file())
    return sorted({*lookup_files, *existing_extra})


def _check_within_budget(rel_path: str, budgets: dict[str, int] = MODULE_BUDGETS) -> None:
    """Fail loudly if ``rel_path`` has no budget entry, or is over its ceiling."""
    ceiling = budgets.get(rel_path)
    if ceiling is None:
        pytest.fail(
            f"{rel_path} has no entry in MODULE_BUDGETS ({__file__}). "
            "Add a ceiling sized per the calibration convention in this module's "
            "docstring — the guardrail cannot be dodged by putting growth in a "
            "new, unbudgeted file."
        )
    actual = (REPO_ROOT / rel_path).read_bytes().count(b"\n")
    assert actual <= ceiling, (
        f"{rel_path} is {actual} lines, over its {ceiling}-line budget. "
        "Extract the growing concern into its own module — see this module's "
        "docstring for the ceiling policy and the key-files map in "
        "docs/architecture.md for where each concern lives."
    )


@pytest.mark.parametrize("rel_path", _budgeted_files())
def test_module_within_budget(rel_path: str) -> None:
    """Each covered module stays within its declared line ceiling."""
    _check_within_budget(rel_path)


def test_no_stale_budget_entries() -> None:
    """MODULE_BUDGETS lists only files that exist (keeps the table honest)."""
    stale = sorted(set(MODULE_BUDGETS) - set(_budgeted_files()))
    assert not stale, (
        f"MODULE_BUDGETS has entries for files that no longer exist: {stale}. "
        "Remove (or rename) them so the budget table stays an accurate map of "
        "the covered files."
    )


def test_extended_perimeter_missing_entry_fails_loudly() -> None:
    """LML#751 probe: a file added to EXTRA_BUDGET_FILES without a budget entry
    fails the same way an unbudgeted lookup/ file does — the missing-entry arm
    covers the extended (out-of-package) perimeter, not just lookup/."""
    with pytest.raises(pytest.fail.Exception, match="has no entry in MODULE_BUDGETS"):
        _check_within_budget("core/search.py", budgets={})


def test_extended_perimeter_stale_entry_detected() -> None:
    """LML#751 probe: a MODULE_BUDGETS entry for an out-of-package file that no
    longer exists is caught by the same stale-entry arm that guards lookup/,
    proving the extended perimeter is covered end to end rather than only at
    its one real entry."""
    budgets_with_stale_entry = {**MODULE_BUDGETS, "core/does_not_exist_probe.py": 100}
    stale = sorted(set(budgets_with_stale_entry) - set(_budgeted_files()))
    assert stale == ["core/does_not_exist_probe.py"]


def test_extended_perimeter_over_budget_detected() -> None:
    """LML#751 probe: an out-of-package file over its declared ceiling fails
    the same over-budget assertion lookup/ files get."""
    with pytest.raises(AssertionError, match="over its .*-line budget"):
        _check_within_budget("core/search.py", budgets={"core/search.py": 1})
