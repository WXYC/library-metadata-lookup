"""Unit tests for lookup/enrichment/wikipedia_bio.py — the Phase-B read-path
Wikipedia-preferred-bio resolver (docs/plans/lml-1192-wikipedia-artist-bio.md;
LML#513/#1192).

``resolve_served_bio`` returns a :class:`ServedBioResolution` carrying the
``(bio, wiki_url)`` pair to thread into ``item.enrich_one``, plus enough
bookkeeping (``source``, ``miss_pick``, ``discogs_artist_id``) for the
COORDINATOR to finish the job after the LML#504 split gate has run.
``wiki_url`` is ALWAYS ``pick.url`` (or ``None`` when there's no pick)
regardless of the flag/floor/cache outcome — Phase B only ever swaps which
TEXT accompanies that link, never the link itself.

LML#1192 review (round 2), B2-1/B2-2: ``resolve_served_bio`` runs BEFORE
``item.enrich_one``, so it cannot know whether the LML#504 split gate will
null the bio out — adoption telemetry and miss-warm scheduling are
therefore a SEPARATE, POST-HOC call (:func:`finalize_bio`, LML#1192 review
round 3, finding 10 — collapses the pair of coordinator calls this module
used to expose publicly, ``record_bio_adoption``/``maybe_schedule_wikipedia_bio_warm``,
now private helpers) the coordinator makes after ``item.enrich_one`` has
run, passing in the ENRICHED top-1 ``artist_bio`` (whether the bio it
decided on actually reached the wire is then a plain truthiness check, not
a separately-computed boolean — see that module's docstring).
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
    BioSource,
    ServedBioResolution,
    _bio_prefer_wikipedia_enabled,
    _maybe_schedule_wikipedia_bio_warm,
    _record_bio_adoption,
    finalize_bio,
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
        resolution = await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg)
        assert resolution.bio == _DISCOGS_BIO
        assert resolution.wiki_url == _PICK.url
        assert resolution.source is BioSource.DISCOGS
        assert resolution.miss_pick is None
        pg.fetchone.assert_not_awaited()

    async def test_no_pick_returns_discogs_pair_with_none_wiki(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        resolution = await resolve_served_bio(None, _DISCOGS_BIO, _DETAILS, pg)
        assert resolution.bio == _DISCOGS_BIO
        assert resolution.wiki_url is None
        pg.fetchone.assert_not_awaited()

    async def test_below_floor_pick_returns_discogs_pair_but_still_the_link(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        resolution = await resolve_served_bio(_BELOW_FLOOR_PICK, _DISCOGS_BIO, _DETAILS, pg)
        assert resolution.bio == _DISCOGS_BIO
        assert resolution.wiki_url == _BELOW_FLOOR_PICK.url
        pg.fetchone.assert_not_awaited()

    async def test_no_pg_handle_degrades_to_discogs_pair(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        resolution = await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, None)
        assert resolution.bio == _DISCOGS_BIO
        assert resolution.wiki_url == _PICK.url

    async def test_no_top1_details_degrades_to_discogs_pair(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        resolution = await resolve_served_bio(_PICK, _DISCOGS_BIO, None, pg)
        assert resolution.bio == _DISCOGS_BIO
        assert resolution.wiki_url == _PICK.url
        pg.fetchone.assert_not_awaited()

    async def test_does_not_record_adoption_telemetry_itself(self, monkeypatch):
        # B2-1: resolve_served_bio no longer records served/fallback_discogs
        # -- that's the coordinator's job, post-gate.
        monkeypatch.delenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, raising=False)
        pg = AsyncMock(spec=PgSource)
        await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg)
        stats = get_cache_stats()
        assert stats.get(FALLBACK_DISCOGS_STAT_KEY) is None
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
            resolution = await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg)
        assert resolution.bio == "Stereolab are a French band."
        assert resolution.wiki_url == _PICK.url
        assert resolution.source is BioSource.WIKIPEDIA
        mock_get.assert_awaited_once_with(
            pg, discogs_artist_id=_DETAILS.artist_id, wikipedia_url=_PICK.url
        )
        # CACHE_HIT is a fact about the cache lookup itself, true regardless
        # of what the LML#504 gate later does -- recorded immediately.
        stats = get_cache_stats()
        assert stats.get(CACHE_HIT_STAT_KEY) == 1
        assert stats.get(SERVED_STAT_KEY) is None  # adoption telemetry deferred

    async def test_negative_cache_hit_falls_back_to_discogs(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_bio.get_cached_artist_wikipedia_bio",
            new_callable=AsyncMock,
            return_value=CachedValue(value=None, was_present=True),
        ):
            resolution = await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg)
        assert resolution.bio == _DISCOGS_BIO
        assert resolution.wiki_url == _PICK.url
        assert resolution.source is BioSource.DISCOGS
        stats = get_cache_stats()
        assert stats.get(CACHE_NEGATIVE_STAT_KEY) == 1

    async def test_miss_falls_back_to_discogs_and_carries_the_miss_pick(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_bio.get_cached_artist_wikipedia_bio",
            new_callable=AsyncMock,
            return_value=CachedValue(value=None, was_present=False),
        ):
            resolution = await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg)
        assert resolution.bio == _DISCOGS_BIO
        assert resolution.wiki_url == _PICK.url
        assert resolution.source is BioSource.DISCOGS
        # A miss does NOT schedule anything itself (B2-2) -- it hands back
        # what a later, post-gate scheduling call needs.
        assert resolution.miss_pick == _PICK
        assert resolution.discogs_artist_id == _DETAILS.artist_id
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
            await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg)
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


# ---------------------------------------------------------------------------
# record_bio_adoption — post-hoc, gated on what actually reached the wire
# (LML#1192 review, B2-1)
# ---------------------------------------------------------------------------


class TestRecordBioAdoption:
    def test_wikipedia_source_surfaced_records_served(self):
        resolution = ServedBioResolution(
            bio="Wikipedia prose.",
            wiki_url=_PICK.url,
            source=BioSource.WIKIPEDIA,
            miss_pick=None,
            discogs_artist_id=None,
        )
        init_cache_stats()
        _record_bio_adoption(resolution, bio_surfaced=True)
        stats = get_cache_stats()
        assert stats.get(SERVED_STAT_KEY) == 1
        assert stats.get(FALLBACK_DISCOGS_STAT_KEY) is None

    def test_discogs_source_surfaced_records_fallback(self):
        resolution = ServedBioResolution(
            bio=_DISCOGS_BIO,
            wiki_url=_PICK.url,
            source=BioSource.DISCOGS,
            miss_pick=None,
            discogs_artist_id=None,
        )
        init_cache_stats()
        _record_bio_adoption(resolution, bio_surfaced=True)
        stats = get_cache_stats()
        assert stats.get(FALLBACK_DISCOGS_STAT_KEY) == 1
        assert stats.get(SERVED_STAT_KEY) is None

    def test_not_surfaced_records_neither(self):
        # The LML#504 split gate nulled the bio out -- neither counter
        # should fire, regardless of which source resolve_served_bio picked.
        resolution = ServedBioResolution(
            bio="Wikipedia prose.",
            wiki_url=_PICK.url,
            source=BioSource.WIKIPEDIA,
            miss_pick=None,
            discogs_artist_id=None,
        )
        init_cache_stats()
        _record_bio_adoption(resolution, bio_surfaced=False)
        stats = get_cache_stats()
        assert stats.get(SERVED_STAT_KEY) is None
        assert stats.get(FALLBACK_DISCOGS_STAT_KEY) is None


# ---------------------------------------------------------------------------
# maybe_schedule_wikipedia_bio_warm — post-hoc, symmetric with
# background.maybe_schedule_discogs_bio_warm (LML#1192 review, B2-2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMaybeScheduleWikipediaBioWarm:
    def _miss_resolution(self):
        return ServedBioResolution(
            bio=_DISCOGS_BIO,
            wiki_url=_PICK.url,
            source=BioSource.DISCOGS,
            miss_pick=_PICK,
            discogs_artist_id=_DETAILS.artist_id,
        )

    async def test_schedules_when_warm_cache_and_surfaced(self, monkeypatch):
        pg = AsyncMock(spec=PgSource)
        init_cache_stats()
        with patch(
            "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
            return_value=True,
        ) as mock_schedule:
            _maybe_schedule_wikipedia_bio_warm(
                self._miss_resolution(), warm_cache=True, bio_surfaced=True, discogs_cache_pg=pg
            )
        mock_schedule.assert_called_once_with(
            discogs_artist_id=_DETAILS.artist_id, pick=_PICK, discogs_cache_pg=pg
        )
        assert get_cache_stats().get(CACHE_MISS_WARM_SCHEDULED_STAT_KEY) == 1

    async def test_skips_when_warm_cache_false(self, monkeypatch):
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
        ) as mock_schedule:
            _maybe_schedule_wikipedia_bio_warm(
                self._miss_resolution(), warm_cache=False, bio_surfaced=True, discogs_cache_pg=pg
            )
        mock_schedule.assert_not_called()

    async def test_skips_when_not_surfaced(self, monkeypatch):
        # B2-2: symmetric with the Discogs ref-warm -- don't warm a bio the
        # response didn't surface (LML#504 gate suppressed it).
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
        ) as mock_schedule:
            _maybe_schedule_wikipedia_bio_warm(
                self._miss_resolution(), warm_cache=True, bio_surfaced=False, discogs_cache_pg=pg
            )
        mock_schedule.assert_not_called()

    async def test_skips_when_no_miss_pick(self, monkeypatch):
        # A hit or negative resolution carries miss_pick=None -- nothing to warm.
        pg = AsyncMock(spec=PgSource)
        resolution = ServedBioResolution(
            bio=_DISCOGS_BIO,
            wiki_url=_PICK.url,
            source=BioSource.DISCOGS,
            miss_pick=None,
            discogs_artist_id=None,
        )
        with patch(
            "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
        ) as mock_schedule:
            _maybe_schedule_wikipedia_bio_warm(
                resolution, warm_cache=True, bio_surfaced=True, discogs_cache_pg=pg
            )
        mock_schedule.assert_not_called()

    async def test_skips_when_no_pg_handle(self, monkeypatch):
        with patch(
            "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
        ) as mock_schedule:
            _maybe_schedule_wikipedia_bio_warm(
                self._miss_resolution(), warm_cache=True, bio_surfaced=True, discogs_cache_pg=None
            )
        mock_schedule.assert_not_called()

    async def test_shed_warm_does_not_record_scheduled(self, monkeypatch):
        pg = AsyncMock(spec=PgSource)
        init_cache_stats()
        with patch(
            "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
            return_value=False,
        ):
            _maybe_schedule_wikipedia_bio_warm(
                self._miss_resolution(), warm_cache=True, bio_surfaced=True, discogs_cache_pg=pg
            )
        assert get_cache_stats().get(CACHE_MISS_WARM_SCHEDULED_STAT_KEY) is None


# ---------------------------------------------------------------------------
# finalize_bio -- the single public post-item.enrich_one entry point
# (LML#1192 review round 3, finding 10). Replaces the coordinator's own
# top1_bio_surfaced/resolution_bio_surfaced pair and the top1_bio_is_discogs
# string comparison -- bio_surfaced is now a plain bool() on the ENRICHED
# artist_bio the coordinator passes in, and "was it Discogs" is answered by
# ServedBioResolution.source directly.
# ---------------------------------------------------------------------------


class TestFinalizeBio:
    def _wikipedia_resolution(self):
        return ServedBioResolution(
            bio="Wikipedia prose.",
            wiki_url=_PICK.url,
            source=BioSource.WIKIPEDIA,
            miss_pick=None,
            discogs_artist_id=None,
        )

    def _miss_resolution(self):
        return ServedBioResolution(
            bio=_DISCOGS_BIO,
            wiki_url=_PICK.url,
            source=BioSource.DISCOGS,
            miss_pick=_PICK,
            discogs_artist_id=_DETAILS.artist_id,
        )

    def test_surfaced_wikipedia_bio_records_served_and_returns_true(self):
        init_cache_stats()
        resolution = self._wikipedia_resolution()
        surfaced = finalize_bio(
            resolution,
            enriched_top1_bio="Wikipedia prose.",
            warm_cache=False,
            discogs_cache_pg=None,
        )
        assert surfaced is True
        assert get_cache_stats().get(SERVED_STAT_KEY) == 1

    def test_gate_nulled_bio_records_nothing_and_returns_false(self):
        # The LML#504 split gate nulled the bio out -- enriched_top1_bio is
        # None even though the resolution itself picked Wikipedia text.
        init_cache_stats()
        resolution = self._wikipedia_resolution()
        surfaced = finalize_bio(
            resolution, enriched_top1_bio=None, warm_cache=False, discogs_cache_pg=None
        )
        assert surfaced is False
        assert get_cache_stats().get(SERVED_STAT_KEY) is None
        assert get_cache_stats().get(FALLBACK_DISCOGS_STAT_KEY) is None

    def test_surfaced_bio_schedules_a_pending_miss_warm(self):
        pg = AsyncMock(spec=PgSource)
        init_cache_stats()
        resolution = self._miss_resolution()
        with patch(
            "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
            return_value=True,
        ) as mock_schedule:
            surfaced = finalize_bio(
                resolution,
                enriched_top1_bio=_DISCOGS_BIO,
                warm_cache=True,
                discogs_cache_pg=pg,
            )
        assert surfaced is True
        mock_schedule.assert_called_once_with(
            discogs_artist_id=_DETAILS.artist_id, pick=_PICK, discogs_cache_pg=pg
        )
        assert get_cache_stats().get(CACHE_MISS_WARM_SCHEDULED_STAT_KEY) == 1

    def test_unsurfaced_miss_does_not_schedule_a_warm(self):
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
        ) as mock_schedule:
            surfaced = finalize_bio(
                self._miss_resolution(),
                enriched_top1_bio=None,
                warm_cache=True,
                discogs_cache_pg=pg,
            )
        assert surfaced is False
        mock_schedule.assert_not_called()
