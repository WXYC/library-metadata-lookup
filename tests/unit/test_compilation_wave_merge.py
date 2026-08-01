"""Unit tests for the Wave A / Wave B merge gate in ``search_compilations_for_track``.

See WXYC/library-metadata-lookup#527. Two Discogs probes run in parallel inside
``search_compilations_for_track``:

- Wave A — ``search_releases_by_track(track, artist)`` with the artist as a
  Discogs field filter. Returns releases credited to the artist directly
  (singles, artist's own albums, single-artist retrospectives).
- Wave B — ``search_releases_by_track(track, artist, artist_as_keyword=True)``
  with ``q=artist`` + ``format=Compilation``. Designed to surface multi-artist
  V/A compilations that credit the artist at track level rather than release
  level.

Wave B's results are merged into the candidate pool only when Wave A has not
already produced a compilation hit (the existing ``seen_album_keys`` dedup
guards against double-counting). The bug fixed here is that the original gate
keyed on ``r.is_compilation`` — Discogs flags single-artist retrospectives
``format=Compilation`` too, so a Wave A hit like *Vivien Goldman — Resolutionary*
silenced the entire Wave B merge and the catalogued V/A *Disco Not Disco* never
made it into the library probe.

The fix narrows the gate to V/A specifically via ``is_compilation_artist`` on
the release's artist string; artist-comps no longer suppress Wave B.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from config.settings import get_settings
from discogs.models import ReleaseInfo, TrackReleasesResponse
from entity.compilation_track_location import CompilationTrackLocationRow
from lookup.location_union import ResolvedLocation
from lookup.strategies.track_on_compilation import search_compilations_for_track
from services.parser import ParsedRequest
from tests.factories import make_library_item


def _make_release(
    *,
    album: str,
    artist: str,
    release_id: int,
    is_compilation: bool,
) -> ReleaseInfo:
    return ReleaseInfo(
        album=album,
        artist=artist,
        release_id=release_id,
        release_url=f"https://www.discogs.com/release/{release_id}",
        is_compilation=is_compilation,
    )


def _make_discogs_service(
    *,
    wave_a: list[ReleaseInfo],
    wave_b: list[ReleaseInfo],
) -> AsyncMock:
    """Stub DiscogsService that returns ``wave_a`` for the artist-field probe and
    ``wave_b`` for the artist-as-keyword probe."""

    service = AsyncMock()
    service.cache_service = None

    async def _track_releases(track, artist=None, artist_as_keyword=False, **_):
        releases = wave_b if artist_as_keyword else wave_a
        return TrackReleasesResponse(
            track=track,
            artist=artist,
            releases=list(releases),
            total=len(releases),
        )

    service.search_releases_by_track = AsyncMock(side_effect=_track_releases)
    service.validate_track_on_release = AsyncMock(return_value=True)
    return service


class TestWaveMergeGate:
    """The Wave B merge gate must trigger off V/A-ness, not Discogs's
    ``format=Compilation`` flag."""

    @pytest.mark.asyncio
    async def test_artist_comp_in_wave_a_does_not_suppress_va_in_wave_b(self):
        """Repro of WXYC/library-metadata-lookup#527.

        Wave A returns *Resolutionary* — a Vivien Goldman retrospective that
        Discogs flags ``format=Compilation`` — and Wave B returns the catalogued
        V/A *Disco Not Disco*. Both must surface in the candidate pool so that
        the library probe finds both library entries.
        """
        resolutionary_item = make_library_item(
            id=65701,
            artist="Vivien Goldman",
            title="Resolutionary",
            call_letters="G",
        )
        disco_not_disco_item = make_library_item(
            id=58610,
            artist="Various Artists",
            title="Disco Not Disco",
            call_letters="V",
        )

        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])

        # db.search call order inside search_compilations_for_track:
        # 1. keyword pre-pass ("vivien goldman launderette") → empty
        # 2. process_release for Resolutionary → search_album_fuzzy hits library
        # 3. process_release for Disco Not Disco → search_album_fuzzy hits library
        # Order of (2) and (3) is determined by asyncio.gather; both must resolve
        # to their respective library rows regardless of which lands first.
        async def _search(query, limit=None, **_):
            q = query.lower()
            if "resolutionary" in q:
                return [resolutionary_item]
            if "disco not disco" in q:
                return [disco_not_disco_item]
            return []

        db.search = AsyncMock(side_effect=_search)

        service = _make_discogs_service(
            wave_a=[
                _make_release(
                    album="Resolutionary",
                    artist="Vivien Goldman",
                    release_id=8205159,
                    is_compilation=True,
                ),
            ],
            wave_b=[
                _make_release(
                    album="Resolutionary",
                    artist="Vivien Goldman",
                    release_id=8205159,
                    is_compilation=True,
                ),
                _make_release(
                    album="Disco Not Disco",
                    artist="Various",
                    release_id=1210707,
                    is_compilation=True,
                ),
            ],
        )

        parsed = ParsedRequest(
            artist="Vivien Goldman",
            song="Launderette",
            raw_message="launderette by vivien goldman",
        )

        with patch(
            "lookup.strategies.track_on_compilation.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            items, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        titles = {item.title for item in items}
        assert "Resolutionary" in titles, (
            f"Artist-comp Resolutionary should still surface, got: {titles}"
        )
        assert "Disco Not Disco" in titles, (
            f"V/A comp Disco Not Disco should also surface (bug #527 regression), got: {titles}"
        )

    @pytest.mark.asyncio
    async def test_va_only_in_wave_b_still_surfaces(self):
        """Wave A empty + Wave B has a V/A comp → V/A comp surfaces.

        This is the canonical "VA-only" path that ``artist_as_keyword=True`` was
        introduced for; guards against regressing the merge entirely.
        """
        disco_not_disco_item = make_library_item(
            id=58610,
            artist="Various Artists",
            title="Disco Not Disco",
            call_letters="V",
        )

        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])

        async def _search(query, limit=None, **_):
            if "disco not disco" in query.lower():
                return [disco_not_disco_item]
            return []

        db.search = AsyncMock(side_effect=_search)

        service = _make_discogs_service(
            wave_a=[],
            wave_b=[
                _make_release(
                    album="Disco Not Disco",
                    artist="Various",
                    release_id=1210707,
                    is_compilation=True,
                ),
            ],
        )

        parsed = ParsedRequest(
            artist="Vivien Goldman",
            song="Launderette",
            raw_message="launderette by vivien goldman",
        )

        with patch(
            "lookup.strategies.track_on_compilation.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            items, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        titles = {item.title for item in items}
        assert titles == {"Disco Not Disco"}, (
            f"Wave B's V/A hit must surface when Wave A is empty, got: {titles}"
        )

    @pytest.mark.asyncio
    async def test_artist_comp_only_still_surfaces_when_wave_b_empty(self):
        """Wave A returns an artist-comp, Wave B is empty → artist-comp surfaces.

        After the fix, ``has_va_compilation`` is False for an artist-comp, so the
        Wave B merge attempts to run but its empty input is a no-op. The artist-
        comp must still flow through ``process_release`` and surface in results.
        """
        resolutionary_item = make_library_item(
            id=65701,
            artist="Vivien Goldman",
            title="Resolutionary",
            call_letters="G",
        )

        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])

        async def _search(query, limit=None, **_):
            if "resolutionary" in query.lower():
                return [resolutionary_item]
            return []

        db.search = AsyncMock(side_effect=_search)

        service = _make_discogs_service(
            wave_a=[
                _make_release(
                    album="Resolutionary",
                    artist="Vivien Goldman",
                    release_id=8205159,
                    is_compilation=True,
                ),
            ],
            wave_b=[],
        )

        parsed = ParsedRequest(
            artist="Vivien Goldman",
            song="Launderette",
            raw_message="launderette by vivien goldman",
        )

        with patch(
            "lookup.strategies.track_on_compilation.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            items, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        titles = {item.title for item in items}
        assert titles == {"Resolutionary"}, (
            f"Artist-comp Wave A hit must surface when Wave B is empty, got: {titles}"
        )

    @pytest.mark.asyncio
    async def test_va_release_in_both_probes_is_not_double_processed(self):
        """Same V/A release in Wave A and Wave B → processed once.

        When Wave A already surfaces a V/A comp, ``has_va_compilation`` is True
        and the Wave B merge is suppressed entirely. The V/A release flows
        through ``process_release`` once, so the Discogs track-validation API
        call (the per-release cost lever) fires once per unique V/A candidate.
        """
        disco_not_disco_item = make_library_item(
            id=58610,
            artist="Various Artists",
            title="Disco Not Disco",
            call_letters="V",
        )

        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])

        async def _search(query, limit=None, **_):
            if "disco not disco" in query.lower():
                return [disco_not_disco_item]
            return []

        db.search = AsyncMock(side_effect=_search)

        va_release = _make_release(
            album="Disco Not Disco",
            artist="Various",
            release_id=1210707,
            is_compilation=True,
        )
        service = _make_discogs_service(wave_a=[va_release], wave_b=[va_release])

        parsed = ParsedRequest(
            artist="Vivien Goldman",
            song="Launderette",
            raw_message="launderette by vivien goldman",
        )

        with patch(
            "lookup.strategies.track_on_compilation.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            items, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        assert [item.title for item in items] == ["Disco Not Disco"]
        assert service.validate_track_on_release.await_count == 1, (
            "Same V/A release in both probes should be validated against Discogs once, "
            f"got {service.validate_track_on_release.await_count} calls"
        )


@pytest.fixture
def _nonlibrary_release_on(monkeypatch):
    """Enable the LML#628 rowless-carve flag for the LML#1026 tests below,
    with the cache_clear-on-both-sides shape the sibling flag fixtures use
    (cf. tests/unit/test_track_on_compilation_identity_bridge.py)."""
    monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _union_task(result: list[ResolvedLocation] | Exception) -> asyncio.Task:
    """Build a real asyncio.Task standing in for the orchestrator's
    ``location_union_task`` (LML#1026): resolves to ``result``, or raises it
    when given an exception instance."""

    async def _resolve() -> list[ResolvedLocation]:
        if isinstance(result, Exception):
            raise result
        return result

    return asyncio.create_task(_resolve())


def _lit_location() -> ResolvedLocation:
    """The LML#271 live-repro shelf location: LiT soundtrack, id 60654."""
    return ResolvedLocation(
        row=CompilationTrackLocationRow(
            library_id=60654,
            track_position="A3",
            track_artist="brian reitzell",
            track_title="ikebana",
            credit_role="primary",
            discogs_release_id=12345,
            artwork_url="https://example.com/lit.jpg",
        ),
        shelf_item=make_library_item(
            id=60654,
            artist="Soundtracks - L",
            title="Lost in Translation",
            call_letters="S",
        ),
    )


class TestRecallIndexAuthoritative:
    """LML#1026: when the orchestrator's location-union task is threaded in,
    the recall index is the single source for comp-track resolution -- the
    live-Discogs comp-discovery pass (Wave A/B + per-release validates) never
    fires. The legacy pass survives untouched only when no task is supplied
    (kill switch off / low-priority caller / no song), which the pre-existing
    tests in this module pin."""

    @pytest.mark.asyncio
    async def test_index_hit_returns_nothing_and_skips_all_live_work(self, _nonlibrary_release_on):
        """An index hit proves the track IS in the library, so the strategy
        contributes nothing: the fold appends the shelf location(s), and no
        keyword search, live probe, album fallback, or rowless carve runs --
        a rowless 'not in your library' row would be wrong and would preempt
        the shelf location as primary."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])
        db.exact_title = AsyncMock(return_value=[])
        service = _make_discogs_service(
            wave_a=[
                _make_release(
                    album="Lost In Translation (Wrong Live Hit)",
                    artist="Various",
                    release_id=99999,
                    is_compilation=True,
                )
            ],
            wave_b=[],
        )
        parsed = ParsedRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="ikebana by brian reitzell",
        )

        with patch(
            "lookup.strategies.track_on_compilation._resolve_nonlibrary_release",
            new_callable=AsyncMock,
        ) as rowless:
            items, titles = await search_compilations_for_track(
                db,
                parsed,
                discogs_service=service,
                location_union_task=_union_task([_lit_location()]),
            )

        assert items == []
        assert titles == {}
        service.search_releases_by_track.assert_not_awaited()
        service.search_releases_by_album_title.assert_not_awaited()
        db.search.assert_not_awaited()
        rowless.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_index_miss_suppresses_comp_discovery_but_keeps_album_fallback_and_keyword(
        self,
    ):
        """An index miss is authoritative for comps ('removed, not augmented'
        -- the #271-locked decision): Wave A/B never fire. The co-located
        non-comp features survive: the #319 album-title probe still fires when
        an album is typed, and the local keyword last-resort still surfaces
        its hit."""
        keyword_item = make_library_item(
            id=777,
            artist="Brian Reitzell",
            title="Sofia's Playlist",
            call_letters="R",
        )
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[keyword_item])
        service = _make_discogs_service(
            wave_a=[
                _make_release(
                    album="Should Never Be Fetched",
                    artist="Various",
                    release_id=88888,
                    is_compilation=True,
                )
            ],
            wave_b=[],
        )
        service.search_releases_by_album_title = AsyncMock(
            return_value=TrackReleasesResponse(track="Ikebana", artist=None, releases=[], total=0)
        )
        parsed = ParsedRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            album="Lost In Translation",
            raw_message="ikebana by brian reitzell from lost in translation",
        )

        items, _titles = await search_compilations_for_track(
            db, parsed, discogs_service=service, location_union_task=_union_task([])
        )

        service.search_releases_by_track.assert_not_awaited()
        service.search_releases_by_album_title.assert_awaited_once()
        assert items == [keyword_item]

    @pytest.mark.asyncio
    async def test_index_miss_keeps_rowless_carve(self, _nonlibrary_release_on):
        """The #628 rowless non-library carry-through is resolution, not comp
        discovery -- an index miss must still reach it (the index cannot
        answer 'not in the library at all')."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])
        db.exact_title = AsyncMock(return_value=[])
        service = _make_discogs_service(wave_a=[], wave_b=[])
        parsed = ParsedRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="ikebana by brian reitzell",
        )

        with patch(
            "lookup.strategies.track_on_compilation._resolve_nonlibrary_release",
            new_callable=AsyncMock,
            return_value=None,
        ) as rowless:
            items, _titles = await search_compilations_for_track(
                db,
                parsed,
                discogs_service=service,
                location_union_task=_union_task([]),
            )

        rowless.assert_awaited_once()
        service.search_releases_by_track.assert_not_awaited()
        assert items == []

    @pytest.mark.asyncio
    async def test_degraded_union_task_restores_the_full_legacy_pass(self):
        """A failed probe task means the index COULD NOT be consulted -- and
        "union unavailable" must never read as "index says no" (a
        discogs-cache outage would otherwise silently zero comp-track
        recall). The strategy falls back to the full legacy live pass: Wave
        A/B fire exactly as if no task existed, and no exception escapes."""
        va_release = _make_release(
            album="Disco Not Disco",
            artist="Various",
            release_id=1210707,
            is_compilation=True,
        )
        disco_not_disco_item = make_library_item(
            id=58610,
            artist="Various Artists",
            title="Disco Not Disco",
            call_letters="V",
        )
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])

        async def _search(query, limit=None, **_):
            if "disco not disco" in query.lower():
                return [disco_not_disco_item]
            return []

        db.search = AsyncMock(side_effect=_search)
        service = _make_discogs_service(wave_a=[], wave_b=[va_release])
        parsed = ParsedRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="ikebana by brian reitzell",
        )

        items, _titles = await search_compilations_for_track(
            db,
            parsed,
            discogs_service=service,
            location_union_task=_union_task(RuntimeError("probe exploded")),
        )

        assert service.search_releases_by_track.await_count == 2, (
            "a degraded probe must restore the legacy Wave A/B live pass"
        )
        assert items == [disco_not_disco_item]

    @pytest.mark.asyncio
    async def test_all_unrenderable_locations_count_as_an_index_miss(self):
        """Index rows whose shelf join found no LibraryItem (stale ids) are
        NOT a hit: the fold would render none of them, so treating the raw
        rows as a hit would make the track vanish entirely. Nothing
        renderable -> authoritative miss -> the suppressed pass runs (keyword
        last-resort fires; Wave A/B stay off)."""
        keyword_item = make_library_item(
            id=777,
            artist="Brian Reitzell",
            title="Sofia's Playlist",
            call_letters="R",
        )
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[keyword_item])
        service = _make_discogs_service(wave_a=[], wave_b=[])
        parsed = ParsedRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="ikebana by brian reitzell",
        )
        stale = ResolvedLocation(row=_lit_location().row, shelf_item=None)

        items, _titles = await search_compilations_for_track(
            db, parsed, discogs_service=service, location_union_task=_union_task([stale])
        )

        service.search_releases_by_track.assert_not_awaited()
        assert items == [keyword_item]

    @pytest.mark.asyncio
    async def test_mixed_renderable_and_stale_locations_count_as_a_hit(self):
        """One renderable location among stale ones is a hit -- the fold has
        something real to supply, so the strategy contributes nothing."""
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[])
        service = _make_discogs_service(wave_a=[], wave_b=[])
        parsed = ParsedRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="ikebana by brian reitzell",
        )
        stale = ResolvedLocation(row=_lit_location().row, shelf_item=None)

        items, titles = await search_compilations_for_track(
            db,
            parsed,
            discogs_service=service,
            location_union_task=_union_task([stale, _lit_location()]),
        )

        assert (items, titles) == ([], {})
        service.search_releases_by_track.assert_not_awaited()
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_strategy_cancellation_does_not_cancel_the_shared_union_task(self):
        """The strategy runner bounds each attempt with asyncio.wait_for, and
        Task.cancel delegates into whatever the attempt is awaiting --
        unshielded, a per-strategy budget cancellation would cancel the
        SHARED union task and the orchestrator's later fold await would leak
        CancelledError past its `except Exception`. The shield keeps the
        union task alive: cancellation aborts the attempt, not the probe."""
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[])
        service = _make_discogs_service(wave_a=[], wave_b=[])
        parsed = ParsedRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="ikebana by brian reitzell",
        )

        started = asyncio.Event()

        async def _pending_forever() -> list[ResolvedLocation]:
            started.set()
            await asyncio.Event().wait()
            return []

        union_task = asyncio.create_task(_pending_forever())
        attempt = asyncio.create_task(
            search_compilations_for_track(
                db, parsed, discogs_service=service, location_union_task=union_task
            )
        )
        await started.wait()
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt

        # The shared task must survive the attempt's cancellation so the
        # orchestrator's fold can still consume it.
        await asyncio.sleep(0)
        assert not union_task.cancelled()
        assert not union_task.done()
        union_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await union_task
