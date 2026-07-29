"""LML#971 — bridge a library row to the resolved release's artist name-variations.

Repro of ``lookup "in the blink of an eye by c spencer yeh"`` returning no library
results even though WXYC owns the 7-inch. On the production cache path the Discogs
release (2789357) resolves under its *canonical* credit "C. Spencer Yeh", and the
library row (57833) is title-matched — but the row is filed under the band name
"Burning Star Core" with ``alternate_artist_name`` "C.S. Yeh", so the strict
``artist_matches_item`` prefix filter drops it (and it isn't a V/A compilation, so
the compilation carve-out can't rescue it). The bridge admits the row because
"C.S. Yeh" is a known name-variation of that release's artist entity; exact
normalized membership (not a prefix) keeps a bare variation like "Yeh" from
admitting unrelated rows, and ``validate_release_for_track`` (typed artist) stays
the safety belt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import ReleaseInfo, TrackReleasesResponse
from lookup.strategies.track_on_compilation import search_compilations_for_track
from services.parser import ParsedRequest
from tests.factories import make_library_item

_RELEASE_ID = 2789357
_ALBUM = "In The Blink Of An Eye"
_ROW_ID = 57833

# The Discogs artist entity's name-variation set (canonical + variations), as
# ``get_release_artist_variations`` returns it for release 2789357.
_VARIATIONS = {"C. Spencer Yeh", "C.S. Yeh", "CS Yeh", "Spencer Yeh", "Yeh"}


def _row() -> object:
    """The WXYC library row: filed under the band name, aliased "C.S. Yeh"."""
    return make_library_item(
        id=_ROW_ID,
        artist="Burning Star Core",
        title='"In The Blink of an Eye" 7-inch',
        alternate_artist_name="C.S. Yeh",
        format='vinyl - 7"',
    )


def _service(variations: set[str]) -> AsyncMock:
    service = AsyncMock()
    service.cache_service = AsyncMock()
    service.search_releases_by_track = AsyncMock(
        return_value=TrackReleasesResponse(
            track="In the Blink of an Eye",
            artist="C. Spencer Yeh",
            releases=[
                ReleaseInfo(
                    album=_ALBUM,
                    artist="C. Spencer Yeh",  # canonical credit (cache path) — no ANV
                    release_id=_RELEASE_ID,
                    release_url=f"https://www.discogs.com/release/{_RELEASE_ID}",
                    is_compilation=False,
                )
            ],
            total=1,
        )
    )
    service.get_release_artist_variations = AsyncMock(return_value=variations)
    return service


@pytest.fixture
def _bridge_on(monkeypatch):
    """Enable the LML#971 flag (default off) and pin the sibling resolve flags off
    so the test isolates the identity bridge."""
    monkeypatch.setenv("LML_RESOLVE_ARTIST_VARIATIONS", "true")
    monkeypatch.setenv("LML_RESOLVE_ARTIST_CANONICAL", "false")
    monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "false")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _bridge_off(monkeypatch):
    """The default: LML#971 flag off (plus sibling resolve flags off)."""
    monkeypatch.setenv("LML_RESOLVE_ARTIST_VARIATIONS", "false")
    monkeypatch.setenv("LML_RESOLVE_ARTIST_CANONICAL", "false")
    monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "false")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _parsed() -> ParsedRequest:
    return ParsedRequest(
        artist="C. Spencer Yeh",
        song="In the Blink of an Eye",
        raw_message="in the blink of an eye by c spencer yeh",
    )


@pytest.mark.asyncio
class TestIdentityBridge:
    async def test_variation_match_surfaces_the_row(self, _bridge_on):
        """Row 57833 surfaces via the name-variation bridge, and the typed-artist
        validation still runs (the bridge is not a validation bypass)."""
        service = _service(_VARIATIONS)
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        with (
            patch(
                "lookup.strategies.track_on_compilation.search_album_fuzzy",
                new_callable=AsyncMock,
                return_value=[_row()],
            ),
            patch(
                "lookup.strategies.track_on_compilation.validate_release_for_track",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_validate,
        ):
            results, _titles = await search_compilations_for_track(
                db, _parsed(), discogs_service=service
            )

        assert {r.id for r in results} == {_ROW_ID}
        service.get_release_artist_variations.assert_awaited_with(_RELEASE_ID)
        mock_validate.assert_awaited()  # safety belt still runs on the typed artist

    async def test_bare_variation_does_not_prefix_match_unrelated_row(self, _bridge_on):
        """A bare variation ("Yeh") must NOT admit an unrelated longer row via a
        prefix — the bridge requires EXACT normalized membership."""
        service = _service({"Yeh"})
        unrelated = make_library_item(
            id=99001, artist="Yellow Magic Orchestra", title=_ALBUM, format="vinyl"
        )
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        with (
            patch(
                "lookup.strategies.track_on_compilation.search_album_fuzzy",
                new_callable=AsyncMock,
                return_value=[unrelated],
            ),
            patch(
                "lookup.strategies.track_on_compilation.validate_release_for_track",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            results, _titles = await search_compilations_for_track(
                db, _parsed(), discogs_service=service
            )

        assert results == []

    async def test_empty_variation_set_does_not_bind(self, _bridge_on):
        """When the variation lookup yields nothing (cache miss / degrade), the
        row is not admitted — the bridge fails safe."""
        service = _service(set())
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        with (
            patch(
                "lookup.strategies.track_on_compilation.search_album_fuzzy",
                new_callable=AsyncMock,
                return_value=[_row()],
            ),
            patch(
                "lookup.strategies.track_on_compilation.validate_release_for_track",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            results, _titles = await search_compilations_for_track(
                db, _parsed(), discogs_service=service
            )

        assert results == []


@pytest.mark.asyncio
class TestServiceWrapperDegrades:
    """DiscogsService.get_release_artist_variations is best-effort: it must never
    raise (a lookup can't fail over this bridge)."""

    async def test_no_cache_returns_empty(self):
        from discogs.service import DiscogsService

        svc = DiscogsService("dummy-token")
        assert svc.cache_service is None
        assert await svc.get_release_artist_variations(_RELEASE_ID) == set()

    async def test_degrades_on_cache_unavailable(self):
        from discogs.cache_service import CacheUnavailableError
        from discogs.service import DiscogsService

        svc = DiscogsService("dummy-token")
        svc.cache_service = AsyncMock()
        svc.cache_service.get_release_artist_variations = AsyncMock(
            side_effect=CacheUnavailableError("cache down")
        )
        assert await svc.get_release_artist_variations(_RELEASE_ID) == set()

    async def test_passes_through_variation_set(self):
        from discogs.service import DiscogsService

        svc = DiscogsService("dummy-token")
        svc.cache_service = AsyncMock()
        svc.cache_service.get_release_artist_variations = AsyncMock(
            return_value={"C.S. Yeh", "C. Spencer Yeh"}
        )
        assert await svc.get_release_artist_variations(_RELEASE_ID) == {
            "C.S. Yeh",
            "C. Spencer Yeh",
        }


@pytest.mark.asyncio
class TestBridgeGating:
    """Flag gate (default off), non-compilation scope (LML#971 review finding), and
    laziness of the bridge's variation query."""

    async def test_disabled_by_default(self, _bridge_off):
        """With the flag off (its default), the bridge never fires -- the row is
        dropped and no variation query is issued."""
        service = _service(_VARIATIONS)
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        with (
            patch(
                "lookup.strategies.track_on_compilation.search_album_fuzzy",
                new_callable=AsyncMock,
                return_value=[_row()],
            ),
            patch(
                "lookup.strategies.track_on_compilation.validate_release_for_track",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            results, _titles = await search_compilations_for_track(
                db, _parsed(), discogs_service=service
            )

        assert results == []
        service.get_release_artist_variations.assert_not_awaited()

    async def test_compilation_release_does_not_use_bridge(self, _bridge_on):
        """A V/A compilation release must NOT admit a non-"Various"-spelled row
        (e.g. filed "VA") via the bridge: the bridge is scoped to NON-compilation
        releases, so a "Various" ANV can't bypass the compilation title-score floor.
        Would surface the wrong row if the branch weren't compilation-gated."""
        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="In the Blink of an Eye",
                artist="C. Spencer Yeh",
                releases=[
                    ReleaseInfo(
                        album=_ALBUM,
                        artist="Various",
                        release_id=_RELEASE_ID,
                        release_url=f"https://www.discogs.com/release/{_RELEASE_ID}",
                        is_compilation=True,
                    )
                ],
                total=1,
            )
        )
        # If the branch weren't compilation-gated, this "Various" ANV set would
        # admit the "VA"-filed row (is_compilation_artist("VA") is False, so the
        # compilation carve-out with its >=80 title floor never fires either).
        service.get_release_artist_variations = AsyncMock(return_value={"Various", "VA", "V. A."})
        va_row = make_library_item(
            id=99002, artist="VA", title="A Totally Different Comp", format="vinyl"
        )
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        with (
            patch(
                "lookup.strategies.track_on_compilation.search_album_fuzzy",
                new_callable=AsyncMock,
                return_value=[va_row],
            ),
            patch(
                "lookup.strategies.track_on_compilation.validate_release_for_track",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            results, _titles = await search_compilations_for_track(
                db, _parsed(), discogs_service=service
            )

        assert results == []
        service.get_release_artist_variations.assert_not_awaited()

    async def test_no_query_when_typed_artist_matches(self, _bridge_on):
        """Lazy: when the library row matches the typed artist directly, the
        bridge's variation query is never issued (no wasted PG round-trip)."""
        service = _service(_VARIATIONS)
        matching_row = make_library_item(
            id=57834, artist="C. Spencer Yeh", title=_ALBUM, format="vinyl"
        )
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        with (
            patch(
                "lookup.strategies.track_on_compilation.search_album_fuzzy",
                new_callable=AsyncMock,
                return_value=[matching_row],
            ),
            patch(
                "lookup.strategies.track_on_compilation.validate_release_for_track",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            results, _titles = await search_compilations_for_track(
                db, _parsed(), discogs_service=service
            )

        assert {r.id for r in results} == {57834}
        service.get_release_artist_variations.assert_not_awaited()
