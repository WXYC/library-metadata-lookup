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
    # Recalibrated again (LML#1192 review round 3, finding 10): the round-2
    # B2-1/B2-2 fix (record_bio_adoption + maybe_schedule_wikipedia_bio_warm,
    # 366 lines, budget bumped to 500) carried a provably-redundant
    # resolution_bio_surfaced boolean and a top1_bio_is_discogs string
    # comparison -- both collapsed into wikipedia_bio.finalize_bio's single
    # bool()/enum check (see that module's docstring). Measured 358 lines at
    # this PR's tip -- close to, but not quite, the pre-round-2 350 the
    # reviewer asked to restore "if the line count allows." Rather than
    # reapply the standard 1.3x-headroom rule (which would re-inflate this
    # back toward 500, defeating the point of the collapse), this is a
    # minimal-headroom restoration: smallest multiple of 50 AT OR ABOVE the
    # measured size, no multiplier.
    #
    # Round 5: grew to 369 measured lines (the enriched_top1_wiki call-site
    # plumbing the P0-6 miss-warm-reachability fix needed). The standard
    # 1.3x rule at 369 would license 500 (1.3 x 369 = 479.7, smallest
    # multiple of 50 at or above that) -- 400 stays a deliberate
    # under-grant, approved by Jake as-is. This SPENDS the plan's
    # (docs/plans/lml-1192-wikipedia-artist-bio.md) original coordinator
    # line-neutrality commitment (net delta <= 0 from 350, funded by
    # relocating the Discogs ref-warm to background.py) -- consciously
    # released, not silently missed; see the plan doc's amended commitment
    # note for the full reasoning. In short: the round-3 collapse above
    # already did the substantive work of keeping this file honest, and
    # extracting the ~19 lines of round-5 growth now would be motivated by
    # a line count rather than a concern boundary -- the same failure mode
    # this guardrail exists to prevent, pointed the other way. The growth
    # is call-site plumbing for the bio resolution, which is what a
    # coordinator is for.
    "lookup/enrichment/__init__.py": 400,
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
    # LML#1101: the inline Apple Music probe, extracted out of item.py to keep
    # that file under its ceiling — the same posture bandcamp_probe.py took for
    # LML#1098 and streaming_status.py for LML#1053. 1.3x its 312-line measured
    # size (405.6) -> 450.
    "lookup/enrichment/apple_probe.py": 450,
    # LML#1101 recalibration: 850 -> 750 after the Apple probe left for
    # apple_probe.py (862 -> 719). Smallest multiple of 50 at or above the
    # measured size, NOT a re-derived 1.3x — an extraction hands its headroom
    # back rather than banking it for the next concern to grow into.
    "lookup/enrichment/item.py": 750,
    # LML#1284: the templated streaming search-URL fallbacks, extracted out of
    # item.py to keep that file under its ceiling — the same posture
    # apple_probe.py took for LML#1101, bandcamp_probe.py for LML#1098, and
    # streaming_status.py for LML#1053. Pure functions of the request scalars
    # and the library row, with no dependency on enrich_one's control flow.
    # Set at 250 against a measured ~200 — the next multiple of 50 above the
    # measurement rather than the strict 1.3x (which would license 300), on
    # the same reading LML#1250 used below for a docstring-heavy file.
    # The ceiling moved twice inside LML#1284's own PR (150 -> 200 -> 250) as
    # two review rounds landed, which is the fact worth keeping rather than
    # the itinerary: this file's growth is documentation, and the budget
    # mechanism cannot tell documentation from behavior. The executable body
    # is 9 lines across three functions and every line of the growth is
    # measured fact about the catalog, so the rule for the next person is
    # about the BODY — if that grows, extract rather than raise again.
    "lookup/enrichment/search_urls.py": 250,
    # LML#1053: the per-service streaming-status merge, extracted out of
    # item.py to stay under its own ceiling above — a pure function with no
    # dependency on enrich_one's control flow. 1.3x its ~70-line measured
    # size -> 100.
    # LML#1101: 76 -> 135 lines converting the per-service parameters into
    # per-service maps and adding the unmodelled-service drop log. 1.3x (175.5)
    # -> 200; at the old 100 the file sat exactly on its ceiling with no
    # maintenance headroom at all.
    "lookup/enrichment/streaming_status.py": 200,
    # Recalibrated (LML#1192 review round 4, P0-10): the Wikipedia-URL
    # extractor call gained its own try/except (a failure there must not
    # discard year/release/details already fetched successfully this same
    # request -- the same LML#1049 rationale the DiscogsBreakerOpenError
    # branch above it already followed). Measured 106 lines at this PR's
    # tip. Smallest multiple of 50 at or above 1.3x that -> 150.
    "lookup/enrichment/top1.py": 150,
    # LML#513/#1192 Phase B (docs/plans/lml-1192-wikipedia-artist-bio.md):
    # the read-path resolver -- CachedValue read, served-pair resolution --
    # plus finalize_bio (LML#1192 review round 3, finding 10: collapses the
    # coordinator's post-gate adoption-telemetry + miss-warm-scheduling
    # calls into one) and its two private helpers. Recalibrated (round 4,
    # P0-6): the miss-warm's gate condition split from bio_surfaced to a
    # separate enriched_top1_wiki-derived signal (an artist with no Discogs
    # profile has bio_surfaced permanently False, which made the warm
    # unreachable for exactly the cohort it exists for).
    #
    # Round 5 (the row-is-authoritative read + WikipediaBioHit consumption)
    # grew this to 337 measured lines, not the 308 an earlier version of
    # this comment stated. The standard 1.3x-headroom rule at 337 would
    # license 450 (1.3 x 337 = 438.1, smallest multiple of 50 at or above
    # that) -- NOT 400. 400 is a deliberate under-grant (~19% headroom,
    # not the rule's ~30%), approved by Jake as-is rather than raised to
    # 450: the tighter number is the point.
    "lookup/enrichment/wikipedia_bio.py": 400,
    # LML#513/#1192 Phase B: the bounded miss-warm executor, modeled on
    # lookup/streaming_warm_admission.py -- depth-bound admission, the
    # fetch, and the unsampled fetch-outcome PostHog counters. Measured
    # 171 lines (plan estimated ~75). Smallest multiple of 50 at or above
    # 1.3x that.
    #
    # LML#1192 review round 6, pass 3, A1: landed at exactly 250/250
    # already (pass 2's own budget-driven trim); this pass's fix (a
    # durable attempt record on the truncated-skip branch, matching C2-3's
    # existing WikipediaFetchError treatment) needed the unsampled
    # fetch-outcome PostHog counters extracted to their own module
    # (wikipedia_warm_telemetry.py) to fit without a ceiling raise.
    #
    # LML#1204 item 1: that extraction was module-budget-driven, not a
    # concern boundary — once the emit mechanics consolidated into
    # core.observability.capture_unsampled_counter, the residue (two stat
    # keys, a prefix, a one-line wrapper) dissolved back in here and
    # wikipedia_warm_telemetry.py was deleted. Fits under the unchanged
    # 250 ceiling.
    "lookup/enrichment/wikipedia_warm.py": 250,
    # LML#1192 review round 4, P0-2 / round 5: fetch-validated Wikipedia
    # page selection. Moved down from #1196 (the drain PR) to #1195 (round
    # 5) -- this is the earliest branch that can host it, since it needs
    # clients.wikipedia.WikipediaSummary from #1194, and round 5 makes the
    # background miss-warm (this branch's own wikipedia_warm.py) a second
    # consumer alongside the offline drain. Measured 137 lines when first
    # added; budget set to 200 (smallest multiple of 50 at or above 1.3x).
    # Recalibrated (LML#1192 review round 6, P2-2): opting into
    # lookup.wikipedia_candidates.score_candidates's include_denylisted=True
    # asymmetry -- one new import line, a module-docstring paragraph, and a
    # call-site comment -- measured 204 lines. 1.3x that is 265.2; smallest
    # multiple of 50 at or above -> 300.
    #
    # Recalibrated (LML#1204 items 2 + review): the module gained two
    # review-directed shared concerns -- make_summary_fetcher (the fetch
    # closure both live-fetch callers hand-built) and
    # write_nothing_attempt_outcome (the verdict->attempt-outcome mapping
    # both callers hand-maintained) -- landing at 298/300, the exact
    # four-lines-left condition this table's own comments warn about. The
    # standard 1.3x rule at 298 would license 400 (387.4 rounded up); 350
    # is a deliberate under-grant (~17% headroom), matching the
    # wikipedia_bio.py/wikipedia_url.py tight-on-purpose precedents.
    "lookup/wikipedia_pick_validation.py": 350,
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
    # LML#1233 miss taxonomy. Carved out of `lookup/router.py` rather than
    # written at its `send_to_posthog` site, which had 27 lines of headroom
    # against its own ceiling — the same extract-don't-append policy that
    # produced `endpoint_family.py` and `server_timing_legs.py`. Sized by the
    # standard formula: smallest multiple of 50 at or above 1.3x the 154-line
    # post-change size. Most of that is docstring: the module is two small
    # functions plus the LML#1236 "known gap" note, which is load-bearing
    # context for anyone reading the `miss_clean` series.
    "lookup/miss_kind.py": 250,
    "lookup/models.py": 150,
    # LML#1244 punctuation fold. Carved out of `lookup/matching.py` rather
    # than written at its `artist_matches_item` site, which would have pushed
    # that file past its own 550 ceiling — the same extract-don't-append
    # policy that produced `endpoint_family.py`, `server_timing_legs.py` and
    # `miss_kind.py`. Sized by the standard formula: smallest multiple of 50
    # at or above 1.3x the 80-line measured size. Most of that is docstring —
    # the module is three small functions, and the prose is the load-bearing
    # part, since it records *why* punctuation folds to a space rather than
    # to nothing, and why this fold is deliberately not merged with
    # `library.db._fts_normalize`. Both `lookup/matching.py`'s prefix gate and
    # `lookup/rowless.py`'s equality gate consume it, so drift here would
    # silently split what the two halves of the same lane call one artist.
    # Recalibrated by LML#1257 (150 -> 200, measured 150). The consolidation
    # found the policy has two fidelities that differ on exactly one
    # character: comparison sites fold `_`, query-construction sites must not
    # (db.search folds it already, and folding early starves the significant-
    # word floors -- "Ras_G" stops scoping its own query). This is the one
    # case where the guardrail's usual "extract, don't append" answer is
    # wrong: the whole point of the module is that the fidelities sit
    # adjacent and share `_PUNCTUATION_CHAR`, so splitting them into two
    # files would manufacture exactly the drift LML#1244 and LML#1257 exist
    # to prevent. Sized per the calibration convention: 1.3x150 -> 200.
    #
    # Raised by LML#1250 (200 -> 250, measured 246). Worth being precise about
    # WHY, because the obvious justification -- "same reason as the LML#1257
    # entry above" -- does not actually transfer. #1257's anti-split argument
    # is that the two fidelities share `_PUNCTUATION_CHAR`; the comparison
    # predicates `folded_hit` and `article_stem_hit` share nothing with the
    # fold, so a `name_matching.py` holding both would preserve their coupling
    # just as well. The honest reasons to keep them here are smaller and
    # sufficient: of the ~60 added lines only 8 are executable, which is not a
    # module; and `lookup/rowless.py` already imports the fold and a predicate
    # from this file together, so a split would fan one import into two for no
    # gain in separation.
    #
    # Sized as a minimal-headroom grant (smallest multiple of 50 at or above
    # the measured size, no 1.3x multiplier), the same under-grant
    # `lookup/enrichment/__init__.py` above took, rather than the 350 a fresh
    # 1.3x grant would license. The under-grant is already load-bearing: this
    # PR's review round overran its own first grant on comment prose and had
    # to trim back to fit rather than raise again, which is exactly the
    # pressure the guardrail is for.
    "lookup/name_folding.py": 250,
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
    #
    # Recalibrated 2026-08-05 (LML#1126): the 1650 ceiling above was set to the
    # file's exact measured size, leaving zero headroom, and this ticket's
    # change landed on top of it via a rebase — CI was green on both PRs
    # independently and only the merge order produced the overflow. The growth
    # is irreducible and not a new concern: one `LookupState.upstream_shed`
    # field (that dataclass lives here) plus its two call sites — the copy in
    # `_step_search_pipeline` and the `degraded`/`degraded_reason` pair in
    # `perform_lookup`'s response build. There is no module to extract; the
    # shed-detection logic itself lives in `core/search.py`. Same shape as
    # LML#944's `router.py` recalibration ("an import plus the two irreducible
    # call sites") and the same tight-on-purpose formula: smallest multiple of
    # 50 at or above the new measured 1664, not a re-derived 1.3x. This is the
    # FOURTH consecutive recalibration — the extraction debt named directly
    # above is now overdue and tracked at LML#1142.
    "lookup/orchestrator.py": 1700,
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
    # LML#513 (Phase A of the Wikipedia-preferred-bio program, docs/plans/
    # lml-1192-wikipedia-artist-bio.md): the slug-scored Wikipedia URL
    # extractor -- parsing, the hard-reject denylist, disambig stripping,
    # scoring/tie-break, PickedWikiUrl, and the shadow-telemetry
    # projection/log pair. Well above the plan's ~110 estimate -- the
    # repo's documentation density, not padding; see the PR-A "Deviations
    # from plan" note. Recalibrated post-review (LML#1192 round 2, A1/A2/A4/
    # A5 fixes: the widened hard-reject denylist + regex, the symmetric
    # artist-name disambig strip, and the telemetry fixes all added lines)
    # -- measured 342 at this PR's tip. Smallest multiple of 50 at or above
    # 1.3x that -> 450.
    #
    # Recalibrated DOWN (LML#1192 review round 6): this module hit 446/450
    # -- four lines of headroom -- once round 6's P2-2/P2-3 fixes needed to
    # land, which is exactly the regrowth this guardrail exists to catch.
    # Candidate URL parsing, slug scoring, the hard-reject denylist, and
    # language normalization extracted to the new
    # lookup/wikipedia_candidates.py (see its own budget entry below); this
    # module kept only the SERVING decision (flag + floor gate) and shadow
    # telemetry. Measured 264 lines post-extraction. Left at 450 (rather
    # than shrunk to 1.3x264's 350) the guardrail would stay silent through
    # another ~180 lines of exactly the regrowth that just happened --
    # shrinking it keeps the ceiling meaningful going forward, not just at
    # the moment of this fix.
    "lookup/wikipedia_url.py": 350,
    # New (LML#1192 review round 6): the scoring/rejection policy extracted
    # from lookup/wikipedia_url.py once that module hit its ceiling --
    # URL/slug parsing, the hard-reject denylist, language normalization,
    # and the total-order try-ranking, shared by the synchronous
    # lookup/wikipedia_url.py (include_denylisted=False, decisive) and the
    # fetch-capable lookup/wikipedia_pick_validation.py
    # (include_denylisted=True, a ranking penalty -- see this module's own
    # docstring for the full asymmetry). Measured 357 lines; 1.3x that is
    # 464.1, smallest multiple of 50 at or above -> 500.
    "lookup/wikipedia_candidates.py": 500,
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
