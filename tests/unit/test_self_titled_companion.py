"""Tests for the self-titled companion row (LML#1405).

A typed self-titled placeholder ("Epon.", "S/T") that the pipeline answered
with some other album gains the row titled the artist's name as a display-only
row after the rows already returned. The invariant under test throughout:
**every row ``main`` returns is untouched and keeps its position; the only
change is appended rows with no ``artwork``.**

``TestCompanionRow`` calls ``apply_shelf_fallback`` directly.
``TestPerformLookupCompanionRow`` drives the real pipeline, mocking only
``LibraryDB``/``DiscogsService``, the same way ``test_shelf_fallback.py`` does.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import DiscogsSearchResponse
from lookup.matching import MAX_SEARCH_RESULTS
from lookup.miss_kind import OUTCOME_HIT, derive_miss_kind
from lookup.models import LookupRequest, LookupResultItem
from lookup.orchestrator import perform_lookup
from lookup.shelf_fallback import apply_shelf_fallback
from services.parser import MessageType, ParsedRequest
from tests.conftest import make_lml_telemetry
from tests.factories import make_discogs_result, make_library_item

PLACEHOLDERS = ["Epon.", "epon", "eponymous", "S/T", "s.t.", "self-titled", "self titled"]


@pytest.fixture
def telemetry():
    return make_lml_telemetry()


def _parsed(**overrides):
    defaults = {
        "artist": "Cat Power",
        "album": "Epon.",
        "song": "Cross Bones Style",
        "message_type": MessageType.REQUEST,
        "is_request": True,
    }
    defaults.update(overrides)
    return ParsedRequest(**defaults)


SELF_TITLED = make_library_item(id=301, artist="Cat Power", title="Cat Power")
NUMBERED = make_library_item(id=302, artist="Cat Power", title="Cat Power II")
MOON_PIX = make_library_item(id=303, artist="Cat Power", title="Moon Pix")


def _bound(row, release_id=555):
    """A row the pipeline served with a release bound to it."""
    return LookupResultItem(
        library_item=row.to_catalog_item(),
        artwork=make_discogs_result(release_id=release_id).to_match_result(),
    )


def _db(rows):
    db = AsyncMock()
    db.search = AsyncMock(return_value=list(rows))
    return db


async def _apply(parsed, db, existing, skip=False):
    return await apply_shelf_fallback(parsed, db, skip, existing, "direct", None, "library")


class TestCompanionRow:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    async def test_artist_named_row_is_appended(self, placeholder):
        existing = [_bound(MOON_PIX)]
        db = _db([MOON_PIX, NUMBERED, SELF_TITLED])

        items, search_type, context, source, netted = await _apply(
            _parsed(album=placeholder), db, existing
        )

        assert [item.library_item.id for item in items] == [303, 301]
        assert items[0] is existing[0]
        assert items[1].artwork is None
        assert (search_type, context, source, netted) == ("direct", None, "library", 0)

    @pytest.mark.asyncio
    async def test_rows_main_returns_keep_their_positions(self):
        other = make_library_item(id=304, artist="Cat Power", title="You Are Free")
        existing = [_bound(MOON_PIX), _bound(other, release_id=556)]

        items, *_ = await _apply(_parsed(), _db([SELF_TITLED, MOON_PIX, other]), existing)

        assert [item.library_item.id for item in items] == [303, 304, 301]
        assert items[0] is existing[0] and items[1] is existing[1]

    @pytest.mark.asyncio
    async def test_no_insert_when_the_response_is_already_full(self):
        """No row ``main`` returns is ever evicted to make room."""
        existing = [
            _bound(make_library_item(id=400 + n, artist="Cat Power", title=f"Album {n}"))
            for n in range(MAX_SEARCH_RESULTS)
        ]

        db = _db([SELF_TITLED])

        out = await _apply(_parsed(), db, existing)

        assert out == (existing, "direct", None, "library", 0)
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_insert_is_capped_by_the_room_left(self):
        existing = [
            _bound(make_library_item(id=400 + n, artist="Cat Power", title=f"Album {n}"))
            for n in range(MAX_SEARCH_RESULTS - 1)
        ]
        lp = make_library_item(id=305, artist="Cat Power", title="Cat Power", format="LP")

        items, *_ = await _apply(_parsed(), _db([SELF_TITLED, lp]), existing)

        assert [item.library_item.id for item in items] == [400, 401, 402, 403, 301]

    @pytest.mark.asyncio
    async def test_no_insert_when_the_row_is_already_returned(self):
        existing = [_bound(MOON_PIX), _bound(SELF_TITLED, release_id=556)]

        out = await _apply(_parsed(), _db([SELF_TITLED, MOON_PIX]), existing)

        assert out == (existing, "direct", None, "library", 0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "parsed",
        [
            _parsed(album="Moon Pix"),
            _parsed(album="Cat Power"),
            _parsed(album=None),
            _parsed(artist=None),
        ],
        ids=["real-album", "album-is-artist", "no-album", "no-artist"],
    )
    async def test_non_placeholder_requests_never_search(self, parsed):
        existing = [_bound(MOON_PIX)]
        db = _db([SELF_TITLED])

        out = await _apply(parsed, db, existing)

        assert out == (existing, "direct", None, "library", 0)
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skip_never_searches(self):
        """Low-priority callers (Backend enrichment, bulk), timed-out and shed
        responses are byte-identical to ``main``."""
        existing = [_bound(MOON_PIX)]
        db = _db([SELF_TITLED])

        out = await _apply(_parsed(), db, existing, skip=True)

        assert out == (existing, "direct", None, "library", 0)
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "literal_title"),
        [("eponymous", "Eponymous"), ("Epon.", "S/T"), ("S/T", "s/t"), ("s.t.", "S.T.")],
    )
    async def test_literal_title_on_the_shelf_wins(self, typed, literal_title):
        """LML#1392's guard: a placeholder that names a shelved record is a
        real title, so the request is left exactly as ``main`` answers it."""
        literal = make_library_item(id=306, artist="Cat Power", title=literal_title)
        existing = [_bound(literal)]

        out = await _apply(_parsed(album=typed), _db([literal, SELF_TITLED]), existing)

        assert out == (existing, "direct", None, "library", 0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("search_type", "context"),
        [
            ("compilation", 'Found "Cross Bones Style" by Cat Power on:'),
            ("compilation", None),
            ("direct", 'Found "Cross Bones Style" by Cat Power on:'),
            ("fallback", '"Epon." not found in the library, but here are other albums by X:'),
        ],
    )
    async def test_no_companion_under_a_claim_about_the_rows(self, search_type, context):
        """A companion is not known to carry the song, so it is never listed
        under "Found X on:" or beside the location-union fold's rows."""
        existing = [_bound(MOON_PIX)]
        db = _db([MOON_PIX, SELF_TITLED])

        out = await apply_shelf_fallback(
            _parsed(), db, False, existing, search_type, context, "library"
        )

        assert out == (existing, search_type, context, "library", 0)
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fuzzy_corrected_artist_is_the_one_searched(self):
        existing = [_bound(MOON_PIX)]
        db = _db([MOON_PIX, SELF_TITLED])
        parsed = _parsed(artist="Cat Powr", library_artist="Cat Power")

        items, *_ = await _apply(parsed, db, existing)

        assert [item.library_item.id for item in items] == [303, 301]
        assert db.search.await_args.kwargs["query"] == "Cat Power"

    @pytest.mark.asyncio
    async def test_row_less_probe_item_keeps_the_lead(self):
        """Step 3a's synthesized ``id == 0`` item stays ``results[0]``."""
        probe = _bound(make_library_item(id=0, artist="Cat Power", title="Cat Power"))

        items, *_ = await _apply(_parsed(), _db([MOON_PIX, SELF_TITLED]), [probe])

        assert items[0] is probe
        assert [item.library_item.id for item in items] == [0, 301]

    @pytest.mark.asyncio
    async def test_artist_without_an_artist_named_row_is_unchanged(self):
        existing = [_bound(MOON_PIX)]

        out = await _apply(_parsed(), _db([MOON_PIX, NUMBERED]), existing)

        assert out == (existing, "direct", None, "library", 0)

    @pytest.mark.asyncio
    async def test_another_artists_row_is_never_inserted(self):
        """Artist equality, never a prefix: "Cat" must not pick up Cat Power."""
        cat = make_library_item(id=310, artist="Cat", title="Nine Lives")
        existing = [_bound(cat)]
        lookalike = make_library_item(id=311, artist="Cat Power", title="Cat")

        out = await _apply(_parsed(artist="Cat"), _db([cat, lookalike]), existing)

        assert out == (existing, "direct", None, "library", 0)

    @pytest.mark.asyncio
    async def test_search_failure_leaves_the_response_unchanged(self):
        existing = [_bound(MOON_PIX)]
        db = AsyncMock()
        db.search = AsyncMock(side_effect=RuntimeError("db gone"))

        out = await _apply(_parsed(), db, existing)

        assert out == (existing, "direct", None, "library", 0)

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        db = AsyncMock()
        db.search = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await _apply(_parsed(), db, [_bound(MOON_PIX)])

    @pytest.mark.asyncio
    async def test_sentry_attr_is_separate_from_the_shelf_lane(self):
        with patch("lookup.shelf_fallback.sentry_sdk") as sentry:
            await _apply(_parsed(), _db([MOON_PIX, SELF_TITLED]), [_bound(MOON_PIX)])

        sentry.get_current_scope.return_value.transaction.set_data.assert_called_once_with(
            "lookup.self_titled_companion_rows", 1
        )


class TestPerformLookupCompanionRow:
    def _wire(self, mock_library_db, mock_discogs_service, shelf):
        mock_library_db.find_similar_artist.return_value = None

        async def fake_search(query=None, **kwargs):
            if query == "Cat Power":
                return list(shelf)
            if query == "Cat Power Moon Pix":
                return [MOON_PIX]
            return []

        mock_library_db.search.side_effect = fake_search
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.validate_track_on_release.return_value = True

    async def _lookup(self, mock_library_db, mock_discogs_service, telemetry):
        request = LookupRequest(
            artist="Cat Power",
            song="Cross Bones Style",
            album="Epon.",
            raw_message="Cross Bones Style - Cat Power - Epon.",
        )
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Cat Power", "Moon Pix")],
        ):
            return await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

    @pytest.mark.asyncio
    async def test_placeholder_answered_with_another_album_gains_the_self_titled_row(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """The LML#1405 repro shape: Discogs resolves the song to another album
        only, so the self-titled record is never searched for."""
        self._wire(mock_library_db, mock_discogs_service, [MOON_PIX, NUMBERED, SELF_TITLED])

        response = await self._lookup(mock_library_db, mock_discogs_service, telemetry)

        assert [item.library_item.id for item in response.results] == [303, 301]
        assert response.results[1].artwork is None
        assert response.search_type == "direct"
        assert response._shelf_fallback_rows == 0
        assert derive_miss_kind(response) == OUTCOME_HIT

    @pytest.mark.asyncio
    async def test_low_priority_caller_gets_exactly_what_main_returns(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        self._wire(mock_library_db, mock_discogs_service, [MOON_PIX, NUMBERED, SELF_TITLED])

        with patch("lookup.orchestrator.is_discogs_low_priority", return_value=True):
            response = await self._lookup(mock_library_db, mock_discogs_service, telemetry)

        assert [item.library_item.id for item in response.results] == [303]
