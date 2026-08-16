"""Unit tests for lookup/enrichment/wikipedia_bio.py — the Phase-B read-path
Wikipedia-preferred-bio resolver (docs/plans/lml-1192-wikipedia-artist-bio.md;
LML#513/#1192).

``resolve_served_bio`` returns a :class:`ServedBioResolution` carrying the
``(bio, wiki_url)`` pair to thread into ``item.enrich_one``, plus enough
bookkeeping (``source``, ``miss_details``, ``discogs_artist_id``) for the
COORDINATOR to finish the job after the LML#504 split gate has run.
``wiki_url`` is ``pick.url`` (or ``None`` when there's no pick) on every
branch EXCEPT a positive cache hit (LML#1192 review round 5) — see
``TestValidatedWarmRoundTrip`` below and ``entity/artist_wikipedia_bio.py``'s
module docstring for why the row, not the caller's sync pick, is now
authoritative there.

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
from entity.artist_wikipedia_bio import WikipediaBioHit
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
_DETAILS = ArtistDetails(
    artist_id=99, name="Stereolab", urls=["https://en.wikipedia.org/wiki/Stereolab"]
)
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
        assert resolution.miss_details is None
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
    async def test_positive_cache_hit_serves_wikipedia_text_and_url(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        hit = WikipediaBioHit(wikipedia_url=_PICK.url, extract="Stereolab are a French band.")
        with patch(
            "lookup.enrichment.wikipedia_bio.get_cached_artist_wikipedia_bio",
            new_callable=AsyncMock,
            return_value=CachedValue(value=hit, was_present=True),
        ) as mock_get:
            resolution = await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg)
        assert resolution.bio == "Stereolab are a French band."
        assert resolution.wiki_url == _PICK.url
        assert resolution.source is BioSource.WIKIPEDIA
        # LML#1192 review round 5: no wikipedia_url kwarg -- the read is
        # keyed on discogs_artist_id alone.
        mock_get.assert_awaited_once_with(pg, discogs_artist_id=_DETAILS.artist_id)
        # CACHE_HIT is a fact about the cache lookup itself, true regardless
        # of what the LML#504 gate later does -- recorded immediately.
        stats = get_cache_stats()
        assert stats.get(CACHE_HIT_STAT_KEY) == 1
        assert stats.get(SERVED_STAT_KEY) is None  # adoption telemetry deferred

    async def test_negative_cache_hit_falls_back_to_discogs(self, monkeypatch):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        hit = WikipediaBioHit(wikipedia_url=_PICK.url, extract=None)
        with patch(
            "lookup.enrichment.wikipedia_bio.get_cached_artist_wikipedia_bio",
            new_callable=AsyncMock,
            return_value=CachedValue(value=hit, was_present=True),
        ):
            resolution = await resolve_served_bio(_PICK, _DISCOGS_BIO, _DETAILS, pg)
        assert resolution.bio == _DISCOGS_BIO
        assert resolution.wiki_url == _PICK.url
        assert resolution.source is BioSource.DISCOGS
        stats = get_cache_stats()
        assert stats.get(CACHE_NEGATIVE_STAT_KEY) == 1

    async def test_miss_falls_back_to_discogs_and_carries_the_artist_details(self, monkeypatch):
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
        # what a later, post-gate scheduling call needs. LML#1192 review
        # round 5: top1_details itself (name + urls), not the sync pick --
        # the warm now runs its OWN fetch-validated selection rather than
        # reusing the sync pick's unvalidated one.
        assert resolution.miss_details is _DETAILS
        assert resolution.discogs_artist_id == _DETAILS.artist_id
        stats = get_cache_stats()
        assert stats.get(CACHE_MISS_WARM_SCHEDULED_STAT_KEY) is None


@pytest.mark.asyncio
class TestValidatedWarmRoundTrip:
    """LML#1192 review round 5: the coordinator's own red test. Through
    round 4, the cache read was keyed on ``wikipedia_url = pick.url`` (the
    caller's current SYNC pick) -- but a fetch-validated warm (round 4,
    P0-2) can legitimately write a row under a DIFFERENT, better url. The
    canonical case: the sync picker (``pick_artist_wikipedia_url``, no live
    fetch) names Low's bare disambiguation-page url; the validated warm
    determines the real article lives at the qualified ``_(band)`` url and
    writes there. Reproduced live against a real Postgres round-trip before
    the entity-layer fix (LML#1192 review round 5's #1194 commit): writing
    under the qualified url and reading back with the unchanged sync pick
    returned ``was_present=False`` forever -- every request would
    re-trigger a warm indefinitely, reintroducing exactly the live
    Wikipedia dependency on the request path the plan's Non-goals rule out.

    The row is now authoritative: a hit serves ITS OWN stored
    ``wikipedia_url``, regardless of what the caller's sync pick names.
    """

    async def test_a_row_the_warm_wrote_under_a_different_url_than_the_sync_pick_is_a_hit(
        self, monkeypatch
    ):
        monkeypatch.setenv(BIO_PREFER_WIKIPEDIA_ENV_VAR, "true")
        pg = AsyncMock(spec=PgSource)
        # The sync pick (unvalidated, no live fetch) names the bare,
        # disambiguation-page url -- exactly what pick_artist_wikipedia_url
        # would return for "Low" with no way to know it's the wrong page.
        sync_pick = PickedWikiUrl(
            url="https://en.wikipedia.org/wiki/Low", lang="en", slug_score=75.0, below_floor=False
        )
        # The validated warm determined the REAL article is the qualified
        # url and wrote the row there instead.
        validated_hit = WikipediaBioHit(
            wikipedia_url="https://en.wikipedia.org/wiki/Low_(band)", extract="Low are a band."
        )
        with patch(
            "lookup.enrichment.wikipedia_bio.get_cached_artist_wikipedia_bio",
            new_callable=AsyncMock,
            return_value=CachedValue(value=validated_hit, was_present=True),
        ):
            resolution = await resolve_served_bio(sync_pick, _DISCOGS_BIO, _DETAILS, pg)
        # A HIT, not a miss -- serving the ROW's url/content, not the sync
        # pick's.
        assert resolution.source is BioSource.WIKIPEDIA
        assert resolution.bio == "Low are a band."
        assert resolution.wiki_url == "https://en.wikipedia.org/wiki/Low_(band)"


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
            miss_details=None,
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
            miss_details=None,
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
            miss_details=None,
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
            miss_details=_DETAILS,
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
            discogs_artist_id=_DETAILS.artist_id,
            artist_name=_DETAILS.name,
            urls=_DETAILS.urls,
            discogs_cache_pg=pg,
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

    async def test_skips_when_no_miss_details(self, monkeypatch):
        # A hit or negative resolution carries miss_details=None -- nothing to warm.
        pg = AsyncMock(spec=PgSource)
        resolution = ServedBioResolution(
            bio=_DISCOGS_BIO,
            wiki_url=_PICK.url,
            source=BioSource.DISCOGS,
            miss_details=None,
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
            miss_details=None,
            discogs_artist_id=None,
        )

    def _miss_resolution(self):
        return ServedBioResolution(
            bio=_DISCOGS_BIO,
            wiki_url=_PICK.url,
            source=BioSource.DISCOGS,
            miss_details=_DETAILS,
            discogs_artist_id=_DETAILS.artist_id,
        )

    def test_surfaced_wikipedia_bio_records_served_and_returns_true(self):
        init_cache_stats()
        resolution = self._wikipedia_resolution()
        surfaced = finalize_bio(
            resolution,
            enriched_top1_bio="Wikipedia prose.",
            enriched_top1_wiki=_PICK.url,
            warm_cache=False,
            discogs_cache_pg=None,
        )
        assert surfaced is True
        assert get_cache_stats().get(SERVED_STAT_KEY) == 1

    def test_gate_nulled_bio_records_nothing_and_returns_false(self):
        # The LML#504 split gate nulled BOTH bio and wiki_url out together
        # -- enriched_top1_bio/enriched_top1_wiki are both None even though
        # the resolution itself picked Wikipedia text.
        init_cache_stats()
        resolution = self._wikipedia_resolution()
        surfaced = finalize_bio(
            resolution,
            enriched_top1_bio=None,
            enriched_top1_wiki=None,
            warm_cache=False,
            discogs_cache_pg=None,
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
                enriched_top1_wiki=_PICK.url,
                warm_cache=True,
                discogs_cache_pg=pg,
            )
        assert surfaced is True
        mock_schedule.assert_called_once_with(
            discogs_artist_id=_DETAILS.artist_id,
            artist_name=_DETAILS.name,
            urls=_DETAILS.urls,
            discogs_cache_pg=pg,
        )
        assert get_cache_stats().get(CACHE_MISS_WARM_SCHEDULED_STAT_KEY) == 1

    def test_gate_nulled_miss_does_not_schedule_a_warm(self):
        # The LML#504 gate blocked this artist entirely -- both bio and
        # wiki_url come back None, so there's nothing worth warming.
        pg = AsyncMock(spec=PgSource)
        with patch(
            "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
        ) as mock_schedule:
            surfaced = finalize_bio(
                self._miss_resolution(),
                enriched_top1_bio=None,
                enriched_top1_wiki=None,
                warm_cache=True,
                discogs_cache_pg=pg,
            )
        assert surfaced is False
        mock_schedule.assert_not_called()

    def test_no_discogs_profile_still_schedules_the_warm(self):
        # LML#1192 review round 4, P0-6: an artist with NO Discogs profile
        # at all has top1_bio=None even on a genuine cache miss, so
        # enriched_top1_bio is unconditionally None/falsy for this cohort
        # -- exactly the artists this warm exists for (Discogs has nothing
        # to offer; Wikipedia might). The warm must NOT gate on
        # bio_surfaced: it gates on whether the LML#504 identity gate let
        # the artist's IDENTITY through at all, which wikipedia_url
        # (nulled by that same gate, independently of bio text) answers
        # correctly. Here the gate passed (wiki_url survived) even though
        # there was never any bio text to surface.
        pg = AsyncMock(spec=PgSource)
        init_cache_stats()
        resolution = self._miss_resolution()
        with patch(
            "lookup.enrichment.wikipedia_bio.wikipedia_warm.schedule_wikipedia_bio_warm",
            return_value=True,
        ) as mock_schedule:
            surfaced = finalize_bio(
                resolution,
                enriched_top1_bio=None,  # no Discogs profile -- nothing to surface
                enriched_top1_wiki=_PICK.url,  # but the gate let the identity through
                warm_cache=True,
                discogs_cache_pg=pg,
            )
        assert surfaced is False  # correctly reports nothing was SHOWN
        mock_schedule.assert_called_once_with(
            discogs_artist_id=_DETAILS.artist_id,
            artist_name=_DETAILS.name,
            urls=_DETAILS.urls,
            discogs_cache_pg=pg,
        )
        assert get_cache_stats().get(CACHE_MISS_WARM_SCHEDULED_STAT_KEY) == 1
