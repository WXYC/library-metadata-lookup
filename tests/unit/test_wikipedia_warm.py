"""Unit tests for lookup/enrichment/wikipedia_warm.py — the bounded miss-warm
executor for the Wikipedia bio cache (Phase B, LML#513/#1192).

Modeled on ``lookup/streaming_warm_admission.py``: bounded queue depth (never
an unbounded task set behind a bare semaphore), concurrency 2, a dedicated
shed stat key. LML#748 lesson (cited in the plan's Testing section): reset
module-level warm state per test, since ``_pending_artist_ids`` /
``_background_tasks`` are process-global.

LML#1192 review round 5: ``schedule_wikipedia_bio_warm``/``_run_warm`` no
longer take a pre-computed ``PickedWikiUrl`` -- they take ``artist_name``/
``urls`` and run their own fetch-validated candidate selection
(``lookup.wikipedia_pick_validation.resolve_and_validate_pick``), the same
fix the offline drain got in review round 4's P0-2. This closes the
infinite-miss-warm loop a single-candidate, unvalidated pick could cause:
see ``tests/unit/test_wikipedia_bio.py::TestValidatedWarmRoundTrip`` for the
read-side half of that story.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from wxyc_fastapi.observability import get_cache_stats, init_cache_stats

from clients.wikipedia import WikipediaFetchError, WikipediaSummary
from entity.sources import PgSource
from lookup.enrichment import wikipedia_warm

_ARTIST_NAME = "Stereolab"
_URLS = ["https://en.wikipedia.org/wiki/Stereolab"]


# LML#748 module-state reset: owned suite-wide by tests/conftest.py's
# autouse ``_reset_wikipedia_warm_state`` (LML#1192 cross-PR review, round 7
# promoted it there -- the state is process-global, so a file-local reset
# left every OTHER file unprotected; see the conftest docstring).


@pytest.fixture(autouse=True)
def _cache_stats():
    init_cache_stats()
    yield


@pytest.mark.asyncio
class TestScheduleWikipediaBioWarm:
    async def test_schedules_a_task_and_returns_true(self):
        pg = AsyncMock(spec=PgSource)
        with patch.object(wikipedia_warm, "_run_warm", new_callable=AsyncMock):
            scheduled = wikipedia_warm.schedule_wikipedia_bio_warm(
                discogs_artist_id=99, artist_name=_ARTIST_NAME, urls=_URLS, discogs_cache_pg=pg
            )
            assert scheduled is True
            assert 99 in wikipedia_warm._pending_artist_ids
            assert len(wikipedia_warm._background_tasks) == 1
            # Let the task run so the done-callback removes it (avoids
            # "Task was destroyed but it is pending" warnings across tests).
            await list(wikipedia_warm._background_tasks)[0]

    async def test_dedupes_an_already_pending_artist(self):
        pg = AsyncMock(spec=PgSource)
        wikipedia_warm._pending_artist_ids.add(99)
        scheduled = wikipedia_warm.schedule_wikipedia_bio_warm(
            discogs_artist_id=99, artist_name=_ARTIST_NAME, urls=_URLS, discogs_cache_pg=pg
        )
        assert scheduled is False
        assert len(wikipedia_warm._background_tasks) == 0

    async def test_sheds_and_records_the_stat_when_the_depth_bound_is_exceeded(self):
        pg = AsyncMock(spec=PgSource)
        bound = wikipedia_warm._queue_depth_bound()
        wikipedia_warm._pending_artist_ids.update(range(1, bound + 1))

        scheduled = wikipedia_warm.schedule_wikipedia_bio_warm(
            discogs_artist_id=99999, artist_name=_ARTIST_NAME, urls=_URLS, discogs_cache_pg=pg
        )

        assert scheduled is False
        assert 99999 not in wikipedia_warm._pending_artist_ids
        stats = get_cache_stats()
        assert stats.get(wikipedia_warm.WARM_SHED_STAT_KEY) == 1

    async def test_create_task_failure_does_not_leak_the_dedup_key(self):
        # LML#1192 review round 3, finding 7: the key was added to
        # _pending_artist_ids BEFORE asyncio.create_task -- if create_task
        # itself raised (e.g. no running loop), the key was already
        # registered and nothing would ever remove it (no task, no
        # done-callback), permanently suppressing this artist's warm for
        # the rest of the process's life. lookup/streaming_warm.py's
        # _enqueue_streaming_warm (:186-189) creates the task FIRST for
        # exactly this reason; match that ordering.
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_warm.asyncio.create_task",
            side_effect=RuntimeError("no running event loop"),
        ):
            with pytest.raises(RuntimeError):
                wikipedia_warm.schedule_wikipedia_bio_warm(
                    discogs_artist_id=99, artist_name=_ARTIST_NAME, urls=_URLS, discogs_cache_pg=pg
                )
        assert 99 not in wikipedia_warm._pending_artist_ids

    async def test_task_removes_itself_from_pending_when_done(self):
        pg = AsyncMock(spec=PgSource)
        with patch.object(wikipedia_warm, "_run_warm", new_callable=AsyncMock):
            wikipedia_warm.schedule_wikipedia_bio_warm(
                discogs_artist_id=99, artist_name=_ARTIST_NAME, urls=_URLS, discogs_cache_pg=pg
            )
            task = list(wikipedia_warm._background_tasks)[0]
            await task
        assert 99 not in wikipedia_warm._pending_artist_ids
        assert len(wikipedia_warm._background_tasks) == 0


@pytest.mark.asyncio
class TestRunWarm:
    """LML#1192 review round 5: ``_run_warm`` now delegates candidate
    selection to ``resolve_and_validate_pick`` instead of trusting a single
    pre-computed pick -- these tests pin the WIRING (the fetch closure
    reuses ``WikipediaClient``/``wikipedia_title_from_url``, the semaphore
    still wraps the whole selection, ``WikipediaFetchError`` still writes
    nothing), not the picker's own candidate-ranking logic (that is
    ``tests/unit/test_wikipedia_pick_validation.py``'s job).
    """

    async def test_positive_summary_writes_extract_and_captures_fetch_ok(self):
        pg = AsyncMock(spec=PgSource)
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
                new_callable=AsyncMock,
                return_value=WikipediaSummary(extract="Stereolab are a band."),
            ) as mock_get_summary,
            patch(
                "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
            patch.object(wikipedia_warm, "_capture_fetch_outcome") as mock_capture,
        ):
            await wikipedia_warm._run_warm(99, _ARTIST_NAME, _URLS, pg)

        mock_set.assert_awaited_once_with(
            pg,
            discogs_artist_id=99,
            wikipedia_url=_URLS[0],
            slug_score=pytest.approx(100.0),
            lang="en",
            extract="Stereolab are a band.",
        )
        mock_capture.assert_called_once_with(wikipedia_warm.FETCH_OK_STAT_KEY)
        # The shared make_summary_fetcher (LML#1204 item 2) reuses
        # wikipedia_title_from_url's conversion -- a page TITLE (spaces, not
        # underscores), never a raw URL -- with this warm's single-retry
        # knob and no rate limiter.
        mock_get_summary.assert_awaited_once_with(
            "Stereolab", "en", max_retries=1, rate_limiter=None
        )

    async def test_a_rejected_candidate_falls_through_to_the_next_one(self):
        # LML#1192 review round 5's whole point: a single-candidate,
        # unvalidated pick could write the WRONG page (the Low/Sade case).
        # Two candidate URLs here -- the bare "Sun_Ra" scores highest (an
        # exact match) and is tried FIRST; the fixture rejects it, so the
        # qualified url must still be tried and win -- proving the warm
        # actually tries more than one candidate, not just whichever one
        # scored highest.
        pg = AsyncMock(spec=PgSource)
        urls = [
            "https://en.wikipedia.org/wiki/Sun_Ra_(disambiguation)",
            "https://en.wikipedia.org/wiki/Sun_Ra",
        ]
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
                new_callable=AsyncMock,
                side_effect=[None, WikipediaSummary(extract="Sun Ra was a musician.")],
            ),
            patch(
                "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
        ):
            await wikipedia_warm._run_warm(99, "Sun Ra", urls, pg)

        assert (
            mock_set.await_args.kwargs["wikipedia_url"]
            == "https://en.wikipedia.org/wiki/Sun_Ra_(disambiguation)"
        )
        assert mock_set.await_args.kwargs["extract"] == "Sun Ra was a musician."

    async def test_all_candidates_rejected_writes_null_extract_and_captures_fetch_reject(self):
        pg = AsyncMock(spec=PgSource)
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
            patch.object(wikipedia_warm, "_capture_fetch_outcome") as mock_capture,
        ):
            await wikipedia_warm._run_warm(99, _ARTIST_NAME, _URLS, pg)

        assert mock_set.await_args.kwargs["extract"] is None
        mock_capture.assert_called_once_with(wikipedia_warm.FETCH_REJECT_STAT_KEY)

    async def test_transient_fetch_error_writes_nothing(self):
        pg = AsyncMock(spec=PgSource)
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
                new_callable=AsyncMock,
                side_effect=WikipediaFetchError("timed out"),
            ),
            patch(
                "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
        ):
            # Must not raise.
            await wikipedia_warm._run_warm(99, _ARTIST_NAME, _URLS, pg)

        mock_set.assert_not_awaited()

    async def test_transient_fetch_error_records_a_durable_attempt(self):
        # LML#1192 review round 6, C2-3: the drain's P0-8 attempt record
        # was never shared with the runtime warm task -- a couldn't-ask
        # here wrote NOTHING anywhere, so a discogs-cache PG outage
        # silently inverted "cache down, degrade quietly" into sustained
        # outbound Wikipedia traffic with a 0% write-back rate. The warm
        # task now records the same durable "fetch_error" attempt the
        # drain already does.
        pg = AsyncMock(spec=PgSource)
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
                new_callable=AsyncMock,
                side_effect=WikipediaFetchError("timed out"),
            ),
            patch(
                "lookup.enrichment.wikipedia_warm.record_artist_wikipedia_bio_attempt",
                new_callable=AsyncMock,
            ) as mock_record,
        ):
            await wikipedia_warm._run_warm(99, _ARTIST_NAME, _URLS, pg)

        mock_record.assert_awaited_once_with(pg, discogs_artist_id=99, outcome="fetch_error")

    async def test_no_wikipedia_url_at_all_writes_nothing(self):
        # Defensive: schedule_wikipedia_bio_warm is only ever called after a
        # genuine miss whose sync pick already cleared the floor, so this
        # should be unreachable in practice -- but urls could in principle
        # be empty (e.g. a caller bug), and this must degrade gracefully,
        # not raise.
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
            new_callable=AsyncMock,
        ) as mock_set:
            await wikipedia_warm._run_warm(99, _ARTIST_NAME, [], pg)
        mock_set.assert_not_awaited()

    async def test_unexpected_exception_is_swallowed(self):
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise -- the task must never propagate to the event loop.
            await wikipedia_warm._run_warm(99, _ARTIST_NAME, _URLS, pg)

    async def test_candidate_fetches_run_inside_the_warm_semaphore(self):
        # The semaphore must still wrap the WHOLE candidate-validation
        # sequence (potentially several fetches), not just one -- confirmed
        # by observing it's acquired exactly once for the whole _run_warm
        # call regardless of how many candidates get tried.
        pg = AsyncMock(spec=PgSource)
        urls = [
            "https://en.wikipedia.org/wiki/Sun_Ra_(disambiguation)",
            "https://en.wikipedia.org/wiki/Sun_Ra",
        ]
        wikipedia_warm._warm_semaphore = AsyncMock()
        wikipedia_warm._warm_semaphore.__aenter__ = AsyncMock()
        wikipedia_warm._warm_semaphore.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
                new_callable=AsyncMock,
                side_effect=[None, WikipediaSummary(extract="Sun Ra was a musician.")],
            ),
            patch(
                "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ),
        ):
            await wikipedia_warm._run_warm(99, "Sun Ra", urls, pg)
        wikipedia_warm._warm_semaphore.__aenter__.assert_awaited_once()
        wikipedia_warm._warm_semaphore.__aexit__.assert_awaited_once()

    async def test_a_truncated_below_floor_fallback_writes_nothing_to_the_content_cache(self):
        # LML#1192 review round 6, C2-1: with only above-floor candidates
        # rejected WITHIN the cap, and at least one ranked candidate left
        # untried by MAX_CANDIDATES_PER_WARM, the fallback is a truncation,
        # not an exhausted negative -- writing here would key an
        # authoritative 7-day negative to a URL that was never fetched,
        # for an artist whose correct page was never tried.
        pg = AsyncMock(spec=PgSource)
        cap = wikipedia_warm.MAX_CANDIDATES_PER_WARM
        langs = ["en", "fr", "de", "es", "it", "pt", "nl", "sv", "pl", "da"]
        urls = [f"https://{lang}.wikipedia.org/wiki/Stereolab" for lang in langs]
        assert len(urls) > cap, "fixture must supply more candidates than the cap to be meaningful"
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
                new_callable=AsyncMock,
                return_value=None,  # every within-cap candidate rejected
            ),
            patch(
                "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
            patch.object(wikipedia_warm, "_capture_fetch_outcome") as mock_capture,
        ):
            await wikipedia_warm._run_warm(99, "Stereolab", urls, pg)

        mock_set.assert_not_awaited()
        mock_capture.assert_not_called()

    async def test_a_truncated_result_still_records_a_durable_attempt(self):
        # LML#1192 review round 6 pass 3, A1: the C2-1 fix above (pass 2)
        # correctly stopped the truncated-fallback write, but recorded
        # NOTHING anywhere else either -- reproduced with the reviewer's
        # exact scenario (four ranked candidates, cap 3, none of the three
        # tried candidates validate): nothing lands in the content cache
        # AND nothing lands in the attempt table, unlike the sibling
        # WikipediaFetchError branch (C2-3, pass 1), which already records
        # a durable "fetch_error" attempt on its own couldn't-ask. Three
        # REAL live fetches happened here (attempted_a_live_fetch is True)
        # -- this must record its own durable attempt (a distinct outcome,
        # "truncated", since this is not a transient couldn't-ask -- we
        # asked, got real answers, and simply ran out of budget) so the
        # offline drain's --retry-misses seed can eventually pick this
        # artist back up, the same shape C2-3 already established.
        pg = AsyncMock(spec=PgSource)
        cap = wikipedia_warm.MAX_CANDIDATES_PER_WARM
        langs = ["en", "fr", "de", "es"]
        urls = [f"https://{lang}.wikipedia.org/wiki/Stereolab" for lang in langs]
        assert len(urls) == cap + 1, "the reviewer's exact four-candidates-cap-3 reproduction"
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
                new_callable=AsyncMock,
                return_value=None,  # every within-cap candidate rejected
            ) as mock_get_summary,
            patch(
                "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
            patch(
                "lookup.enrichment.wikipedia_warm.record_artist_wikipedia_bio_attempt",
                new_callable=AsyncMock,
            ) as mock_record,
        ):
            await wikipedia_warm._run_warm(99, "Stereolab", urls, pg)

        assert mock_get_summary.await_count == cap, "the reviewer's own reproduction shape"
        mock_set.assert_not_awaited()
        mock_record.assert_awaited_once_with(pg, discogs_artist_id=99, outcome="truncated")

    async def test_a_below_floor_pick_with_zero_live_fetches_writes_nothing(self):
        # LML#1192 review round 6, C2-1: no above-floor candidate exists at
        # all, so the loop never runs and nothing was ever actually asked
        # about -- "we didn't ask" must never produce a negative either,
        # independent of the truncation case above.
        pg = AsyncMock(spec=PgSource)
        urls = ["https://en.wikipedia.org/wiki/Totally_Unrelated_Page"]
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
                new_callable=AsyncMock,
            ) as mock_get_summary,
            patch(
                "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
        ):
            await wikipedia_warm._run_warm(99, "Stereolab", urls, pg)

        mock_get_summary.assert_not_awaited()
        mock_set.assert_not_awaited()

    async def test_candidates_tried_are_capped(self):
        # LML#1192 review round 5, MAX_CANDIDATES_PER_WARM: a multi-
        # candidate artist must not hold the semaphore permit for an
        # unbounded number of sequential fetches -- confirm get_summary is
        # never called more than the documented cap, even when every
        # candidate URL supplied would otherwise be tried. Same slug, many
        # language subdomains -- verified these all score 100.0 (an exact
        # slug match is language-independent), so every one clears the
        # floor and would be tried absent a cap.
        pg = AsyncMock(spec=PgSource)
        cap = wikipedia_warm.MAX_CANDIDATES_PER_WARM
        langs = ["en", "fr", "de", "es", "it", "pt", "nl", "sv", "pl", "da"]
        urls = [f"https://{lang}.wikipedia.org/wiki/Stereolab" for lang in langs]
        assert len(urls) > cap, "fixture must supply more candidates than the cap to be meaningful"
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
                new_callable=AsyncMock,
                return_value=None,  # every candidate rejected
            ) as mock_get_summary,
            patch(
                "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ),
        ):
            await wikipedia_warm._run_warm(99, "Stereolab", urls, pg)
        assert mock_get_summary.await_count == cap


@pytest.mark.asyncio
class TestNoResolvablePickRecordsAttempt:
    """LML#1204 item 2: the no-resolvable-pick branch used to diverge from
    the offline drain (which records a durable ``unresolvable`` attempt) by
    recording nothing — an attempt that found no resolvable candidate is
    still information, and without the record the artist resurfaces as a
    schedulable miss forever."""

    async def test_no_resolvable_pick_records_a_durable_unresolvable_attempt(self):
        from entity.artist_wikipedia_bio_attempt import OUTCOME_UNRESOLVABLE

        pg = AsyncMock(spec=PgSource)
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
            patch(
                "lookup.enrichment.wikipedia_warm.record_artist_wikipedia_bio_attempt",
                new_callable=AsyncMock,
            ) as mock_record,
        ):
            await wikipedia_warm._run_warm(99, _ARTIST_NAME, [], pg)

        mock_set.assert_not_awaited()
        mock_record.assert_awaited_once_with(pg, discogs_artist_id=99, outcome=OUTCOME_UNRESOLVABLE)


@pytest.mark.asyncio
class TestZeroFetchDeclineRecordsAttempt:
    """LML#1204 review: the below-floor zero-fetch decline used to return
    without any durable record — leaving the artist a schedulable miss
    forever, the exact pathology the attempt record exists to prevent, and
    a divergence from the offline drain (which durably records its declines
    as content rows). The warm now records a ``declined`` attempt via the
    shared verdict mapping."""

    async def test_zero_fetch_decline_records_a_durable_declined_attempt(self):
        from entity.artist_wikipedia_bio_attempt import OUTCOME_DECLINED

        pg = AsyncMock(spec=PgSource)
        # A wikipedia.org URL whose slug can't clear the floor against the
        # artist name: below-floor pick, no candidate ranked, zero fetches.
        urls = ["https://en.wikipedia.org/wiki/Something_Entirely_Different"]
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
            patch(
                "lookup.enrichment.wikipedia_warm.record_artist_wikipedia_bio_attempt",
                new_callable=AsyncMock,
            ) as mock_record,
        ):
            await wikipedia_warm._run_warm(99, "Stereolab", urls, pg)

        mock_set.assert_not_awaited()
        mock_record.assert_awaited_once_with(pg, discogs_artist_id=99, outcome=OUTCOME_DECLINED)
