"""A1 carry-through: non-library resolvable releases surface row-less (LML#628).

The track-validating Discogs-aware strategies (TRACK_ON_COMPILATION,
SONG_AS_TRACK, SWAPPED_INTERPRETATION) gate a candidate Discogs release on a
fuzzy *library-row* match first and drop it if none exists — even when the
release validates from the inputs (#536 defers track-validation until after the
library gate). LML#628 wires the row-less carry-through: when the flag is on and
no library row matches, the strategy resolves + validates the release and emits
the ``LibraryItem(id=0)`` + real ``DiscogsSearchResult`` shape the library-miss
search already produces (orchestrator.py ``_library_miss_discogs_search``),
rather than the BS#1185 ``release_id=0`` sentinel.

Spine case (the #604 repro shape): A Guy Called Gerald — "When There Is No Sun"
(a track on a Various-Artists compilation filed under the track artist, so the
library never carries the release). One case per strategy here, plus a
#632 cache-threading check (the deeper read-amortization is unit-tested in
``tests/unit/test_nonlibrary_release_resolution.py``) and a regression for the
LML#487 enrichment gate clobbering the row-less binding on a mismatched request
album. The wire-contract guard for the row-less shape lives in
``tests/unit/test_rowless_result_contract.py``.

Driven end-to-end through ``perform_lookup`` against the real seeded
``library_db`` with a mock ``DiscogsService`` so the strategy gating, the
two-channel artist seam (#626), and Step-6 id==0 serialization are all
exercised. Flag default-off asserts today's empty/sentinel behavior.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.models import (
    DiscogsSearchResponse,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackReleasesResponse,
)
from lookup.models import LookupRequest
from lookup.orchestrator import ROWLESS_NO_ALBUM_CONFIDENCE, perform_lookup
from tests.conftest import make_lml_telemetry

# A real-looking Discogs release id + canonical URL for the non-library
# compilation the spine track sits on. Distinct from every seeded library row.
SPINE_RELEASE_ID = 36907527
SPINE_RELEASE_URL = f"https://www.discogs.com/release/{SPINE_RELEASE_ID}"
SPINE_ALBUM = "When There Is No Sun"
SPINE_ARTIST = "A Guy Called Gerald"
SPINE_TRACK = "Message to Black Youth"


@pytest.fixture
def enable_nonlibrary_release(monkeypatch):
    """Turn on LML_RESOLVE_NONLIBRARY_RELEASE for the carry-through."""
    monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _spine_release(
    release_id: int = SPINE_RELEASE_ID, artist: str = "Various", album: str = SPINE_ALBUM
) -> ReleaseInfo:
    """The V/A compilation release carrying the spine track — not in library."""
    return ReleaseInfo(
        album=album,
        artist=artist,
        release_id=release_id,
        release_url=f"https://www.discogs.com/release/{release_id}",
        is_compilation=True,
    )


def _track_releases(*releases: ReleaseInfo) -> TrackReleasesResponse:
    return TrackReleasesResponse(
        track=SPINE_TRACK,
        artist=SPINE_ARTIST,
        releases=list(releases),
        total=len(releases),
        cached=False,
    )


def _empty_track_releases() -> TrackReleasesResponse:
    return TrackReleasesResponse(track="", artist="", releases=[], total=0, cached=False)


def _spine_release_metadata() -> ReleaseMetadataResponse:
    return ReleaseMetadataResponse(
        release_id=SPINE_RELEASE_ID,
        title=SPINE_ALBUM,
        artist="Various",
        release_url=SPINE_RELEASE_URL,
        artwork_url="https://i.discogs.com/sun.jpg",
    )


def _base_discogs_mock() -> AsyncMock:
    """A mock DiscogsService that surfaces the spine release only via the
    track-probe, never via the library-miss ``search()`` (which the row-less
    path does NOT use) and never via a library-matching album title."""
    svc = AsyncMock()
    svc.cache_service = None
    # The library-miss Step-3a probe + the strategies' floor re-search both go
    # through search(); return nothing so neither short-circuits the strategy.
    svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
    svc.validate_track_on_release = AsyncMock(return_value=True)
    svc.search_releases_by_album_title = AsyncMock(return_value=_empty_track_releases())
    svc.get_release = AsyncMock(return_value=_spine_release_metadata())
    return svc


class _RecordingPg:
    """Dict-backed ``PgSource`` stand-in for the #632 cache, recording writes.

    Keyed on ``(artist_normalized, title_normalized, is_track)`` — close enough
    to the ``release_resolution_cache`` SELECT/UPSERT shape that the real
    ``get_cached_release_id`` / ``set_cached_release_id`` exercise their
    normalization + three-valued logic against it.
    """

    def __init__(self):
        self.store: dict[tuple[str, str, bool], int | None] = {}
        self.execute_calls = 0

    async def fetchone(self, query: str, *args):
        key = (args[0], args[1], args[2])
        if key not in self.store:
            return None
        return {"release_id": self.store[key]}

    async def execute(self, query: str, *args):
        self.execute_calls += 1
        self.store[(args[0], args[1], args[2])] = args[3]
        return "INSERT 0 1"


# ---------------------------------------------------------------------------
# TRACK_ON_COMPILATION
# ---------------------------------------------------------------------------


async def _run_track_on_compilation(library_db, svc, *, pg=None):
    """Drive perform_lookup down the TRACK_ON_COMPILATION cut-site.

    The artist is not in the seeded library, ``resolve_albums_for_track`` sees
    only a V/A release for the track (sets ``song_not_found``), and the
    compilation album title is not shelved — so ``search_compilations_for_track``
    finds the Discogs release but no library row.
    """
    request = LookupRequest(
        artist=SPINE_ARTIST,
        song=SPINE_TRACK,
        raw_message=f"{SPINE_ARTIST} - {SPINE_TRACK}",
    )
    return await perform_lookup(
        request,
        library_db,
        svc,
        telemetry=make_lml_telemetry(),
        discogs_cache_pg=pg,
    )


@pytest.mark.asyncio
async def test_track_on_compilation_surfaces_rowless_when_flag_on(
    library_db, enable_nonlibrary_release
):
    svc = _base_discogs_mock()
    # resolve_albums_for_track probes by track (no album) and validate; surface
    # the V/A comp so song_not_found is set, then the compilation strategy
    # re-finds it by track.
    svc.search_releases_by_track = AsyncMock(return_value=_track_releases(_spine_release()))

    response = await _run_track_on_compilation(library_db, svc)

    assert len(response.results) == 1
    item = response.results[0]
    assert item.library_item.id == 0
    assert item.artwork is not None
    assert item.artwork.release_id == SPINE_RELEASE_ID
    assert item.artwork.release_id > 0
    assert item.artwork.release_url == SPINE_RELEASE_URL
    # Pin routing: served by the compilation path, not silently re-routed
    # through SWAPPED (which would report "alternative"). All three strategies
    # emit byte-identical row-less output, so without this a priority change
    # could re-route the test and still pass.
    assert response.search_type == "compilation"


@pytest.mark.asyncio
async def test_track_on_compilation_flag_off_no_rowless(library_db):
    svc = _base_discogs_mock()
    svc.search_releases_by_track = AsyncMock(return_value=_track_releases(_spine_release()))

    response = await _run_track_on_compilation(library_db, svc)

    # Flag off: no row-less identity surfaces. Either no results, or no result
    # carries a real Discogs identity (release_id > 0).
    assert not any(
        r.library_item.id == 0 and r.artwork is not None and r.artwork.release_id > 0
        for r in response.results
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("extended", [False, True])
async def test_track_on_compilation_survives_mismatched_request_album(
    library_db, enable_nonlibrary_release, extended
):
    """A request album that doesn't match the resolved compilation title must NOT
    clobber the validated carry-through binding down to the BS#1185 sentinel.

    The carry-through resolves by *track*, so the resolved release title ("When
    There Is No Sun") routinely differs from a typed album. The LML#487 title
    gate in ``enrich_artwork_results`` guards a *library row*'s artwork from
    leaking onto a mismatched album — but a row-less item has no library row to
    leak from, and its ``release_id`` was validated to carry the track. Gating it
    emitted ``release_id=0`` (the sentinel) for the common album-bearing request,
    silently defeating the feature.

    Parametrized over ``extended`` because Backend-Service forces ``extended=true``
    on every write-path call (LML#504) — the extended branch takes the
    album-derived-eligible enrichment path that the lean branch doesn't, so the
    binding must survive both.
    """
    svc = _base_discogs_mock()
    svc.search_releases_by_track = AsyncMock(return_value=_track_releases(_spine_release()))

    request = LookupRequest(
        artist=SPINE_ARTIST,
        song=SPINE_TRACK,
        album="Black Secret Technology",  # the artist's own LP, not the V/A comp
        extended=extended,
        raw_message=f"{SPINE_ARTIST} - {SPINE_TRACK}",
    )
    response = await perform_lookup(
        request,
        library_db,
        svc,
        telemetry=make_lml_telemetry(),
        discogs_cache_pg=None,
    )

    rowless = [r for r in response.results if r.library_item.id == 0 and r.artwork is not None]
    assert len(rowless) == 1
    # The validated Discogs identity survives enrichment — NOT the release_id=0 sentinel.
    assert rowless[0].artwork.release_id == SPINE_RELEASE_ID
    assert rowless[0].artwork.release_id > 0
    assert rowless[0].artwork.release_url == SPINE_RELEASE_URL


@pytest.mark.asyncio
async def test_track_on_compilation_threads_pg_and_writes_cache(
    library_db, enable_nonlibrary_release
):
    """The #632 cache handle reaches ``_resolve_nonlibrary_release`` through the
    full ``perform_lookup`` -> ``build_strategies`` partial plumbing: a lookup
    with a pg writes the resolved release to the cache. A partial that dropped
    ``pg=discogs_cache_pg`` would leave the store empty and fail this."""
    svc = _base_discogs_mock()
    svc.search_releases_by_track = AsyncMock(return_value=_track_releases(_spine_release()))
    pg = _RecordingPg()

    response = await _run_track_on_compilation(library_db, svc, pg=pg)

    assert any(
        r.library_item.id == 0 and r.artwork is not None and r.artwork.release_id > 0
        for r in response.results
    )
    # The pg handle was threaded all the way down: the positive resolution landed
    # in the #632 cache (keyed on the typed artist + track).
    assert pg.execute_calls >= 1
    assert pg.store.get((SPINE_ARTIST.lower(), SPINE_TRACK.lower(), True)) == SPINE_RELEASE_ID


@pytest.mark.asyncio
async def test_track_on_compilation_surfaces_rowless_via_album_title_wave(
    library_db, enable_nonlibrary_release
):
    """The row-less carry-through must fire the album-title probe (#646).

    The #319/#237 shape: a non-library release whose per-track credit Discogs
    files oddly (trio collaboration / odd credit), so the artist-scoped track
    probes (``search_releases_by_track`` Wave A/B) surface *nothing* — the
    release is discoverable only by its album title. The in-library #604
    lazy-bind already fires the album-title wave (``also_probe_album_title``);
    the #628 carry-through did not, so this non-library release was dropped by
    ``process_release`` (no library row) *and* missed by the carry-through's
    track-only resolve.

    Threading ``also_probe_album_title=bool(album)`` into
    ``_resolve_nonlibrary_release``'s ``resolve_release_for_track`` call closes
    the gap. The fix lives in the shared helper, so SONG_AS_TRACK and SWAPPED
    inherit it; TRACK_ON_COMPILATION is the natural home for the trio shape
    (artist + album typed) and the call site #646 names.
    """
    svc = _base_discogs_mock()
    # Track-artist probe finds nothing — the odd-credit case.
    svc.search_releases_by_track = AsyncMock(return_value=_empty_track_releases())
    # The release is reachable only by album title.
    svc.search_releases_by_album_title = AsyncMock(return_value=_track_releases(_spine_release()))

    request = LookupRequest(
        artist=SPINE_ARTIST,
        song=SPINE_TRACK,
        album=SPINE_ALBUM,
        raw_message=f"{SPINE_ARTIST} - {SPINE_TRACK}",
    )
    response = await perform_lookup(
        request,
        library_db,
        svc,
        telemetry=make_lml_telemetry(),
        discogs_cache_pg=None,
    )

    # Discriminator: the track-artist probe is empty and the library gate drops
    # the strategy's own album candidate, so the only path to a row-less result
    # is the carry-through firing its *own* album-title wave inside
    # resolve_release_for_track. Pre-#646 this list is empty (the carry-through
    # resolved track-only). The unit test pins the resolver kwarg directly.
    rowless = [r for r in response.results if r.library_item.id == 0 and r.artwork is not None]
    assert len(rowless) == 1
    assert rowless[0].artwork.release_id == SPINE_RELEASE_ID
    assert rowless[0].artwork.release_id > 0
    assert rowless[0].artwork.release_url == SPINE_RELEASE_URL


# ---------------------------------------------------------------------------
# SONG_AS_TRACK
# ---------------------------------------------------------------------------


async def _run_song_as_track(library_db, svc, *, pg=None):
    """Drive perform_lookup down the SONG_AS_TRACK cut-site.

    Song-only (no artist) so SONG_AS_ARTIST runs first and finds nothing, then
    SONG_AS_TRACK cross-references the song as a track. The surfaced release is
    not in the library, so the kernel's library gate drops it.
    """
    request = LookupRequest(song=SPINE_TRACK, raw_message=SPINE_TRACK)
    return await perform_lookup(
        request,
        library_db,
        svc,
        telemetry=make_lml_telemetry(),
        discogs_cache_pg=pg,
    )


@pytest.mark.asyncio
async def test_song_as_track_surfaces_rowless_when_flag_on(library_db, enable_nonlibrary_release):
    svc = _base_discogs_mock()
    # SONG_AS_ARTIST: no releases by the song-as-artist name.
    svc.lookup_releases_by_artist = AsyncMock(return_value=[])
    # Song-only probe surfaces the spine release (its own artist credit anchors
    # the row-less resolve, since there is no typed artist here). The
    # ``artist=SPINE_ARTIST`` override is load-bearing post-LML#649: a *real*
    # release credit anchors and surfaces, whereas the realistic default
    # ``"Various"`` credit is suppressed (see
    # ``test_song_as_track_various_anchor_suppresses_rowless``).
    svc.search_releases_by_track = AsyncMock(
        return_value=_track_releases(_spine_release(artist=SPINE_ARTIST))
    )

    response = await _run_song_as_track(library_db, svc)

    assert len(response.results) == 1
    item = response.results[0]
    assert item.library_item.id == 0
    assert item.artwork is not None
    assert item.artwork.release_id == SPINE_RELEASE_ID
    assert item.artwork.release_url == SPINE_RELEASE_URL
    # A2 (LML#629): a song-only (no typed album) row-less result is soft. Pin it
    # here so a future reversion of the no-album confidence is caught on the
    # #628 SONG_AS_TRACK path, not only on the new #629 tests.
    assert item.artwork.confidence == ROWLESS_NO_ALBUM_CONFIDENCE
    # Pin routing: SONG_AS_TRACK (song-only, no typed artist), not SWAPPED.
    assert response.search_type == "compilation"


@pytest.mark.asyncio
async def test_song_as_track_flag_off_no_rowless(library_db):
    svc = _base_discogs_mock()
    svc.lookup_releases_by_artist = AsyncMock(return_value=[])
    svc.search_releases_by_track = AsyncMock(
        return_value=_track_releases(_spine_release(artist=SPINE_ARTIST))
    )

    response = await _run_song_as_track(library_db, svc)

    assert not any(
        r.library_item.id == 0 and r.artwork is not None and r.artwork.release_id > 0
        for r in response.results
    )


@pytest.mark.asyncio
async def test_song_as_track_various_anchor_suppresses_rowless(
    library_db, enable_nonlibrary_release
):
    """LML#649: a song-only V/A-comp miss whose only artist signal is the
    release-level "Various" credit must NOT surface row-less, even flag-on.

    The anchor falls back to the surfaced release's own credit, which is the
    compilation marker "Various" for a real V/A comp (the case the carry-through
    targets). That is not a usable per-track artist: resolving under it validates
    only against the release-level "Various" credit (blind to the track's actual
    performer) and would key the #632 cache on ``("various", <title>, True)``,
    colliding same-titled tracks across different comps. Until the per-track
    credit is plumbed through, suppress the carry-through rather than surface an
    imprecise, collision-prone item.

    The sibling ``..._surfaces_rowless_when_flag_on`` overrides
    ``artist=SPINE_ARTIST`` — a real release credit — and still surfaces; this
    pins the realistic default-``Various`` case that override hid.
    """
    svc = _base_discogs_mock()
    svc.lookup_releases_by_artist = AsyncMock(return_value=[])
    # Default _spine_release() credits the comp to "Various" — the realistic V/A
    # shape, not the artist=SPINE_ARTIST the masking test substitutes.
    svc.search_releases_by_track = AsyncMock(return_value=_track_releases(_spine_release()))

    response = await _run_song_as_track(library_db, svc)

    assert not any(
        r.library_item.id == 0 and r.artwork is not None and r.artwork.release_id > 0
        for r in response.results
    )


@pytest.mark.asyncio
async def test_song_as_track_various_anchor_never_writes_632_cache(
    library_db, enable_nonlibrary_release
):
    """LML#649 AC3: the song-only V/A path must never write the #632 cache under
    a "Various" anchor — that ``("various", <title>, True)`` key is what collides
    distinct same-titled tracks across different comps. Suppressing the
    carry-through means no resolve, hence no cache write at all.

    Contrast ``test_track_on_compilation_threads_pg_and_writes_cache``, where a
    *typed* artist anchor legitimately writes ``(SPINE_ARTIST, SPINE_TRACK)``.
    """
    svc = _base_discogs_mock()
    svc.lookup_releases_by_artist = AsyncMock(return_value=[])
    svc.search_releases_by_track = AsyncMock(return_value=_track_releases(_spine_release()))
    pg = _RecordingPg()

    await _run_song_as_track(library_db, svc, pg=pg)

    # No row was written under any key — in particular nothing under "various".
    assert pg.execute_calls == 0
    assert pg.store == {}


# ---------------------------------------------------------------------------
# SWAPPED_INTERPRETATION
# ---------------------------------------------------------------------------


# The SWAPPED track side: a word ("Tunes") that co-occurs in Stereolab's seeded
# catalog ("Aluminum Tunes"), so the combined "Stereolab Tunes" FTS query
# surfaces the Stereolab row (results1) while "Tunes" as an *artist* matches
# nothing (results2 empty) — the unambiguous single-artist branch that narrows
# via the track cross-reference. Typed song/artist are left unset so neither
# ARTIST_PLUS_ALBUM (which would surface Stereolab rows and pre-empt SWAPPED's
# ``not state.results`` gate) nor TRACK_ON_COMPILATION / SONG_AS_TRACK (both
# need ``parsed.song``) can fire — SWAPPED is the only strategy in play.
SWAPPED_ARTIST = "Stereolab"
SWAPPED_TRACK = "Tunes"


async def _run_swapped(library_db, svc, *, pg=None):
    """Drive perform_lookup down the SWAPPED_INTERPRETATION cut-site.

    ``"Stereolab - Tunes"`` is an ambiguous X - Y. "Stereolab" matches the seeded
    library artist (results1), "Tunes" as an artist matches nothing (results2
    empty), so the kernel narrows by cross-referencing "Tunes" as a track against
    Discogs — but the release holding it is a comp not shelved in the library.
    """
    request = LookupRequest(raw_message=f"{SWAPPED_ARTIST} - {SWAPPED_TRACK}")
    return await perform_lookup(
        request,
        library_db,
        svc,
        telemetry=make_lml_telemetry(),
        discogs_cache_pg=pg,
    )


def _swapped_track_probe(track: str, artist: str | None = None, **_kw) -> TrackReleasesResponse:
    """Return the spine release only for the identified-artist track probe.

    Scopes the surfaced release to the ``("Tunes", "Stereolab")`` probe so the
    mock doesn't bleed the V/A comp into any other (artist, track) pair.
    """
    if track == SWAPPED_TRACK and artist == SWAPPED_ARTIST:
        return _track_releases(_spine_release(artist=SWAPPED_ARTIST, album="Some Comp"))
    return _empty_track_releases()


@pytest.mark.asyncio
async def test_swapped_interpretation_surfaces_rowless_when_flag_on(
    library_db, enable_nonlibrary_release
):
    svc = _base_discogs_mock()
    # The identified artist (Stereolab) probes the track; the release is a comp
    # not shelved in the library. Artist-scoped so it can't reach another path.
    svc.search_releases_by_track = AsyncMock(side_effect=_swapped_track_probe)

    response = await _run_swapped(library_db, svc)

    rowless = [r for r in response.results if r.library_item.id == 0 and r.artwork is not None]
    assert len(rowless) == 1
    assert rowless[0].artwork.release_id == SPINE_RELEASE_ID
    assert rowless[0].artwork.release_id > 0
    assert rowless[0].artwork.release_url == SPINE_RELEASE_URL
    # Pin routing: SWAPPED_INTERPRETATION reports search_type "alternative".
    # This is the discriminator that catches the exact bug already hit once
    # (a SWAPPED case silently served by TRACK_ON_COMPILATION).
    assert response.search_type == "alternative"


@pytest.mark.asyncio
async def test_swapped_interpretation_flag_off_no_rowless(library_db):
    svc = _base_discogs_mock()
    svc.search_releases_by_track = AsyncMock(side_effect=_swapped_track_probe)

    response = await _run_swapped(library_db, svc)

    assert not any(
        r.library_item.id == 0 and r.artwork is not None and r.artwork.release_id > 0
        for r in response.results
    )
