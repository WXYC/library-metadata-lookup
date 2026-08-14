"""Unit tests for lookup/enrichment/background.py's ``maybe_schedule_discogs_bio_warm``
(LML#513/#1192 Phase B: the Discogs ref-warm gate relocated from
``lookup/enrichment/__init__.py``, re-specified for the Wikipedia-preferred-bio
swap).

Before this ticket, ``top1_bio_surfaced`` alone answered "was the Discogs
bio surfaced" — true whenever ``enriched[0][1].artist_bio`` was truthy,
because the only source of ``artist_bio`` was ever the Discogs profile. Once
Phase B can serve a Wikipedia extract instead, that's no longer true: a
surfaced bio might be Wikipedia prose, and deep-parsing the UNRENDERED
Discogs profile's refs in that case is exactly the quota burn the LML#504
comment prohibits. ``served_bio_is_discogs`` is the new, additional
requirement.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from discogs.service import DiscogsService
from lookup.enrichment import background


@pytest.fixture(autouse=True)
def _reset_background_tasks():
    background._background_tasks.clear()
    yield
    background._background_tasks.clear()


@pytest.mark.asyncio
class TestMaybeScheduleDiscogsBioWarm:
    async def test_schedules_when_all_conditions_hold(self):
        discogs_service = AsyncMock(spec=DiscogsService)
        with patch("lookup.enrichment.background.asyncio.create_task") as mock_create_task:
            mock_create_task.return_value = AsyncMock()
            background.maybe_schedule_discogs_bio_warm(
                warm_cache=True,
                top1_bio="A Discogs profile.",
                top1_bio_surfaced=True,
                served_bio_is_discogs=True,
                discogs_service=discogs_service,
            )
        mock_create_task.assert_called_once()

    async def test_skips_when_warm_cache_false(self):
        discogs_service = AsyncMock(spec=DiscogsService)
        with patch("lookup.enrichment.background.asyncio.create_task") as mock_create_task:
            background.maybe_schedule_discogs_bio_warm(
                warm_cache=False,
                top1_bio="A Discogs profile.",
                top1_bio_surfaced=True,
                served_bio_is_discogs=True,
                discogs_service=discogs_service,
            )
        mock_create_task.assert_not_called()

    async def test_skips_when_top1_bio_is_none(self):
        discogs_service = AsyncMock(spec=DiscogsService)
        with patch("lookup.enrichment.background.asyncio.create_task") as mock_create_task:
            background.maybe_schedule_discogs_bio_warm(
                warm_cache=True,
                top1_bio=None,
                top1_bio_surfaced=True,
                served_bio_is_discogs=True,
                discogs_service=discogs_service,
            )
        mock_create_task.assert_not_called()

    async def test_skips_when_bio_not_surfaced(self):
        discogs_service = AsyncMock(spec=DiscogsService)
        with patch("lookup.enrichment.background.asyncio.create_task") as mock_create_task:
            background.maybe_schedule_discogs_bio_warm(
                warm_cache=True,
                top1_bio="A Discogs profile.",
                top1_bio_surfaced=False,
                served_bio_is_discogs=True,
                discogs_service=discogs_service,
            )
        mock_create_task.assert_not_called()

    async def test_skips_when_served_bio_is_wikipedia_not_discogs(self):
        """The LML#513/#1192 re-spec: a surfaced bio that ISN'T the Discogs
        text (Phase B served a Wikipedia extract instead) must not trigger
        the Discogs deep-parse warm — nothing renders it."""
        discogs_service = AsyncMock(spec=DiscogsService)
        with patch("lookup.enrichment.background.asyncio.create_task") as mock_create_task:
            background.maybe_schedule_discogs_bio_warm(
                warm_cache=True,
                top1_bio="A Discogs profile.",
                top1_bio_surfaced=True,
                served_bio_is_discogs=False,
                discogs_service=discogs_service,
            )
        mock_create_task.assert_not_called()

    async def test_scheduled_task_is_anchored_in_background_tasks(self):
        discogs_service = AsyncMock(spec=DiscogsService)
        with patch("lookup.enrichment.background._warm_bio_cache", new_callable=AsyncMock):
            background.maybe_schedule_discogs_bio_warm(
                warm_cache=True,
                top1_bio="A Discogs profile.",
                top1_bio_surfaced=True,
                served_bio_is_discogs=True,
                discogs_service=discogs_service,
            )
            assert len(background._background_tasks) == 1
            await list(background._background_tasks)[0]
        assert len(background._background_tasks) == 0
