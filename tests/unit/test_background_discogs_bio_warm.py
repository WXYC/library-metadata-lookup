"""Unit tests for lookup/enrichment/background.py's ``maybe_schedule_discogs_bio_warm``
(LML#513/#1192 Phase B: the Discogs ref-warm gate relocated from
``lookup/enrichment/__init__.py``).

LML#1192 review round 6, pass 3, A3: through round 6 pass 2, this gate
additionally required ``served_bio_is_discogs`` — reasoning that a
Wikipedia-served bio leaves the Discogs profile "unrendered," so
deep-parsing its refs would be the same quota burn LML#504 prohibits. That
reasoning is false: ``profile_tokens`` (``lookup/enrichment/item.py:616-618``)
is populated from the raw Discogs profile whenever ``ctx.extended and
is_artist_derived_eligible`` — independently of which text won the
``artist_bio`` slot — and ships on the SAME response. The api.yaml wire
contract (``generated/api_models.py``'s ``profile_tokens`` field)
explicitly promises: "Pair with ``LookupRequest.warm_cache=true`` on
write-path calls to progressively populate the cache so subsequent reads
render richer." With ``served_bio_is_discogs`` in the gate, that promise
was silently broken for every artist whose bio comes from Wikipedia: their
``profile_tokens`` refs never populate no matter how many
``warm_cache=true`` calls follow.

The gate is now ``warm_cache and top1_bio and profile_tokens_shipped`` --
``profile_tokens_shipped`` is the caller's own answer to "did
``profile_tokens`` actually reach THIS response" (``top1_enriched_result
.profile_tokens is not None`` at the ``__init__.py`` call site), which is
the exact fact the wire-contract promise is about and folds in BOTH of
``profile_tokens``'s own conditions (``ctx.extended`` and
``is_artist_derived_eligible``) without this module needing to re-derive
either one. This still correctly skips the warm when the LML#504 identity
gate rejects the artist entirely (``profile_tokens`` doesn't ship then
either -- see ``TestArtistIdentitySplitGate.test_warm_cache_skipped_when_bio_suppressed``
in ``test_enrichment.py``), while no longer skipping just because Wikipedia
won the ``artist_bio`` slot.
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
                profile_tokens_shipped=True,
                discogs_service=discogs_service,
            )
        mock_create_task.assert_called_once()

    async def test_skips_when_warm_cache_false(self):
        discogs_service = AsyncMock(spec=DiscogsService)
        with patch("lookup.enrichment.background.asyncio.create_task") as mock_create_task:
            background.maybe_schedule_discogs_bio_warm(
                warm_cache=False,
                top1_bio="A Discogs profile.",
                profile_tokens_shipped=True,
                discogs_service=discogs_service,
            )
        mock_create_task.assert_not_called()

    async def test_skips_when_top1_bio_is_none(self):
        discogs_service = AsyncMock(spec=DiscogsService)
        with patch("lookup.enrichment.background.asyncio.create_task") as mock_create_task:
            background.maybe_schedule_discogs_bio_warm(
                warm_cache=True,
                top1_bio=None,
                profile_tokens_shipped=True,
                discogs_service=discogs_service,
            )
        mock_create_task.assert_not_called()

    async def test_skips_when_profile_tokens_did_not_ship(self):
        # Covers BOTH of profile_tokens's own conditions collapsing to
        # False: extended=False, or the LML#504 identity gate rejected the
        # artist outright (is_artist_derived_eligible=False) -- either way,
        # warming refs for a field this response doesn't carry is the one
        # case LML#504's original rule still applies to.
        discogs_service = AsyncMock(spec=DiscogsService)
        with patch("lookup.enrichment.background.asyncio.create_task") as mock_create_task:
            background.maybe_schedule_discogs_bio_warm(
                warm_cache=True,
                top1_bio="A Discogs profile.",
                profile_tokens_shipped=False,
                discogs_service=discogs_service,
            )
        mock_create_task.assert_not_called()

    async def test_schedules_even_when_wikipedia_is_served_as_the_artist_bio(self):
        # LML#1192 review round 6, pass 3, A3: the fix itself. profile_tokens
        # is Discogs-derived and ships on the response regardless of which
        # text won the artist_bio slot -- a Wikipedia-served bio must NOT
        # suppress this warm, or every warm_cache=true call for that artist
        # is silently a no-op forever, contradicting the shipped contract
        # text ("subsequent reads render richer"). This caller has no
        # notion of "which source served artist_bio" at all any more --
        # profile_tokens_shipped=True is enough on its own.
        discogs_service = AsyncMock(spec=DiscogsService)
        with patch("lookup.enrichment.background.asyncio.create_task") as mock_create_task:
            mock_create_task.return_value = AsyncMock()
            background.maybe_schedule_discogs_bio_warm(
                warm_cache=True,
                top1_bio="A Discogs profile.",
                profile_tokens_shipped=True,
                discogs_service=discogs_service,
            )
        mock_create_task.assert_called_once()

    async def test_scheduled_task_is_anchored_in_background_tasks(self):
        discogs_service = AsyncMock(spec=DiscogsService)
        with patch("lookup.enrichment.background._warm_bio_cache", new_callable=AsyncMock):
            background.maybe_schedule_discogs_bio_warm(
                warm_cache=True,
                top1_bio="A Discogs profile.",
                profile_tokens_shipped=True,
                discogs_service=discogs_service,
            )
            assert len(background._background_tasks) == 1
            await list(background._background_tasks)[0]
        assert len(background._background_tasks) == 0
