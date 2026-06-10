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

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import ReleaseInfo, TrackReleasesResponse
from lookup.orchestrator import search_compilations_for_track
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
            "lookup.orchestrator.lookup_releases_by_track",
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
            "lookup.orchestrator.lookup_releases_by_track",
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
            "lookup.orchestrator.lookup_releases_by_track",
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
            "lookup.orchestrator.lookup_releases_by_track",
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
