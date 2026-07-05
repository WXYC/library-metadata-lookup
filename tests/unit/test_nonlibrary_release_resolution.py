"""Unit tests for the row-less resolve helper + its #632 cache (LML#628).

``_resolve_nonlibrary_release`` is the shared kernel the three Discogs-aware
strategies call at their library-gate drop point. It:

1. Reads the #632 PG positive cache (``get_cached_release_id``) keyed on the
   typed artist, short-circuiting on a fresh hit (a release_id → re-hydrate via
   ``get_release``) or a fresh known miss (``was_present=True, release_id=None``
   → skip the probe).
2. On a cold miss, resolves via the **uncached, bounded** resolver
   (``resolve_release_for_track(..., max_validations=5)``) — never the L1
   ``resolve_release_for_track_cached`` wrapper whose key omits ``max_validations``
   (the #633 landmine).
3. Writes the resolution back to the #632 cache (positive id or a known miss).

These tests pin the amortization contract: a second identical lookup reads the
cache and does NOT re-probe Discogs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.models import (
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackReleasesResponse,
)
from entity.release_resolution_cache import ReleaseResolution
from lookup.rowless import _resolve_nonlibrary_release

RELEASE_ID = 36907527
RELEASE_URL = f"https://www.discogs.com/release/{RELEASE_ID}"
ALBUM = "When There Is No Sun"
ARTIST = "A Guy Called Gerald"
TRACK = "Message to Black Youth"


def _release() -> ReleaseInfo:
    return ReleaseInfo(
        album=ALBUM,
        artist="Various",
        release_id=RELEASE_ID,
        release_url=RELEASE_URL,
        is_compilation=True,
    )


def _track_releases() -> TrackReleasesResponse:
    return TrackReleasesResponse(
        track=TRACK, artist=ARTIST, releases=[_release()], total=1, cached=False
    )


def _empty_track_releases() -> TrackReleasesResponse:
    return TrackReleasesResponse(track="", artist="", releases=[], total=0, cached=False)


def _resolving_service() -> AsyncMock:
    svc = AsyncMock()
    svc.cache_service = None
    svc.search_releases_by_track = AsyncMock(return_value=_track_releases())
    svc.validate_track_on_release = AsyncMock(return_value=True)
    svc.search_releases_by_album_title = AsyncMock(return_value=_empty_track_releases())
    svc.get_release = AsyncMock(
        return_value=ReleaseMetadataResponse(
            release_id=RELEASE_ID,
            title=ALBUM,
            artist="Various",
            release_url=RELEASE_URL,
            artwork_url="https://i.discogs.com/sun.jpg",
        )
    )
    return svc


class _RecordingPg:
    """A minimal PgSource stand-in backed by a dict, recording read/write calls.

    Stores by ``(artist_normalized, title_normalized, is_track)``. Mirrors the
    SELECT/UPSERT shape ``release_resolution_cache`` uses closely enough that
    ``get_cached_release_id`` / ``set_cached_release_id`` exercise their real
    normalization + three-valued logic against it.
    """

    def __init__(self):
        self.store: dict[tuple[str, str, bool], int | None] = {}
        self.fetchone_calls = 0
        self.execute_calls = 0

    async def fetchone(self, query: str, *args):
        self.fetchone_calls += 1
        artist_key, title_key, is_track = args[0], args[1], args[2]
        key = (artist_key, title_key, is_track)
        if key not in self.store:
            return None
        # Present rows are always "fresh" in this stand-in (the staleness
        # cutoffs are real cutoffs but the row's resolved_at is now()).
        return {"release_id": self.store[key]}

    async def execute(self, query: str, *args):
        self.execute_calls += 1
        artist_key, title_key, is_track, release_id = args[0], args[1], args[2], args[3]
        self.store[(artist_key, title_key, is_track)] = release_id
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_cold_resolve_then_cache_hit_skips_second_probe():
    """First call resolves + writes the cache; the second identical call reads
    the cache and does NOT re-probe ``search_releases_by_track``."""
    svc = _resolving_service()
    pg = _RecordingPg()

    first = await _resolve_nonlibrary_release(
        svc, pg, song=TRACK, artist=ARTIST, album=ALBUM, is_track=True
    )
    assert first is not None
    assert first.release_id == RELEASE_ID
    assert first.release_url == RELEASE_URL
    assert svc.search_releases_by_track.await_count >= 1
    probes_after_first = svc.search_releases_by_track.await_count
    # A positive resolution was written to the #632 cache.
    assert pg.execute_calls == 1
    assert pg.store[(ARTIST.lower(), TRACK.lower(), True)] == RELEASE_ID

    second = await _resolve_nonlibrary_release(
        svc, pg, song=TRACK, artist=ARTIST, album=ALBUM, is_track=True
    )
    assert second is not None
    assert second.release_id == RELEASE_ID
    assert second.release_url == RELEASE_URL
    # The whole point: no second resolve. The probe count is unchanged.
    assert svc.search_releases_by_track.await_count == probes_after_first


@pytest.mark.asyncio
async def test_transient_rehydrate_failure_does_not_demote_positive_to_miss():
    """A known-good cached id that fails to re-hydrate (transient ``get_release``
    outage) must NOT be overwritten with a known miss when the live resolve also
    comes up empty — otherwise a transient outage pins a 7-day miss over good
    data. It self-heals once the outage clears."""
    svc = _resolving_service()
    # Outage: get_release can't fetch (rehydrate fails) AND the live re-probe
    # finds nothing (the same outage starves validation upstream).
    svc.get_release = AsyncMock(return_value=None)
    svc.search_releases_by_track = AsyncMock(return_value=_empty_track_releases())

    pg = _RecordingPg()
    # Pre-seed a known-good positive entry (a prior successful resolve).
    pg.store[(ARTIST.lower(), TRACK.lower(), True)] = RELEASE_ID

    result = await _resolve_nonlibrary_release(
        svc, pg, song=TRACK, artist=ARTIST, album=ALBUM, is_track=True
    )

    # Nothing surfaces this add, but the cache is NOT demoted to a miss.
    assert result is None
    assert pg.execute_calls == 0
    assert pg.store[(ARTIST.lower(), TRACK.lower(), True)] == RELEASE_ID


@pytest.mark.asyncio
async def test_empty_title_rehydrate_falls_through_to_live_resolve():
    """A #632 cache hit whose release rehydrates to an empty title (a malformed,
    title-less but non-404 Discogs release) must NOT surface a degenerate
    title-less row-less item — it falls through to the live resolve instead,
    reusing the ``cached_positive_unhydrated`` machinery (no demotion)."""
    svc = _resolving_service()
    # The cache-hit rehydrate returns a present (release_id>0, not 404) but
    # title-less release. The live re-probe still finds the real release.
    svc.get_release = AsyncMock(
        return_value=ReleaseMetadataResponse(
            release_id=RELEASE_ID,
            title="",
            artist="Various",
            release_url=RELEASE_URL,
            artwork_url="https://i.discogs.com/sun.jpg",
        )
    )
    pg = _RecordingPg()
    pg.store[(ARTIST.lower(), TRACK.lower(), True)] = RELEASE_ID  # known-good positive

    result = await _resolve_nonlibrary_release(
        svc, pg, song=TRACK, artist=ARTIST, album=ALBUM, is_track=True
    )

    # Fell through to the live resolve (search_releases_by_track -> real title),
    # not the empty-title rehydrate.
    assert result is not None
    assert result.album_title == ALBUM


@pytest.mark.asyncio
async def test_fresh_known_miss_short_circuits_without_probe(monkeypatch):
    """A cached known-miss (``was_present=True, release_id=None``) skips the
    live probe entirely."""
    svc = _resolving_service()

    async def _miss(*_a, **_k):
        return ReleaseResolution(release_id=None, was_present=True)

    monkeypatch.setattr("lookup.rowless.get_cached_release_id", AsyncMock(side_effect=_miss))
    set_spy = AsyncMock()
    monkeypatch.setattr("lookup.rowless.set_cached_release_id", set_spy)

    result = await _resolve_nonlibrary_release(
        svc, object(), song=TRACK, artist=ARTIST, album=ALBUM, is_track=True
    )

    assert result is None
    svc.search_releases_by_track.assert_not_called()
    # A known miss is authoritative — no write-back needed.
    set_spy.assert_not_called()


@pytest.mark.asyncio
async def test_cold_miss_writes_known_miss_to_cache(monkeypatch):
    """When the resolver finds nothing, a known-miss row is written so the next
    identical lookup short-circuits."""
    svc = _resolving_service()
    svc.search_releases_by_track = AsyncMock(return_value=_empty_track_releases())
    pg = _RecordingPg()

    result = await _resolve_nonlibrary_release(
        svc, pg, song=TRACK, artist=ARTIST, album=ALBUM, is_track=True
    )

    assert result is None
    # Known miss recorded (release_id None).
    assert pg.execute_calls == 1
    assert pg.store[(ARTIST.lower(), TRACK.lower(), True)] is None


@pytest.mark.asyncio
async def test_no_pg_resolves_uncached_and_does_not_crash():
    """With no PG handle the helper still resolves via the bounded resolver —
    the cache is best-effort, not required."""
    svc = _resolving_service()

    result = await _resolve_nonlibrary_release(
        svc, None, song=TRACK, artist=ARTIST, album=ALBUM, is_track=True
    )

    assert result is not None
    assert result.release_id == RELEASE_ID


@pytest.mark.asyncio
async def test_bounded_resolver_is_used_not_l1_wrapper(monkeypatch):
    """The helper must call the UNCACHED bounded resolver with max_validations=5
    — never the L1 ``resolve_release_for_track_cached`` wrapper (the #633
    landmine: its key omits max_validations)."""
    svc = _resolving_service()

    bounded = AsyncMock(return_value=[])
    monkeypatch.setattr("lookup.rowless.resolve_release_for_track", bounded)
    # If the L1 wrapper is ever imported into orchestrator and used, this spy
    # would catch it. It is intentionally NOT referenced by the helper.
    monkeypatch.setattr(
        "lookup.rowless.get_cached_release_id",
        AsyncMock(return_value=ReleaseResolution(release_id=None, was_present=False)),
    )
    monkeypatch.setattr("lookup.rowless.set_cached_release_id", AsyncMock())

    await _resolve_nonlibrary_release(
        svc, None, song=TRACK, artist=ARTIST, album=ALBUM, is_track=True
    )

    bounded.assert_awaited_once()
    _args, kwargs = bounded.await_args
    assert kwargs.get("max_validations") == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("album", "expected_probe"),
    [(ALBUM, True), (None, False), ("", False)],
)
async def test_album_title_wave_gated_on_album_presence(monkeypatch, album, expected_probe):
    """The bounded resolve fires the album-title wave only when an album is
    present (#646) — parity with the in-library #604 lazy-bind, which gates the
    same probe on ``bool(album)``. Without it, a non-library release reachable
    only by album title (the #319/#237 trio / odd-credit shape) is missed."""
    svc = _resolving_service()

    bounded = AsyncMock(return_value=[])
    monkeypatch.setattr("lookup.rowless.resolve_release_for_track", bounded)
    monkeypatch.setattr(
        "lookup.rowless.get_cached_release_id",
        AsyncMock(return_value=ReleaseResolution(release_id=None, was_present=False)),
    )
    monkeypatch.setattr("lookup.rowless.set_cached_release_id", AsyncMock())

    await _resolve_nonlibrary_release(
        svc, None, song=TRACK, artist=ARTIST, album=album, is_track=True
    )

    bounded.assert_awaited_once()
    _args, kwargs = bounded.await_args
    assert kwargs.get("also_probe_album_title") == expected_probe


# ---------------------------------------------------------------------------
# _recover_track_credit (LML#660): the SONG_AS_TRACK per-track-credit anchor
# ---------------------------------------------------------------------------


def _non_comp_release(release_id: int) -> ReleaseInfo:
    return ReleaseInfo(
        album="Some Single",
        artist="Various",
        release_id=release_id,
        release_url=f"https://www.discogs.com/release/{release_id}",
        is_compilation=False,
    )


class TestRecoverTrackCredit:
    @pytest.mark.asyncio
    async def test_returns_first_recoverable_per_track_credit(self):
        from lookup.rowless import _recover_track_credit

        svc = AsyncMock()
        svc.get_track_credit_on_release = AsyncMock(return_value=ARTIST)

        credit = await _recover_track_credit(svc, [_release()], TRACK)

        assert credit == ARTIST
        svc.get_track_credit_on_release.assert_awaited_once_with(RELEASE_ID, TRACK)

    @pytest.mark.asyncio
    async def test_returns_none_when_no_release_exposes_credit(self):
        from lookup.rowless import _recover_track_credit

        svc = AsyncMock()
        svc.get_track_credit_on_release = AsyncMock(return_value=None)

        credit = await _recover_track_credit(svc, [_release(), _non_comp_release(99)], TRACK)

        assert credit is None

    @pytest.mark.asyncio
    async def test_scans_compilations_before_own_releases(self):
        """The per-track credit lives on the V/A comp, so comps are fetched first
        even when an own-artist release precedes them in the probe results."""
        from lookup.rowless import _recover_track_credit

        svc = AsyncMock()
        svc.get_track_credit_on_release = AsyncMock(return_value=ARTIST)

        # Own-release first in input order; the comp must still be scanned first.
        credit = await _recover_track_credit(svc, [_non_comp_release(99), _release()], TRACK)

        assert credit == ARTIST
        svc.get_track_credit_on_release.assert_awaited_once_with(RELEASE_ID, TRACK)

    @pytest.mark.asyncio
    async def test_bounds_fetches_to_the_cap(self, monkeypatch):
        """A popular track with many credit-less candidates must not fan out an
        unbounded number of release fetches."""
        from lookup import rowless
        from lookup.rowless import _recover_track_credit

        monkeypatch.setattr(rowless, "_MAX_CREDIT_RECOVERY_FETCHES", 3)
        svc = AsyncMock()
        svc.get_track_credit_on_release = AsyncMock(return_value=None)
        releases = [_non_comp_release(i) for i in range(1, 11)]

        credit = await _recover_track_credit(svc, releases, TRACK)

        assert credit is None
        assert svc.get_track_credit_on_release.await_count == 3

    @pytest.mark.asyncio
    async def test_idless_releases_do_not_consume_fetch_budget(self, monkeypatch):
        """A release with no usable ``release_id`` is unfetchable, so it must not
        burn a slot of the fetch budget — the cap bounds *real* fetches. A creditful
        release sitting behind id-less ones is still reached within the cap."""
        from lookup import rowless
        from lookup.rowless import _recover_track_credit

        monkeypatch.setattr(rowless, "_MAX_CREDIT_RECOVERY_FETCHES", 2)
        svc = AsyncMock()
        svc.get_track_credit_on_release = AsyncMock(return_value=ARTIST)

        idless = ReleaseInfo(
            album="No Id Comp",
            artist="Various",
            release_id=0,
            release_url="https://www.discogs.com/release/0",
            is_compilation=True,
        )
        # Two id-less comps ahead of the one creditful release. With the cap at 2,
        # counting the id-less releases against the budget would stop before the
        # creditful release and miss the credit.
        releases = [idless, idless, _release()]

        credit = await _recover_track_credit(svc, releases, TRACK)

        assert credit == ARTIST
        svc.get_track_credit_on_release.assert_awaited_once_with(RELEASE_ID, TRACK)
