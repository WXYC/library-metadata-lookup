"""LML#1318 — a failed track resolution degrades to the typed (artist, album) album-level match.

On a song-bearing library-miss lookup, a completed-but-failed track resolution
used to zero the whole response, even when the album-level path resolves the
same typed ``(artist, album)`` pair from the local release cache. The fix:
``_resolve_nonlibrary_release`` degrades to the LML#1318 album-level fallback
(``lookup/album_level_match.py`` — the ARTIST_PLUS_ALBUM match class, local
cache only) when the track leg comes up empty, and ``TRACK_ON_COMPILATION``
surfaces that degrade honestly (``song_not_found`` stays True; no compilation
claim) so Backend-Service's track-context trust gate can see the track was
never confirmed.

Three parameterized repro shapes from the 2026-09-18 metadata-no-match digest
(WXYC/Backend-Service#1912):

* album-title-typed-as-track (Eliana Glass — "E at Home" · *E at Home*)
* one-space track spelling variant (Agriculture — "Micah (5:15 AM)" vs
  Discogs "Micah (5:15am)" · *The Spiritual Sound*)
* candidate crowd-out on a short reused track name (Jill Scott — "Pressha" ·
  *To Whom it May Concern*, cache-titled "To Whom This May Concern")

Plus the LML#632 pin contract (a track-key NULL pin must not suppress the
album answer; the album fallback writes only the ``is_track=False`` channel)
and the library-lane parity guard (a shelved album that answers the typed
pair keeps its row — no row-less duplicate).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from core.search import SearchState
from discogs.models import (
    DiscogsSearchResponse,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackReleasesResponse,
)
from entity.release_resolution_cache import DEFAULT_MISS_TTL
from lookup.album_level_match import resolve_typed_album_level_match
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from lookup.release_resolution import ResolvedRelease
from lookup.rowless import ROWLESS_LIBRARY_ID, _resolve_nonlibrary_release
from lookup.strategies.track_on_compilation import _unconfirmed_album_outcome
from services.parser import ParsedRequest
from tests.conftest import make_lml_telemetry
from tests.factories import make_library_item
from tests.unit.test_nonlibrary_release_resolution import _RecordingPg

# ---------------------------------------------------------------------------
# Shared fixture data — the three digest rows (real 2026-09-18 plays).
# ---------------------------------------------------------------------------

AGRICULTURE_RELEASE_ID = 35246362
JILL_SCOTT_RELEASE_ID = 36505024
ELIANA_GLASS_RELEASE_ID = 37161147


@pytest.fixture
def enable_nonlibrary_release(monkeypatch):
    """Turn on LML_RESOLVE_NONLIBRARY_RELEASE so the row-less producers fire."""
    monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _cache_row(release_id: int, title: str, artist: str) -> dict:
    """One ``DiscogsCacheService.search_releases`` result row (the PG arm's shape)."""
    return {
        "release_id": release_id,
        "title": title,
        "artist_name": artist,
        "artist_credits": [artist],
        "artwork_url": f"https://i.discogs.com/{release_id}.jpg",
    }


def _release_info(release_id: int, album: str, artist: str) -> ReleaseInfo:
    return ReleaseInfo(
        album=album,
        artist=artist,
        release_id=release_id,
        release_url=f"https://www.discogs.com/release/{release_id}",
        is_compilation=False,
    )


def _track_response(song: str, artist: str, releases: list[ReleaseInfo]) -> TrackReleasesResponse:
    return TrackReleasesResponse(
        track=song, artist=artist, releases=releases, total=len(releases), cached=False
    )


def _build_discogs_service(
    *,
    artist: str,
    album_cache_rows: list[dict],
    track_candidates: list[ReleaseInfo],
    song: str,
    rehydrate: ReleaseMetadataResponse | None = None,
) -> AsyncMock:
    """A Discogs service whose track leg fails validation while the LOCAL cache
    resolves the typed album. ``discogs_service.search`` (the live album-search
    seam) returns empty on purpose — the fallback must be served by the local
    release cache, never a new live probe."""
    svc = AsyncMock()
    svc.cache_service = AsyncMock()
    svc.cache_service.search_releases = AsyncMock(return_value=album_cache_rows)
    # Step 3b's A4 net probes this on the compilation-tier path; a bare
    # AsyncMock would return a truthy Mock and derail the cascade.
    svc.cache_service.search_releases_by_track = AsyncMock(return_value=[])
    # The album-channel pin rehydrate (review fix 3) reads this lean local
    # hydration; None = "row not in the local cache", falling to the probe.
    svc.cache_service.get_release_lean = AsyncMock(return_value=None)
    svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
    svc.search_releases_by_track = AsyncMock(
        return_value=_track_response(song, artist, track_candidates)
    )
    svc.search_releases_by_album_title = AsyncMock(
        return_value=_track_response("", "", track_candidates)
    )
    # The heart of all three repro shapes: every per-track validation fails.
    svc.validate_track_on_release = AsyncMock(return_value=False)
    svc.get_track_credit_on_release = AsyncMock(return_value=None)
    svc.get_release_artist_variations = AsyncMock(return_value=[])
    svc.get_release = AsyncMock(return_value=rehydrate)
    return svc


def _build_library_db(shelf_rows_by_artist_query: dict[str, list] | None = None) -> AsyncMock:
    db = AsyncMock()
    rows_map = shelf_rows_by_artist_query or {}

    async def _search(query: str, limit: int = 10):
        return list(rows_map.get(query, []))

    db.search = AsyncMock(side_effect=_search)
    db.exact_title = AsyncMock(return_value=[])
    db.find_similar_artist = AsyncMock(return_value=None)
    db.connect = AsyncMock()
    db.close = AsyncMock()
    db.is_available = AsyncMock(return_value=True)
    db._conn = Mock()
    return db


# ---------------------------------------------------------------------------
# Kernel tier: _resolve_nonlibrary_release degrades to the album-level match.
# ---------------------------------------------------------------------------


class TestKernelAlbumLevelDegrade:
    """``_resolve_nonlibrary_release``'s LML#1318 degrade contract."""

    ARTIST = "Agriculture"
    ALBUM = "The Spiritual Sound"
    SONG = "Micah (5:15 AM)"

    def _service(self, **overrides) -> AsyncMock:
        kwargs: dict = {
            "artist": self.ARTIST,
            "album_cache_rows": [_cache_row(AGRICULTURE_RELEASE_ID, self.ALBUM, self.ARTIST)],
            "track_candidates": [_release_info(AGRICULTURE_RELEASE_ID, self.ALBUM, self.ARTIST)],
            "song": self.SONG,
        }
        kwargs.update(overrides)
        return _build_discogs_service(**kwargs)

    @pytest.mark.asyncio
    async def test_track_miss_degrades_to_typed_album_match(self):
        svc = self._service()
        resolved = await _resolve_nonlibrary_release(
            svc, None, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        assert resolved is not None
        assert resolved.release_id == AGRICULTURE_RELEASE_ID
        assert resolved.album_title == self.ALBUM
        assert resolved.track_confirmed is False

    @pytest.mark.asyncio
    async def test_degrade_is_cache_only_never_a_live_album_search(self):
        svc = self._service()
        await _resolve_nonlibrary_release(
            svc, None, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        svc.search.assert_not_called()
        # Review fix 3: nor the get_release read-through, whose fallthrough
        # seam includes a LIVE API leg — the degrade never leaves the local
        # cache on ANY of its branches.
        svc.get_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_album_typed_still_returns_none(self):
        svc = self._service()
        resolved = await _resolve_nonlibrary_release(
            svc, None, song=self.SONG, artist=self.ARTIST, album=None
        )
        assert resolved is None
        svc.cache_service.search_releases.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirmed_track_resolution_bypasses_the_degrade(self):
        svc = self._service()
        svc.validate_track_on_release = AsyncMock(return_value=True)
        resolved = await _resolve_nonlibrary_release(
            svc, None, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        assert resolved is not None
        assert resolved.track_confirmed is True
        svc.cache_service.search_releases.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_track_null_pin_does_not_suppress_the_album_answer(self):
        """Acceptance criterion 3: the durable NULL pin stays track-key-scoped.

        A fresh known miss on the track key still short-circuits the live
        track probe — but the lookup now degrades to the album answer instead
        of zeroing."""
        pg = _RecordingPg()
        pg.seed(("agriculture", "micah (5:15 am)", True), None)
        svc = self._service()
        resolved = await _resolve_nonlibrary_release(
            svc, pg, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        svc.search_releases_by_track.assert_not_called()
        assert resolved is not None
        assert resolved.release_id == AGRICULTURE_RELEASE_ID
        assert resolved.track_confirmed is False

    @pytest.mark.asyncio
    async def test_degrade_writes_the_album_channel_and_leaves_the_track_pin(self):
        pg = _RecordingPg()
        svc = self._service()
        resolved = await _resolve_nonlibrary_release(
            svc, pg, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        assert resolved is not None
        # The track key records the miss (track resolution genuinely failed)...
        track_row = pg.store[("agriculture", "micah (5:15 am)", True)]
        assert track_row["release_id"] is None
        # ...and the album answer is amortized on its own is_track=False channel.
        album_row = pg.store[("agriculture", "the spiritual sound", False)]
        assert album_row["release_id"] == AGRICULTURE_RELEASE_ID

    @pytest.mark.asyncio
    async def test_album_channel_positive_short_circuits_the_cache_probe(self):
        """Review fix 3: the pin-hit rehydrate reads the LOCAL cache's lean
        by-id hydration, never ``DiscogsService.get_release`` (whose
        fallthrough seam includes a live API leg + write-back) — the exact
        shape where a pin outlives its pruned PG row must stay cache-only."""
        pg = _RecordingPg()
        pg.seed(("agriculture", "the spiritual sound", False), AGRICULTURE_RELEASE_ID)
        svc = self._service()
        svc.cache_service.get_release_lean = AsyncMock(
            return_value=ReleaseMetadataResponse(
                release_id=AGRICULTURE_RELEASE_ID,
                title=self.ALBUM,
                artist=self.ARTIST,
                release_url=f"https://www.discogs.com/release/{AGRICULTURE_RELEASE_ID}",
            )
        )
        resolved = await _resolve_nonlibrary_release(
            svc, pg, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        assert resolved is not None
        assert resolved.release_id == AGRICULTURE_RELEASE_ID
        assert resolved.track_confirmed is False
        svc.cache_service.search_releases.assert_not_called()
        svc.get_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_pin_outliving_its_cache_row_falls_back_to_the_probe_without_api(self):
        """Review fix 3, the 9/4-rebuild shape: the album pin survives but the
        pruned cache row is gone. The lean rehydrate misses, the trgm probe
        answers, and the API leg is never entered."""
        pg = _RecordingPg()
        pg.seed(("agriculture", "the spiritual sound", False), AGRICULTURE_RELEASE_ID)
        svc = self._service()
        svc.cache_service.get_release_lean = AsyncMock(return_value=None)
        resolved = await _resolve_nonlibrary_release(
            svc, pg, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        assert resolved is not None
        assert resolved.release_id == AGRICULTURE_RELEASE_ID
        svc.cache_service.search_releases.assert_called()
        svc.get_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_album_cache_miss_pins_a_short_ttl_crowd_out_miss(self):
        """Review fix 4: a cache-only empty is not evidence Discogs lacks the
        pair (the daily ETL may cache it tomorrow), but leaving it unpinned
        made every repeat inside the 7-day track-miss window pay the pin read
        + a pg_trgm scan. Pin it with the 1-hour crowd-out TTL semantics —
        the same "self-heals within the hour" trade LML#824 codified."""
        pg = _RecordingPg()
        svc = self._service(album_cache_rows=[])
        resolved = await _resolve_nonlibrary_release(
            svc, pg, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        assert resolved is None
        album_row = pg.store[("agriculture", "the spiritual sound", False)]
        assert album_row["release_id"] is None
        assert album_row["crowd_out"] is True

    @pytest.mark.asyncio
    async def test_fresh_album_channel_miss_short_circuits_the_probe(self):
        pg = _RecordingPg()
        pg.seed(("agriculture", "the spiritual sound", False), None, crowd_out=True)
        svc = self._service()
        resolved = await _resolve_nonlibrary_release(
            svc, pg, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        assert resolved is None
        svc.cache_service.search_releases.assert_not_called()

    @pytest.mark.asyncio
    async def test_album_channel_miss_expires_on_the_crowd_out_ttl(self):
        from entity.release_resolution_cache import DEFAULT_CROWD_OUT_MISS_TTL

        pg = _RecordingPg()
        pg.seed(("agriculture", "the spiritual sound", False), None, crowd_out=True)
        pg.age(("agriculture", "the spiritual sound", False), DEFAULT_CROWD_OUT_MISS_TTL * 2)
        svc = self._service()
        resolved = await _resolve_nonlibrary_release(
            svc, pg, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        assert resolved is not None
        assert resolved.release_id == AGRICULTURE_RELEASE_ID
        svc.cache_service.search_releases.assert_called()

    @pytest.mark.asyncio
    async def test_unhydratable_positive_pin_is_not_demoted_to_a_miss(self):
        """Self-heal guard, mirroring the kernel's own: a positive album pin
        whose cache row is temporarily unreadable must not be overwritten by
        a miss when the probe also comes up empty."""
        pg = _RecordingPg()
        pg.seed(("agriculture", "the spiritual sound", False), AGRICULTURE_RELEASE_ID)
        svc = self._service(album_cache_rows=[])
        svc.cache_service.get_release_lean = AsyncMock(return_value=None)
        resolved = await _resolve_nonlibrary_release(
            svc, pg, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        assert resolved is None
        album_row = pg.store[("agriculture", "the spiritual sound", False)]
        assert album_row["release_id"] == AGRICULTURE_RELEASE_ID

    @pytest.mark.asyncio
    async def test_floor_rejects_an_alternative_same_artist_album(self):
        """Trust-gate constraint (the BS#1359 class): the fallback must be the
        TYPED pair, never a different same-artist album that happens to be
        cached."""
        svc = self._service(album_cache_rows=[_cache_row(11111111, "Living Is Easy", self.ARTIST)])
        resolved = await _resolve_nonlibrary_release(
            svc, None, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        assert resolved is None

    @pytest.mark.asyncio
    async def test_one_token_album_title_variant_clears_the_floor(self):
        """The Jill Scott shape: typed "To Whom it May Concern" vs the cache's
        "To Whom This May Concern" — the same one-token variant the album-only
        replay cleared in production."""
        svc = _build_discogs_service(
            artist="Jill Scott",
            album_cache_rows=[
                _cache_row(JILL_SCOTT_RELEASE_ID, "To Whom This May Concern", "Jill Scott")
            ],
            track_candidates=[],
            song="Pressha",
        )
        resolved = await _resolve_nonlibrary_release(
            svc, None, song="Pressha", artist="Jill Scott", album="To Whom it May Concern"
        )
        assert resolved is not None
        assert resolved.release_id == JILL_SCOTT_RELEASE_ID
        assert resolved.track_confirmed is False

    @pytest.mark.asyncio
    async def test_stale_track_miss_reprobes_then_degrades(self):
        """Past the miss TTL the track leg re-probes (and re-fails); the degrade
        still answers."""
        pg = _RecordingPg()
        pg.seed(("agriculture", "micah (5:15 am)", True), None)
        pg.age(("agriculture", "micah (5:15 am)", True), DEFAULT_MISS_TTL * 2)
        svc = self._service()
        resolved = await _resolve_nonlibrary_release(
            svc, pg, song=self.SONG, artist=self.ARTIST, album=self.ALBUM
        )
        svc.search_releases_by_track.assert_called()
        assert resolved is not None
        assert resolved.release_id == AGRICULTURE_RELEASE_ID


class TestAlbumLevelMatchHelper:
    """Direct contract of ``resolve_typed_album_level_match``."""

    @pytest.mark.asyncio
    async def test_no_cache_service_degrades_to_none_and_writes_nothing(self):
        svc = AsyncMock()
        svc.cache_service = None
        pg = _RecordingPg()
        resolved = await resolve_typed_album_level_match(
            svc, pg, artist="Agriculture", album="The Spiritual Sound"
        )
        assert resolved is None
        assert pg.store == {}

    @pytest.mark.asyncio
    async def test_cache_probe_failure_degrades_to_none_and_pins_no_miss(self):
        """Couldn't-ask is never a known miss: a probe failure must not pin the
        1-hour miss a probe-answered empty earns."""
        svc = AsyncMock()
        svc.cache_service = AsyncMock()
        svc.cache_service.get_release_lean = AsyncMock(return_value=None)
        svc.cache_service.search_releases = AsyncMock(side_effect=RuntimeError("pg down"))
        pg = _RecordingPg()
        resolved = await resolve_typed_album_level_match(
            svc, pg, artist="Agriculture", album="The Spiritual Sound"
        )
        assert resolved is None
        assert pg.store == {}

    @pytest.mark.asyncio
    async def test_blank_album_returns_none(self):
        svc = AsyncMock()
        resolved = await resolve_typed_album_level_match(
            svc, None, artist="Agriculture", album="   "
        )
        assert resolved is None

    @pytest.mark.asyncio
    async def test_self_titled_placeholder_swaps_to_the_artist_name(self):
        """Parity with the ARTIST_PLUS_ALBUM class (LML#784 category 4): a
        query-side "S/T" album can never match a real cache title."""
        svc = AsyncMock()
        svc.cache_service = AsyncMock()
        svc.cache_service.search_releases = AsyncMock(
            return_value=[_cache_row(22222222, "Duster", "Duster")]
        )
        resolved = await resolve_typed_album_level_match(svc, None, artist="Duster", album="S/T")
        assert resolved is not None
        assert resolved.release_id == 22222222

    @pytest.mark.asyncio
    async def test_malformed_cache_row_degrades_to_none(self):
        """Review fix 7: a row missing the hard-indexed keys must degrade like
        any other probe failure, never escape as a KeyError 500."""
        svc = AsyncMock()
        svc.cache_service = AsyncMock()
        svc.cache_service.search_releases = AsyncMock(return_value=[{"release_id": 5}])
        resolved = await resolve_typed_album_level_match(
            svc, None, artist="Agriculture", album="The Spiritual Sound"
        )
        assert resolved is None


# ---------------------------------------------------------------------------
# Orchestrator tier: the three digest repro shapes, end to end.
# ---------------------------------------------------------------------------

JILL_SHELF_ROWS = [
    make_library_item(id=71, artist="Jill Scott", title="Who Is Jill Scott?"),
    make_library_item(id=72, artist="Jill Scott", title="Beautifully Human"),
]

REPRO_SHAPES = [
    pytest.param(
        {
            "artist": "Eliana Glass",
            "album": "E at Home",
            "song": "E at Home",
            "cache_rows": [_cache_row(ELIANA_GLASS_RELEASE_ID, "E at Home", "Eliana Glass")],
            "track_candidates": [
                _release_info(ELIANA_GLASS_RELEASE_ID, "E at Home", "Eliana Glass")
            ],
            "shelf_rows": [],
            "expected_release_id": ELIANA_GLASS_RELEASE_ID,
            "expected_title": "E at Home",
        },
        id="album-title-typed-as-track",
    ),
    pytest.param(
        {
            "artist": "Agriculture",
            "album": "The Spiritual Sound",
            "song": "Micah (5:15 AM)",
            "cache_rows": [
                _cache_row(AGRICULTURE_RELEASE_ID, "The Spiritual Sound", "Agriculture")
            ],
            "track_candidates": [
                _release_info(AGRICULTURE_RELEASE_ID, "The Spiritual Sound", "Agriculture")
            ],
            "shelf_rows": [],
            "expected_release_id": AGRICULTURE_RELEASE_ID,
            "expected_title": "The Spiritual Sound",
        },
        id="one-space-track-spelling-variant",
    ),
    pytest.param(
        {
            "artist": "Jill Scott",
            "album": "To Whom it May Concern",
            "song": "Pressha",
            "cache_rows": [
                _cache_row(JILL_SCOTT_RELEASE_ID, "To Whom This May Concern", "Jill Scott")
            ],
            # Candidate crowding: an unrelated Discogs artist is named Pressha,
            # so the bounded resolve validates (and fails) five crowd releases
            # and truncates before ever reaching the real one.
            "track_candidates": [
                _release_info(90000000 + n, f"Crowd Release {n}", "Pressha") for n in range(6)
            ]
            + [_release_info(JILL_SCOTT_RELEASE_ID, "To Whom This May Concern", "Jill Scott")],
            "shelf_rows": JILL_SHELF_ROWS,
            "expected_release_id": JILL_SCOTT_RELEASE_ID,
            "expected_title": "To Whom This May Concern",
        },
        id="crowd-out-on-short-reused-track-name",
    ),
]


class TestSongBearingLibraryMissDegradesToAlbumMatch:
    """perform_lookup surfaces the typed (artist, album) answer when the track leg fails."""

    async def _run(self, shape: dict):
        artist, album, song = shape["artist"], shape["album"], shape["song"]
        svc = _build_discogs_service(
            artist=artist,
            album_cache_rows=shape["cache_rows"],
            track_candidates=shape["track_candidates"],
            song=song,
            rehydrate=ReleaseMetadataResponse(
                release_id=shape["expected_release_id"],
                title=shape["expected_title"],
                artist=artist,
                release_url=f"https://www.discogs.com/release/{shape['expected_release_id']}",
                artwork_url=f"https://i.discogs.com/{shape['expected_release_id']}.jpg",
            ),
        )
        db = _build_library_db({artist: shape["shelf_rows"]})
        request = LookupRequest(
            artist=artist,
            album=album,
            song=song,
            raw_message=f"{artist} - {album} - {song}",
        )
        return await perform_lookup(request, db, svc, make_lml_telemetry())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", REPRO_SHAPES)
    async def test_album_level_match_persists_when_track_resolution_fails(
        self, shape, enable_nonlibrary_release
    ):
        response = await self._run(shape)
        assert response.results, "the lookup must not zero out on a failed track resolution"
        top = response.results[0]
        assert top.library_item.id == ROWLESS_LIBRARY_ID
        assert top.library_item.title == shape["expected_title"]
        assert top.artwork is not None
        assert top.artwork.release_id == shape["expected_release_id"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", REPRO_SHAPES)
    async def test_search_type_and_song_not_found_stay_honest(
        self, shape, enable_nonlibrary_release
    ):
        """Constraint 3: BS's track-context trust gate (trust.ts) accepts only
        direct|compilation — an album-level degrade must present as neither, and
        the unconfirmed track must stay flagged."""
        response = await self._run(shape)
        assert response.song_not_found is True
        assert response.found_on_compilation is False
        assert response.search_type not in ("direct", "compilation")

    @pytest.mark.asyncio
    async def test_crowd_out_shape_serves_the_typed_pair_alone(self, enable_nonlibrary_release):
        """The artist's unrelated shelf albums were already album-floor-dropped
        by ARTIST_PLUS_ALBUM (``_filter_results_by_album_match`` — the typed
        album matches none of them), so the pre-fix response was EMPTY. The
        degrade serves exactly the typed-pair answer."""
        shape = REPRO_SHAPES[2].values[0]
        response = await self._run(shape)
        ids = [item.library_item.id for item in response.results]
        assert ids == [ROWLESS_LIBRARY_ID]


class TestUnconfirmedAlbumOutcome:
    """Direct contract of the strategy-side outcome shaping."""

    def _resolved(self) -> ResolvedRelease:
        return ResolvedRelease(
            release_id=JILL_SCOTT_RELEASE_ID,
            release_url=f"https://www.discogs.com/release/{JILL_SCOTT_RELEASE_ID}",
            is_compilation=False,
            album_title="To Whom This May Concern",
            track_confirmed=False,
        )

    def test_prior_fallback_rows_ride_behind_the_album_answer(self):
        """The LML#1184 presentation: when prior (non-answering) rows survived
        into ``state.results``, they append behind the typed-pair answer rather
        than being evicted by ``_apply``'s wholesale replace."""
        parsed = ParsedRequest(artist="Jill Scott", album="To Whom it May Concern", song="Pressha")
        state = SearchState()
        state.results = list(JILL_SHELF_ROWS)
        rowless = make_library_item(
            id=ROWLESS_LIBRARY_ID, artist="Jill Scott", title="To Whom This May Concern"
        )
        titles = {ROWLESS_LIBRARY_ID: self._resolved()}
        outcome = _unconfirmed_album_outcome(parsed, state, [rowless], titles)
        assert [r.id for r in outcome.items] == [ROWLESS_LIBRARY_ID, 71, 72]
        assert outcome.song_not_found_after is True
        assert outcome.found_on_compilation_after is False
        assert outcome.discogs_titles == titles

    def test_a_surfaced_row_answering_the_typed_pair_suppresses_the_degrade(self):
        """A row-less duplicate of a shelved record is the LML#629 Minimoonstar
        failure class — when a surfaced row already answers the typed pair, the
        degrade contributes nothing and the shelf row keeps the album metadata
        (the library-lane behavior this fix mirrors)."""
        parsed = ParsedRequest(artist="Stereolab", album="Aluminum Tunes", song="Fanfare")
        state = SearchState()
        state.results = [make_library_item(id=45353, artist="Stereolab", title="Aluminum Tunes")]
        rowless = make_library_item(
            id=ROWLESS_LIBRARY_ID, artist="Stereolab", title="Aluminum Tunes"
        )
        titles = {ROWLESS_LIBRARY_ID: self._resolved()}
        outcome = _unconfirmed_album_outcome(parsed, state, [rowless], titles)
        assert outcome.items == []
        assert outcome.discogs_titles is None

    @pytest.mark.parametrize("shelf_title", ["S/T", "Duster"])
    def test_self_titled_query_recognizes_the_shelved_self_titled_row(self, shelf_title):
        """Review fix 2: the pair guard must mirror the kernel's LML#784
        self-titled swap (and the catalog's own "S/T" filing form, the
        artist_plus_album.py sibling), or a typed "S/T" album surfaces a
        row-less duplicate ahead of the shelved self-titled record — the
        Minimoonstar class this guard exists to prevent."""
        parsed = ParsedRequest(artist="Duster", album="S/T", song="Unheard Track")
        state = SearchState()
        state.results = [make_library_item(id=88, artist="Duster", title=shelf_title)]
        rowless = make_library_item(id=ROWLESS_LIBRARY_ID, artist="Duster", title="Duster")
        titles = {
            ROWLESS_LIBRARY_ID: ResolvedRelease(
                release_id=22222222,
                release_url="https://www.discogs.com/release/22222222",
                is_compilation=False,
                album_title="Duster",
                track_confirmed=False,
            )
        }
        outcome = _unconfirmed_album_outcome(parsed, state, [rowless], titles)
        assert outcome.items == []


class TestSongAsAlbumTitlePromotionHonesty:
    """Review fix 1: step 3b's LML#717 promotion must never promote the
    never-track-confirmed degrade row into a found answer."""

    @pytest.mark.asyncio
    async def test_degrade_row_is_never_promoted_to_a_found_answer(self, enable_nonlibrary_release):
        """The DOGA class: song == album (album title typed in the track
        field) plus a shelf row ("Remixes of DOGA") that survives
        ARTIST_PLUS_ALBUM's token-set floor (100 — token subset) but fails
        ``album_title_acceptable`` (ratio 42), so the pair guard does not
        suppress the degrade and step 3b runs over [rowless, shelf]. The
        id=0 row scores 100 against the typed song and — without the
        exclusion — is promoted with ``song_not_found=False`` while
        ``search_type`` stays ``fallback``: internally inconsistent, and a
        found-claim nothing track-confirmed."""
        artist, album, song = "Juana Molina", "DOGA", "DOGA"
        shelf = make_library_item(id=46524, artist=artist, title="Remixes of DOGA")
        svc = _build_discogs_service(
            artist=artist,
            album_cache_rows=[_cache_row(44444444, "DOGA", artist)],
            track_candidates=[_release_info(44444444, "DOGA", artist)],
            song=song,
        )
        db = _build_library_db({f"{artist} {album}": [shelf]})
        request = LookupRequest(
            artist=artist, album=album, song=song, raw_message=f"{artist} - {album} - {song}"
        )
        response = await perform_lookup(request, db, svc, make_lml_telemetry())
        ids = [item.library_item.id for item in response.results]
        assert not (response.song_not_found is False and ROWLESS_LIBRARY_ID in ids), (
            "the id=0 degrade row was promoted to a found answer without any track confirmation"
        )


class TestKernelCallSiteHonesty:
    """Review fix 5: the SONG_AS_TRACK/SWAPPED kernel surfaces every non-None
    kernel result via Outcome.track_match (song_not_found_after=False), so a
    track-unconfirmed release must never be surfaced there."""

    @pytest.mark.asyncio
    async def test_track_unconfirmed_release_is_not_surfaced_as_a_track_match(
        self, enable_nonlibrary_release, monkeypatch
    ):
        from unittest.mock import patch

        from lookup.strategies.track_release_matching import _match_track_releases_to_library

        unconfirmed = ResolvedRelease(
            release_id=AGRICULTURE_RELEASE_ID,
            release_url=f"https://www.discogs.com/release/{AGRICULTURE_RELEASE_ID}",
            is_compilation=False,
            album_title="The Spiritual Sound",
            track_confirmed=False,
        )
        svc = _build_discogs_service(
            artist="Agriculture",
            album_cache_rows=[],
            track_candidates=[
                _release_info(AGRICULTURE_RELEASE_ID, "The Spiritual Sound", "Agriculture")
            ],
            song="Micah (5:15 AM)",
        )
        db = _build_library_db()
        with patch(
            "lookup.strategies.track_release_matching._resolve_nonlibrary_release",
            AsyncMock(return_value=unconfirmed),
        ):
            items, hints, titles = await _match_track_releases_to_library(
                db,
                svc,
                "Micah (5:15 AM)",
                artist="Agriculture",
                source="swapped_interpretation",
                require_artist="Agriculture",
            )
        assert items == []
        assert titles == {}


class TestLibraryLaneParity:
    """A shelved album answering the typed pair is untouched by the degrade."""

    @pytest.mark.asyncio
    async def test_shelved_pair_keeps_its_row_and_gains_no_rowless_duplicate(
        self, enable_nonlibrary_release
    ):
        """The artist+song FTS branch surfaces the shelved pair with the song
        still unconfirmed (``song_not_found=True``) — the exact library-lane
        shape the 2026-08-17 audit measured (album persists, track-scoped
        fields null). TRACK_ON_COMPILATION then runs, its carve degrades to
        the same release... and must contribute nothing, because the shelf row
        already answers the typed pair."""
        artist, album, song = "Stereolab", "Aluminum Tunes", "Zzyzx Marginal Fanfare"
        shelf_row = make_library_item(id=45353, artist=artist, title=album)
        svc = _build_discogs_service(
            artist=artist,
            album_cache_rows=[_cache_row(33333333, album, artist)],
            track_candidates=[_release_info(33333333, album, artist)],
            song=song,
        )
        # The album-fed FTS query misses; the artist+song branch finds the row
        # (and it survives the album-match floor, matching the typed album).
        db = _build_library_db({f"{artist} {song}": [shelf_row]})
        request = LookupRequest(
            artist=artist, album=album, song=song, raw_message=f"{artist} - {album} - {song}"
        )
        response = await perform_lookup(request, db, svc, make_lml_telemetry())
        ids = [item.library_item.id for item in response.results]
        assert 45353 in ids
        assert ROWLESS_LIBRARY_ID not in ids, (
            "the album-level degrade must never surface a row-less duplicate of "
            "a shelved row that already answers the typed pair"
        )
        assert response.song_not_found is True
