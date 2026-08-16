"""Integration test for the full validated-warm round trip (LML#1192 review round 5).

This is the coordinator's own red test, run end to end against real
Postgres rather than mocked: a request path whose synchronous, unvalidated
pick names a WRONG page (the Low/Sade shape — a bare disambiguation-page
url), a background warm that fetch-validates and writes the RIGHT page
under a DIFFERENT url, and a second request (same, unchanged sync pick)
that must see a cache HIT serving the validated content — not a miss that
schedules another warm forever.

Exercises three real layers together: ``entity/artist_wikipedia_bio.py``'s
authoritative-by-``discogs_artist_id`` read/write, ``lookup/enrichment/
wikipedia_warm.py``'s ``_run_warm`` (the real function, only its
``WikipediaClient.get_summary`` calls are faked), and ``lookup/enrichment/
wikipedia_bio.py``'s ``resolve_served_bio`` (also real). ``schedule_wikipedia_bio_warm``
itself is not used here (it's fire-and-forget over ``asyncio.create_task`` —
awkward to await deterministically in a test); ``_run_warm`` is awaited
directly, which is what the scheduled task's body actually runs.

Run with: pytest -m pg -v tests/integration/test_wikipedia_bio_warm_round_trip.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from clients.wikipedia import WikipediaSummary
from discogs.models import ArtistDetails
from entity.artist_wikipedia_bio import set_up_artist_wikipedia_bio_schema
from lookup.enrichment import wikipedia_bio, wikipedia_warm
from lookup.wikipedia_url import PickedWikiUrl
from tests.integration.conftest import skip_if_named_tables_populated


@pytest_asyncio.fixture(autouse=True)
async def set_up_cache_schema(pg_pool, pg_source):
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "artist_wikipedia_bio"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.artist_wikipedia_bio")
    await set_up_artist_wikipedia_bio_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.artist_wikipedia_bio")


@pytest.mark.pg
class TestValidatedWarmClosesTheInfiniteMissLoop:
    @pytest.mark.asyncio
    async def test_warm_then_reread_with_the_unchanged_sync_pick_is_a_hit(
        self, pg_source, monkeypatch
    ):
        monkeypatch.setenv("LML_BIO_PREFER_WIKIPEDIA", "true")
        artist_id = 12345
        # The sync pick: unvalidated (no live fetch), names the bare,
        # disambiguation-page url -- exactly what pick_artist_wikipedia_url
        # would return for "Low" with no way to know it's the wrong page.
        sync_pick = PickedWikiUrl(
            url="https://en.wikipedia.org/wiki/Low", lang="en", slug_score=75.0, below_floor=False
        )
        details = ArtistDetails(
            artist_id=artist_id,
            name="Low",
            urls=[
                "https://en.wikipedia.org/wiki/Low",
                "https://en.wikipedia.org/wiki/Low_(band)",
            ],
        )

        # -- First request: cache is empty, so this is a genuine miss.
        first = await wikipedia_bio.resolve_served_bio(
            sync_pick, "A Discogs bio.", details, pg_source
        )
        assert first.source is wikipedia_bio.BioSource.DISCOGS
        assert first.miss_details is details

        # -- The scheduled warm runs: the live fetch rejects the bare
        # (disambiguation) page and validates the qualified (real) one.
        async def fake_get_summary(title, lang, *, max_retries=1):
            if title == "Low":
                return None  # disambiguation page -- rejected
            return WikipediaSummary(extract="Low are a band.")

        with patch(
            "lookup.enrichment.wikipedia_warm.WikipediaClient.get_summary",
            new_callable=AsyncMock,
            side_effect=fake_get_summary,
        ):
            await wikipedia_warm._run_warm(artist_id, details.name, details.urls, pg_source)

        # -- Second request: the SAME (unchanged) sync pick -- the request
        # path never re-validates. Before LML#1192 review round 5's fix,
        # the cache read was keyed on wikipedia_url = sync_pick.url ("Low"),
        # which the warm did NOT write under (it wrote "Low_(band)") --
        # a permanent miss, forever re-scheduling a warm. The row is now
        # authoritative: this must be a HIT, serving the row's own url.
        second = await wikipedia_bio.resolve_served_bio(
            sync_pick, "A Discogs bio.", details, pg_source
        )
        assert second.source is wikipedia_bio.BioSource.WIKIPEDIA
        assert second.bio == "Low are a band."
        assert second.wiki_url == "https://en.wikipedia.org/wiki/Low_(band)"
