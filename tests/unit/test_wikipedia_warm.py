"""Unit tests for lookup/enrichment/wikipedia_warm.py — the bounded miss-warm
executor for the Wikipedia bio cache (Phase B, LML#513/#1192).

Modeled on ``lookup/streaming_warm_admission.py``: bounded queue depth (never
an unbounded task set behind a bare semaphore), concurrency 2, a dedicated
shed stat key. LML#748 lesson (cited in the plan's Testing section): reset
module-level warm state per test, since ``_pending_artist_ids`` /
``_background_tasks`` are process-global.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from wxyc_fastapi.observability import get_cache_stats, init_cache_stats

from clients.wikipedia import WikipediaFetchError, WikipediaSummary
from entity.sources import PgSource
from lookup.enrichment import wikipedia_warm
from lookup.wikipedia_url import PickedWikiUrl

_PICK = PickedWikiUrl(
    url="https://en.wikipedia.org/wiki/Stereolab", lang="en", slug_score=97.0, below_floor=False
)


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
                discogs_artist_id=99, pick=_PICK, discogs_cache_pg=pg
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
            discogs_artist_id=99, pick=_PICK, discogs_cache_pg=pg
        )
        assert scheduled is False
        assert len(wikipedia_warm._background_tasks) == 0

    async def test_sheds_and_records_the_stat_when_the_depth_bound_is_exceeded(self):
        pg = AsyncMock(spec=PgSource)
        bound = wikipedia_warm._queue_depth_bound()
        wikipedia_warm._pending_artist_ids.update(range(1, bound + 1))

        scheduled = wikipedia_warm.schedule_wikipedia_bio_warm(
            discogs_artist_id=99999, pick=_PICK, discogs_cache_pg=pg
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
                    discogs_artist_id=99, pick=_PICK, discogs_cache_pg=pg
                )
        assert 99 not in wikipedia_warm._pending_artist_ids

    async def test_task_removes_itself_from_pending_when_done(self):
        pg = AsyncMock(spec=PgSource)
        with patch.object(wikipedia_warm, "_run_warm", new_callable=AsyncMock):
            wikipedia_warm.schedule_wikipedia_bio_warm(
                discogs_artist_id=99, pick=_PICK, discogs_cache_pg=pg
            )
            task = list(wikipedia_warm._background_tasks)[0]
            await task
        assert 99 not in wikipedia_warm._pending_artist_ids
        assert len(wikipedia_warm._background_tasks) == 0


@pytest.mark.asyncio
class TestRunWarm:
    async def test_positive_summary_writes_extract_and_captures_fetch_ok(self):
        pg = AsyncMock(spec=PgSource)
        with (
            patch(
                "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
                new_callable=AsyncMock,
                return_value=WikipediaSummary(extract="Stereolab are a band."),
            ),
            patch(
                "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
            patch.object(wikipedia_warm, "_capture_fetch_outcome") as mock_capture,
        ):
            await wikipedia_warm._run_warm(99, _PICK, pg)

        mock_set.assert_awaited_once_with(
            pg,
            discogs_artist_id=99,
            wikipedia_url=_PICK.url,
            slug_score=_PICK.slug_score,
            lang=_PICK.lang,
            extract="Stereolab are a band.",
        )
        mock_capture.assert_called_once_with(wikipedia_warm.FETCH_OK_STAT_KEY)

    async def test_negative_summary_writes_null_extract_and_captures_fetch_reject(self):
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
            await wikipedia_warm._run_warm(99, _PICK, pg)

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
            await wikipedia_warm._run_warm(99, _PICK, pg)

        mock_set.assert_not_awaited()

    async def test_unparseable_title_writes_nothing(self):
        pg = AsyncMock(spec=PgSource)
        bad_pick = PickedWikiUrl(
            url="https://en.wikipedia.org/not-a-wiki-url",
            lang="en",
            slug_score=90.0,
            below_floor=False,
        )
        with patch(
            "lookup.enrichment.wikipedia_warm.set_cached_artist_wikipedia_bio",
            new_callable=AsyncMock,
        ) as mock_set:
            await wikipedia_warm._run_warm(99, bad_pick, pg)
        mock_set.assert_not_awaited()

    async def test_unexpected_exception_is_swallowed(self):
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise -- the task must never propagate to the event loop.
            await wikipedia_warm._run_warm(99, _PICK, pg)
