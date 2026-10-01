"""Unit + perform_lookup-level tests for LML#1405.

A typed self-titled placeholder ("Epon.", "S/T") that names no shelved record
runs step 2's song->album Discogs lookup (LML#1392/#1394). That lookup leads
with whatever album Discogs happens to return first, so a row titled the
artist's own name never gets promoted unless Discogs itself names it. This
file covers the promotion ``search_library_with_fallback`` applies: when its
artist+album search already returned a row, the rows filed under exactly the
library artist and titled the artist's name move to the front.

Two limits are pinned here because each was a review finding on #1414:

- When the artist+album search returned nothing, nothing is promoted. The
  function returns what it returns on ``main``, so the step-3a library-miss
  probe still runs and binds the self-titled Discogs release.
- The promotion is not the last word. Step 3b validates every returned row's
  tracklist, and drops the promoted row when Discogs confirms the song on
  another returned album only.

Typed album equal to the artist's name (not a placeholder) is a different
lane (LML#1412) and must stay untouched; see
``test_typed_album_equal_to_artist_name_is_untouched`` below.
"""

import re
from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import DiscogsSearchRequest, DiscogsSearchResponse
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from lookup.strategies.artist_plus_album import search_library_with_fallback
from services.parser import MessageType, ParsedRequest
from tests.conftest import make_lml_telemetry
from tests.factories import make_discogs_result, make_library_item

SELF_TITLED_ROW = make_library_item(id=1, artist="Jessica Pratt", title="Jessica Pratt")
SIBLING_II = make_library_item(id=2, artist="Jessica Pratt", title="Jessica Pratt II")
SIBLING_III = make_library_item(id=3, artist="Jessica Pratt", title="Jessica Pratt III")
OTHER_ALBUM = make_library_item(id=4, artist="Jessica Pratt", title="Quiet Signs")

SHELF = [SELF_TITLED_ROW, SIBLING_II, SIBLING_III, OTHER_ALBUM]

PLACEHOLDERS = ["S/T", "s.t.", "self-titled", "self titled", "eponymous", "epon", "Epon."]


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


def _db(rows):
    db = AsyncMock()
    db.search = AsyncMock(return_value=rows)
    return db


class TestSelfTitledPlaceholderPromotion:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    async def test_artist_named_row_leads_other_discogs_album(self, placeholder):
        """Step 2 resolved the song to an unrelated shelved album; the
        artist-named row leads it, and the numbered siblings are absent."""
        parsed = _parsed(album=placeholder)

        results, fallback_used = await search_library_with_fallback(
            _db(SHELF), parsed, [placeholder, "Quiet Signs"]
        )

        assert [r.id for r in results] == [1, 4]
        assert fallback_used is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    async def test_no_promotion_when_album_fed_branch_finds_nothing(self, placeholder):
        """The blocking review finding on #1414. Discogs resolved no album for
        the song, so the album-fed branch returns no row. The artist-named row
        must NOT be promoted: the function returns what ``main`` returns
        (nothing, with the song-not-found flag), which is what leaves the
        step-3a library-miss probe open to bind the self-titled release."""
        parsed = _parsed(album=placeholder)

        results, fallback_used = await search_library_with_fallback(
            _db(SHELF), parsed, [placeholder]
        )

        assert results == []
        assert fallback_used is True

    @pytest.mark.asyncio
    async def test_literal_title_guard_shape_is_untouched(self):
        """A catalog row literally titled 'Eponymous' satisfies the LML#1392
        literal-title guard, so nothing is promoted -- even though the shelf
        also holds an artist-named row that the promotion would otherwise lead
        with. Fails if the ``runs_album_resolution`` conjunct is removed."""
        literal = make_library_item(id=9, artist="Jessica Pratt", title="Eponymous")
        parsed = _parsed(album="Eponymous")

        results, fallback_used = await search_library_with_fallback(
            _db([SELF_TITLED_ROW, literal, OTHER_ALBUM]), parsed, ["Eponymous"]
        )

        assert [r.id for r in results] == [9]
        assert fallback_used is False

    @pytest.mark.asyncio
    async def test_catalog_st_row_shape_is_untouched(self):
        """A catalog row literally titled 'S/t' also satisfies the guard; the
        artist-named row beside it is not promoted over it."""
        literal = make_library_item(id=10, artist="Jessica Pratt", title="S/t")
        parsed = _parsed(album="S/T")

        results, fallback_used = await search_library_with_fallback(
            _db([SELF_TITLED_ROW, literal, OTHER_ALBUM]), parsed, ["S/T"]
        )

        assert [r.id for r in results] == [10]
        assert fallback_used is False

    @pytest.mark.asyncio
    async def test_placeholder_without_song_is_untouched(self):
        """No song means step 2 never runs (``needs_album_resolution``), so the
        promotion must not either: the album-fed row stands alone."""
        literal = make_library_item(id=10, artist="Jessica Pratt", title="S/t")
        parsed = _parsed(album="S/T", song=None)

        results, fallback_used = await search_library_with_fallback(
            _db([SELF_TITLED_ROW, literal, OTHER_ALBUM]), parsed, ["S/T"]
        )

        assert [r.id for r in results] == [10]
        assert fallback_used is False

    @pytest.mark.asyncio
    async def test_no_artist_named_row_is_untouched(self):
        """The artist has no row titled its own name -- the promotion finds
        nothing to add, and the pre-existing album match stands alone."""
        parsed = _parsed(album="Epon.")

        results, fallback_used = await search_library_with_fallback(
            _db([SIBLING_II, SIBLING_III, OTHER_ALBUM]), parsed, ["Epon.", "Quiet Signs"]
        )

        assert [r.id for r in results] == [4]
        assert fallback_used is False

    @pytest.mark.asyncio
    async def test_prefix_artist_row_is_not_promoted(self):
        """Typed "Cat" prefix-matches rows filed under Cat Power, so the
        album-fed branch still returns Moon Pix as it does on ``main``. The
        Cat Power row titled "Cat" is a different artist's record and must not
        lead: the promotion requires exact normalized artist equality."""
        titled_cat = make_library_item(id=20, artist="Cat Power", title="Cat")
        moon_pix = make_library_item(id=21, artist="Cat Power", title="Moon Pix")
        parsed = _parsed(artist="Cat", album="S/T", song="Metal Heart")

        results, fallback_used = await search_library_with_fallback(
            _db([titled_cat, moon_pix]), parsed, ["S/T", "Moon Pix"]
        )

        assert [r.id for r in results] == [21]
        assert fallback_used is False

    @pytest.mark.asyncio
    async def test_other_artists_row_titled_the_artist_name_is_not_promoted(self):
        """A row titled "Jessica Pratt" filed under someone else (a tribute, a
        split) is not the artist's self-titled record."""
        tribute = make_library_item(id=30, artist="Cat Power", title="Jessica Pratt")
        parsed = _parsed(album="S/T")

        results, _ = await search_library_with_fallback(
            _db([tribute, OTHER_ALBUM]), parsed, ["S/T", "Quiet Signs"]
        )

        assert [r.id for r in results] == [4]

    @pytest.mark.asyncio
    async def test_title_matches_artist_across_punctuation(self):
        """The title/artist comparison is punctuation-folded: a record filed
        "Chuquimamani Condori" is the self-titled row of Chuquimamani-Condori."""
        self_titled = make_library_item(
            id=40, artist="Chuquimamani-Condori", title="Chuquimamani Condori"
        )
        edits = make_library_item(id=41, artist="Chuquimamani-Condori", title="Edits")
        parsed = _parsed(artist="Chuquimamani-Condori", album="S/T", song="Call Your Name")

        results, _ = await search_library_with_fallback(
            _db([edits, self_titled]), parsed, ["S/T", "Edits"]
        )

        assert [r.id for r in results] == [40, 41]

    @pytest.mark.asyncio
    async def test_fuzzy_corrected_artist_promotes_on_the_library_spelling(self):
        """Library channel of the LML#626 seam: the typed artist is misspelled,
        ``library_artist`` carries the correction, and the promotion compares
        against the corrected name."""
        parsed = _parsed(artist="Jesica Prat", library_artist="Jessica Pratt", album="S/T")

        results, _ = await search_library_with_fallback(_db(SHELF), parsed, ["S/T", "Quiet Signs"])

        assert [r.id for r in results] == [1, 4]

    @pytest.mark.asyncio
    async def test_every_artist_named_row_is_promoted_in_shelf_order(self):
        """Two formats of the self-titled record (LP and CD rows) both lead,
        in shelf order, ahead of the album-fed row."""
        second_format = make_library_item(id=5, artist="Jessica Pratt", title="Jessica Pratt")
        parsed = _parsed(album="S/T")

        results, _ = await search_library_with_fallback(
            _db([OTHER_ALBUM, SELF_TITLED_ROW, SIBLING_II, second_format]),
            parsed,
            ["S/T", "Quiet Signs"],
        )

        assert [r.id for r in results] == [1, 5, 4]

    @pytest.mark.asyncio
    async def test_row_discogs_already_named_is_not_duplicated(self):
        """Step 2 itself named the self-titled album, so the album-fed search
        already holds the artist-named row. It leads once."""
        parsed = _parsed(album="S/T")

        results, _ = await search_library_with_fallback(
            _db([OTHER_ALBUM, SELF_TITLED_ROW, SIBLING_II]),
            parsed,
            ["S/T", "Quiet Signs", "Jessica Pratt"],
        )

        assert [r.id for r in results] == [1, 4]

    @pytest.mark.asyncio
    async def test_typed_album_equal_to_artist_name_is_untouched(self):
        """LML#1412: typed album literally equal to the artist's name is not a
        placeholder, so the guard never fires -- no artist-only query is
        issued beyond the normal album-combined search."""
        db = _db(SHELF)
        parsed = _parsed(album="Jessica Pratt")

        await search_library_with_fallback(db, parsed, ["Jessica Pratt"])

        queries = [call.kwargs.get("query") for call in db.search.await_args_list]
        assert queries == ["Jessica Pratt Jessica Pratt"]

    @pytest.mark.asyncio
    async def test_non_placeholder_album_is_untouched(self):
        """A normal typed album (not a placeholder, not equal to the artist)
        never enters the new branch at all."""
        db = _db([OTHER_ALBUM])
        parsed = _parsed(album="Quiet Signs")

        await search_library_with_fallback(db, parsed, ["Quiet Signs"])

        queries = [call.kwargs.get("query") for call in db.search.await_args_list]
        assert queries == ["Jessica Pratt Quiet Signs"]


SELF_TITLED_RELEASE_ID = 111
OTHER_ALBUM_RELEASE_ID = 222


def _tokens(text):
    return set(re.findall(r"\w+", text.lower()))


async def _token_library_search(query, limit=None, **_kwargs):
    """Every query token must appear in the row, as with the FTS index: the
    artist-only query returns the shelf, and "Jessica Pratt S/T" returns
    nothing. A blanket ``return_value`` would hand the shelf to every strategy
    and hide the empty-pipeline lane the step-3a probe depends on."""
    wanted = _tokens(query)
    return [row for row in SHELF if wanted <= _tokens(f"{row.artist} {row.title}")]


async def _discogs_search(request: DiscogsSearchRequest, *_args, **_kwargs):
    album = (request.album or "").lower()
    if album == "jessica pratt":
        release_id = SELF_TITLED_RELEASE_ID
    elif album == "quiet signs":
        release_id = OTHER_ALBUM_RELEASE_ID
    else:
        return DiscogsSearchResponse(results=[])
    return DiscogsSearchResponse(
        results=[
            make_discogs_result(
                release_id=release_id, artist="Jessica Pratt", album=request.album or ""
            )
        ]
    )


class TestPerformLookupSelfTitledPlaceholder:
    """End to end through ``perform_lookup``, with the library answering by
    token match and Discogs knowing two releases: the self-titled record and
    Quiet Signs."""

    async def _lookup(self, mock_library_db, mock_discogs_service, *, step2_albums, song_is_on):
        mock_library_db.find_similar_artist.return_value = None
        mock_library_db.search = AsyncMock(side_effect=_token_library_search)
        mock_discogs_service.search = AsyncMock(side_effect=_discogs_search)
        mock_discogs_service.validate_track_on_release = AsyncMock(
            side_effect=lambda release_id, _song, _artist: release_id in song_is_on
        )
        request = LookupRequest(
            artist="Jessica Pratt",
            album="S/T",
            song="Back, Baby",
            raw_message="Play Back, Baby by Jessica Pratt, S/T",
        )
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Jessica Pratt", album) for album in step2_albums],
        ):
            return await perform_lookup(
                request, mock_library_db, mock_discogs_service, make_lml_telemetry()
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "song_is_on",
        [set(), {SELF_TITLED_RELEASE_ID, OTHER_ALBUM_RELEASE_ID}],
        ids=["song-confirmed-nowhere", "song-confirmed-on-both"],
    )
    async def test_artist_named_row_leads_the_response(
        self, mock_library_db, mock_discogs_service, song_is_on
    ):
        """The LML#1405 repro shape: step 2 resolves the song to an unrelated
        album and the shelf holds the artist-named row plus a numbered sibling.
        ``results[0]`` is the artist-named row, and the sibling is absent."""
        response = await self._lookup(
            mock_library_db,
            mock_discogs_service,
            step2_albums=["Quiet Signs"],
            song_is_on=song_is_on,
        )

        assert [item.library_item.id for item in response.results] == [1, 4]
        assert response.search_type == "direct"

    @pytest.mark.asyncio
    async def test_step_3b_keeps_only_the_promoted_row_when_the_song_is_on_it(
        self, mock_library_db, mock_discogs_service
    ):
        response = await self._lookup(
            mock_library_db,
            mock_discogs_service,
            step2_albums=["Quiet Signs"],
            song_is_on={SELF_TITLED_RELEASE_ID},
        )

        assert [item.library_item.id for item in response.results] == [1]
        assert response.search_type == "direct"

    @pytest.mark.asyncio
    async def test_step_3b_drops_the_promoted_row_when_the_song_is_on_the_other_album(
        self, mock_library_db, mock_discogs_service
    ):
        """Discogs confirms the song on Quiet Signs only. Step 3b narrows the
        list to the confirmed album, so the promoted row is gone and the
        response is the one ``main`` gives. What the promotion cost here is one
        Discogs search plus one tracklist validation for the promoted row."""
        response = await self._lookup(
            mock_library_db,
            mock_discogs_service,
            step2_albums=["Quiet Signs"],
            song_is_on={OTHER_ALBUM_RELEASE_ID},
        )

        assert [item.library_item.id for item in response.results] == [4]
        assert response.search_type == "direct"
        validated = [
            call.args[0] for call in mock_discogs_service.validate_track_on_release.await_args_list
        ]
        assert validated == [SELF_TITLED_RELEASE_ID, OTHER_ALBUM_RELEASE_ID]

    @pytest.mark.asyncio
    async def test_library_miss_probe_still_binds_the_self_titled_release(
        self, mock_library_db, mock_discogs_service
    ):
        """The blocking review finding on #1414, end to end. Discogs resolves
        no album for the song, so the pipeline returns no library row and the
        step-3a probe answers with a row-less item bound to the self-titled
        release. Promoting the shelf row instead closed that probe and the
        serve gate collapsed the binding to ``release_id`` 0."""
        response = await self._lookup(
            mock_library_db, mock_discogs_service, step2_albums=[], song_is_on=set()
        )

        assert response.results[0].library_item.id == 0
        assert response.results[0].artwork is not None
        assert response.results[0].artwork.release_id == SELF_TITLED_RELEASE_ID
