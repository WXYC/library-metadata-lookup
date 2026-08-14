"""Unit tests for lookup/enrichment/wikipedia_bio.py — the Phase-B read-path
Wikipedia-preferred-bio resolver (docs/plans/lml-1192-wikipedia-artist-bio.md;
LML#513/#1192).

``resolve_served_bio`` returns the ``(served_bio, served_wiki_url)`` pair.
``served_wiki_url`` is ALWAYS ``pick.url`` (or ``None`` when there's no pick)
regardless of the flag/floor/cache outcome — Phase B only ever swaps which
TEXT accompanies that link, never the link itself, so the pair can never
disagree (the LML#504 split gate downstream nulls both together). Every
branch is table-driven per the plan's byte-identical-when-off requirement.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from wxyc_fastapi.observability import get_cache_stats, init_cache_stats

from discogs.models import ArtistDetails
from entity.cache_toolkit import CachedValue
from entity.sources import PgSource
from lookup.enrichment.wikipedia_bio import (
    BIO_PREFER_WIKIPEDIA_ENV_VAR,
    CACHE_HIT_STAT_KEY,
    CACHE_MISS_WARM_SCHEDULED_STAT_KEY,
    CACHE_NEGATIVE_STAT_KEY,
    FALLBACK_DISCOGS_STAT_KEY,
    SERVED_STAT_KEY,
    _bio_prefer_wikipedia_enabled,
    resolve_served_bio,
)
from lookup.wikipedia_url import PickedWikiUrl

_PICK = PickedWikiUrl(
    url="https://en.wikipedia.org/wiki/Stereolab", lang="en", slug_score=97.0, below_floor=False
)
_BELOW_FLOOR_PICK = PickedWikiUrl(
    url="https://en.wikipedia.org/wiki/Some_Other_Page",
    lang="en",
    slug_score=40.0,
    below_floor=True,
)
_DETAILS = ArtistDetails(artist_id=99, name="Stereolab")
_DISCOGS_BIO = "Stereolab are an Anglo-French band."


@pytest.fixture(autouse=True)
def _cache_stats():
    init_cache_stats()
    yield


@pytest.mark.asyncio
class TestFlagOffOrNoPick:
    """Byte-identical-when-off: every one of these degrades to the Discogs
    pair without ever touching the cache, regardless of what's cached."""

    async def test_flag_off_returns_discogs_pair(self, monkeypatch):
        monkeypatch.delenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, raising=False)
        pg = AsyncMock(spec=PgSource)
        bio, wiki = await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg, warm_cache=False)
        assert bio == _DISCOGS_BIO
        assert wiki == _PICK.url
        pg.fetchone.assert_not_awaited()

    async def test_no_pick_returns_discogs_pair_with_none_wiki(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        bio, wiki = await resolve_served_bio(None, _DISCOGS_BIO, _DETAILS, pg, warm_cache=False)
        assert bio == _DISCOGS_BIO
        assert wiki is None
        pg.fetchone.assert_not_awaited()

    async def test_below_floor_pick_returns_discogs_pair_but_still_the_link(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        bio, wiki = await resolve_served_bio(
            _BELOW_FLOOR_PICK, _DISCOGS_BIO, _DETAILS, pg, warm_cache=False
        )
        assert bio == _DISCOGS_BIO
        assert wiki == _BELOW_FLOOR_PICK.url
        pg.fetchone.assert_not_awaited()

    async def test_no_pg_handle_degrades_to_discogs_pair(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        bio, wiki = await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, None, warm_cache=False)
        assert bio == _DISCOGS_BIO
        assert wiki == _PICK.url

    async def test_no_top1_details_degrades_to_discogs_pair(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        bio, wiki = await resolve_served_bio(_PICK, _DISCOGS_BIO, None, pg, warm_cache=False)
        assert bio == _DISCOGS_BIO
        assert wiki == _PICK.url
        pg.fetchone.assert_not_awaited()

    async def test_records_fallback_discogs_not_served(self, monkeypatch):
        monkeypatch.delenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, raising=False)
        pg = AsyncMock(spec=PgSource)
        await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg, warm_cache=False)
        stats = get_cache_stats()
        assert stats.get(FALLBACK_DISCOGS_STAT_KEY) == 1
        assert stats.get(SERVED_STAT_KEY) is None


@pytest.mark.asyncio
class TestFlagOnAbovefloorCacheOutcomes:
    async def test_positive_cache_hit_serves_wikipedia_text(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_bio.get_cached_artist_wikipedia_bio",
            new_callable=AsyncMock,
            return_value=CachedValue(value="Stereolab are a French band.", was_present=True),
        ) as mock_get:
            bio, wiki = await resolve_served_bio(
                _PICK, _DISCOGS_BIO, _DETAILS, pg, warm_cache=False
            )
        assert bio == "Stereolab are a French band."
        assert wiki == _PICK.url
        mock_get.assert_awaited_once_with(
            pg, discogs_artist_id=_DETAILS.artist_id, wikipedia_url=_PICK.url
        )
        stats = get_cache_stats()
        assert stats.get(CACHE_HIT_STAT_KEY) == 1
        assert stats.get(SERVED_STAT_KEY) == 1

    async def test_negative_cache_hit_falls_back_to_discogs(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_bio.get_cached_artist_wikipedia_bio",
            new_callable=AsyncMock,
            return_value=CachedValue(value=None, was_present=True),
        ):
            bio, wiki = await resolve_served_bio(
                _PICK, _DISCOGS_BIO, _DETAILS, pg, warm_cache=False
            )
        assert bio == _DISCOGS_BIO
        assert wiki == _PICK.url
        stats = get_cache_stats()
        assert stats.get(CACHE_NEGATIVE_STAT_KEY) == 1
        assert stats.get(FALLBACK_DISCOGS_STAT_KEY) == 1

    async def test_miss_falls_back_to_discogs_and_schedules_warm_when_warm_cache_true(
        self, monkeypatch
    ):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        with (
            patch(
                "lookup.enrichment.wikipedia_bio.get_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
                return_value=CachedValue(value=None, was_present=False),
            ),
            patch(
                "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
                return_value=True,
            ) as mock_schedule,
        ):
            bio, wiki = await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg, warm_cache=True)
        assert bio == _DISCOGS_BIO
        assert wiki == _PICK.url
        mock_schedule.assert_called_once_with(
            discogs_artist_id=_DETAILS.artist_id, pick=_PICK, discogs_cache_pg=pg
        )
        stats = get_cache_stats()
        assert stats.get(CACHE_MISS_WARM_SCHEDULED_STAT_KEY) == 1
        assert stats.get(FALLBACK_DISCOGS_STAT_KEY) == 1

    async def test_miss_does_not_schedule_warm_when_warm_cache_false(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        with (
            patch(
                "lookup.enrichment.wikipedia_bio.get_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
                return_value=CachedValue(value=None, was_present=False),
            ),
            patch(
                "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
            ) as mock_schedule,
        ):
            bio, wiki = await resolve_served_bio(
                _PICK, _DISCOGS_BIO, _DETAILS, pg, warm_cache=False
            )
        assert bio == _DISCOGS_BIO
        assert wiki == _PICK.url
        mock_schedule.assert_not_called()
        stats = get_cache_stats()
        assert stats.get(CACHE_MISS_WARM_SCHEDULED_STAT_KEY) is None

    async def test_shed_warm_does_not_record_scheduled(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        with (
            patch(
                "lookup.enrichment.wikipedia_bio.get_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
                return_value=CachedValue(value=None, was_present=False),
            ),
            patch(
                "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
                return_value=False,
            ),
        ):
            await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg, warm_cache=True)
        stats = get_cache_stats()
        assert stats.get(CACHE_MISS_WARM_SCHEDULED_STAT_KEY) is None


@pytest.mark.asyncio
class TestUrlMismatchSelfHealing:
    async def test_cache_read_is_keyed_on_the_current_pick_url(self, monkeypatch):
        # entity/artist_wikipedia_bio.py's own SQL predicate does the
        # self-healing; this pins that resolve_served_bio always passes the
        # CURRENT pick's url through, never a stale/cached one.
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_bio.get_cached_artist_wikipedia_bio",
            new_callable=AsyncMock,
            return_value=CachedValue(value=None, was_present=False),
        ) as mock_get:
            await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg, warm_cache=False)
        assert mock_get.await_args.kwargs["wikipedia_url"] == _PICK.url


class TestBioPreferWikipediaEnabled:
    def test_default_off_when_unset(self, monkeypatch):
        monkeypatch.delenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, raising=False)
        assert _bio_prefer_wikipedia_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "True", "yes", "YES", "on", " on "])
    def test_true_flag_values_enable(self, monkeypatch, value):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, value)
        assert _bio_prefer_wikipedia_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "disabled", "garbage", ""])
    def test_everything_else_stays_off(self, monkeypatch, value):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, value)
        assert _bio_prefer_wikipedia_enabled() is False
