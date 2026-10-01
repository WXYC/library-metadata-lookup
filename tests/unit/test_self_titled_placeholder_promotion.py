"""Unit + perform_lookup-level tests for LML#1405.

A typed self-titled placeholder ("Epon.", "S/T") that names no shelved record
runs step 2's song->album Discogs lookup (LML#1392/#1394). That lookup leads
with whatever album Discogs happens to return first, so a row titled the
artist's own name never gets promoted unless Discogs itself names it. This
file covers the promotion ``search_library_with_fallback`` now applies: the
artist-named row, found via the same artist-only query the literal-title
guard already issues, always leads -- independent of what Discogs returned.

Typed album equal to the artist's name (not a placeholder) is a different
lane (LML#1412) and must stay untouched; see
``test_typed_album_equal_to_artist_name_is_untouched`` below.
"""

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import DiscogsSearchResponse
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from lookup.strategies.artist_plus_album import search_library_with_fallback
from services.parser import MessageType, ParsedRequest
from tests.conftest import make_lml_telemetry
from tests.factories import make_library_item

SELF_TITLED_ROW = make_library_item(id=1, artist="Jessica Pratt", title="Jessica Pratt")
SIBLING_II = make_library_item(id=2, artist="Jessica Pratt", title="Jessica Pratt II")
SIBLING_III = make_library_item(id=3, artist="Jessica Pratt", title="Jessica Pratt III")
OTHER_ALBUM = make_library_item(id=4, artist="Jessica Pratt", title="Quiet Signs")

SHELF = [SELF_TITLED_ROW, SIBLING_II, SIBLING_III, OTHER_ALBUM]


def _parsed(**overrides):
    defaults = {
        "artist": "Jessica Pratt",
        "album": "Epon.",
        "song": "Back, Baby",
        "message_type": MessageType.REQUEST,
        "is_request": True,
    }
    defaults.update(overrides)
    return ParsedRequest(**defaults)


class TestSelfTitledPlaceholderPromotion:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "placeholder",
        ["Epon.", "S/T", "s.t.", "eponymous"],
    )
    async def test_artist_named_row_leads_other_discogs_album(self, placeholder):
        """Discogs returns only the unrelated album; the artist-named row still
        leads, and the numbered siblings are absent."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=SHELF)
        parsed = _parsed(album=placeholder)

        results, fallback_used = await search_library_with_fallback(
            db, parsed, [placeholder, "Quiet Signs"]
        )

        assert [r.id for r in results] == [1, 4]
        assert fallback_used is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("placeholder", ["Epon.", "S/T"])
    async def test_artist_named_row_alone_when_discogs_finds_nothing(self, placeholder):
        db = AsyncMock()
        db.search = AsyncMock(return_value=SHELF)
        parsed = _parsed(album=placeholder)

        results, fallback_used = await search_library_with_fallback(db, parsed, [placeholder])

        assert [r.id for r in results] == [1]
        assert fallback_used is False

    @pytest.mark.asyncio
    async def test_literal_title_guard_shape_is_untouched(self):
        """A catalog row literally titled 'Eponymous' satisfies the LML#1392
        literal-title guard, so ``runs_album_resolution`` is False and the new
        promotion path is never entered -- the only artist-only query is
        ``runs_album_resolution``'s own literal-title check, not a second one
        from the new branch."""
        literal = make_library_item(id=9, artist="Jessica Pratt", title="Eponymous")
        db = AsyncMock()
        db.search = AsyncMock(return_value=[literal, OTHER_ALBUM])
        parsed = _parsed(album="Eponymous")

        results, fallback_used = await search_library_with_fallback(db, parsed, ["Eponymous"])

        assert [r.id for r in results] == [9]
        assert fallback_used is False
        assert db.search.await_count == 2

    @pytest.mark.asyncio
    async def test_catalog_st_row_shape_is_untouched(self):
        """A catalog row literally titled 'S/t' also satisfies the guard."""
        literal = make_library_item(id=10, artist="Jessica Pratt", title="S/t")
        db = AsyncMock()
        db.search = AsyncMock(return_value=[literal, OTHER_ALBUM])
        parsed = _parsed(album="S/T")

        results, fallback_used = await search_library_with_fallback(db, parsed, ["S/T"])

        assert [r.id for r in results] == [10]
        assert fallback_used is False
        assert db.search.await_count == 2

    @pytest.mark.asyncio
    async def test_no_artist_named_row_is_untouched(self):
        """The artist has no row titled its own name -- the promotion finds
        nothing to add, and the pre-existing album match stands alone."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[SIBLING_II, SIBLING_III, OTHER_ALBUM])
        parsed = _parsed(album="Epon.")

        results, fallback_used = await search_library_with_fallback(
            db, parsed, ["Epon.", "Quiet Signs"]
        )

        assert [r.id for r in results] == [4]
        assert fallback_used is False

    @pytest.mark.asyncio
    async def test_typed_album_equal_to_artist_name_is_untouched(self):
        """LML#1412: typed album literally equal to the artist's name is not a
        placeholder, so the guard never fires -- no artist-only query is
        issued beyond the normal album-combined search."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=SHELF)
        parsed = _parsed(album="Jessica Pratt")

        await search_library_with_fallback(db, parsed, ["Jessica Pratt"])

        queries = [call.kwargs.get("query") for call in db.search.await_args_list]
        assert queries == ["Jessica Pratt Jessica Pratt"]

    @pytest.mark.asyncio
    async def test_non_placeholder_album_is_untouched(self):
        """A normal typed album (not a placeholder, not equal to the artist)
        never enters the new branch at all."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[OTHER_ALBUM])
        parsed = _parsed(album="Quiet Signs")

        await search_library_with_fallback(db, parsed, ["Quiet Signs"])

        queries = [call.kwargs.get("query") for call in db.search.await_args_list]
        assert queries == ["Jessica Pratt Quiet Signs"]


class TestPerformLookupSelfTitledPlaceholderRepro:
    @pytest.fixture
    def telemetry(self):
        return make_lml_telemetry()

    @pytest.mark.asyncio
    async def test_artist_named_row_leads_the_response(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """The LML#1405 repro shape: a typed placeholder, Discogs resolving the
        song to an unrelated album, and the artist shelving a row titled its
        own name plus a numbered sibling. ``results[0]`` must be the
        artist-named row, not whatever Discogs returned first."""
        mock_library_db.find_similar_artist.return_value = None
        mock_library_db.search.return_value = SHELF
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Jessica Pratt",
            album="Epon.",
            song="Back, Baby",
            raw_message="Play Back, Baby by Jessica Pratt, Epon.",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Jessica Pratt", "Quiet Signs")],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert [item.library_item.id for item in response.results] == [1, 4]
        assert response.search_type != "fallback"
