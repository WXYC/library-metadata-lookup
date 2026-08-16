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


@pytest.fixture(autouse=True)
def _reset_module_state():
    """LML#748 lesson: process-global warm state must reset per test."""
    wikipedia_warm._pending_artist_ids.clear()
    wikipedia_warm._background_tasks.clear()
    wikipedia_warm._warm_semaphore = None
    yield
    wikipedia_warm._pending_artist_ids.clear()
    wikipedia_warm._background_tasks.clear()
    wikipedia_warm._warm_semaphore = None


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
        # The fetch closure reuses wikipedia_title_from_url's conversion --
        # a page TITLE (spaces, not underscores), never a raw URL.
        mock_get_summary.assert_awaited_once_with("Stereolab", "en", max_retries=1)

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
