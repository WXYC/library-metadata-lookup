"""Unit tests for the artist-fallback album-match floor (LML#400).

When ``search_library_with_fallback`` falls through to the artist-only branch
(``song_not_found=True``), it historically returned an arbitrary library row
for the requested artist regardless of how that row's title related to the
DJ-typed album. Downstream ``enrich_artwork_results`` then attached the
matched Discogs release's ``release_year`` / ``apple_music_url`` /
``spotify_url`` / ``discogs_url`` / ``artwork_url`` and Backend-Service wrote
those onto the flowsheet row — contaminating ~184k free-text rows in prod
(see #400 evidence). #390 and #398 tightened the iTunes-result verification;
this guard tightens the LML lookup result itself.

Behavior under test: when ``parsed.album`` is non-empty AND the candidate
library row's title doesn't clear ``token_set_ratio >= _ALBUM_MATCH_FLOOR``
against ``parsed.album`` (diacritic-folded), the row is rejected. If every
artist-fallback candidate is rejected, the function returns the
unknown-artist shape (``[], song_not_found_flag``) — no album-derived
metadata leaks downstream.

Mirrors the 80-floor + ``rapidfuzz.fuzz.token_set_ratio`` +
``wxyc_etl.text.to_match_form`` primitives used by ``_fetch_apple_music_url``
(#390 / #398).
"""

from __future__ import annotations

import pytest

from lookup.matching import _ALBUM_MATCH_FLOOR
from lookup.strategies.artist_plus_album import search_library_with_fallback
from services.parser import MessageType, ParsedRequest
from tests.factories import make_library_item


class TestArtistFallbackAlbumMatchFloor:
    """Artist-fallback contamination guard (LML#400).

    Locked Approach 1: when the DJ-typed album is non-empty and the cascade
    falls through to artist-only matching, surviving rows must clear the
    80-floor token_set_ratio against the requested album, otherwise drop.
    """

    def test_floor_constant_is_80(self):
        """Pinned at the same value as #390/#398's ``_APPLE_MUSIC_MATCH_FLOOR``."""
        assert _ALBUM_MATCH_FLOOR == 80.0

    @pytest.mark.asyncio
    async def test_artist_fallback_rejected_when_typed_album_misses_library_titles(
        self, mock_library_db
    ):
        """The #400 failure mode: Miles Davis / Kind of Blue against a library that
        has Miles Davis but no Kind of Blue. Without the floor the cascade returns
        an arbitrary Miles Davis library row whose title bears no relation to the
        typed album, and downstream enrichment inherits the matched release's
        Discogs URL / release year / Spotify URL / Apple URL onto a row tagged
        ``album_title="Kind of Blue"``.
        """
        # Library has Miles Davis but only "Bitches Brew" and "On the Corner".
        # Neither title overlaps with "Kind of Blue" at token_set_ratio >= 80.
        bitches_brew = make_library_item(
            id=10, artist="Miles Davis", title="Bitches Brew", call_letters="DA"
        )
        on_the_corner = make_library_item(
            id=11,
            artist="Miles Davis",
            title="On the Corner",
            call_letters="DA",
            release_call_number=2,
        )
        # search.side_effect drives the cascade:
        #   1) artist+song (FTS) → no match
        #   2) artist-only → returns the contaminating candidates
        mock_library_db.search.side_effect = [
            [],
            [bitches_brew, on_the_corner],
        ]

        parsed = ParsedRequest(
            song="So What",
            artist="Miles Davis",
            album="Kind of Blue",
            raw_message="So What by Miles Davis - Kind of Blue",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(mock_library_db, parsed, [])

        # Both candidates fail the 80-floor against "Kind of Blue" → empty.
        assert results == []
        # song_not_found stays True so downstream cascades know nothing surfaced
        # and the response mirrors the unknown-artist shape.
        assert fallback is True

    @pytest.mark.asyncio
    async def test_artist_fallback_keeps_candidate_when_title_clears_floor(self, mock_library_db):
        """When the artist-fallback candidate's title actually matches the typed
        album (token_set_ratio >= 80), it survives the floor.

        Token-set-ratio handles "Kind of Blue" vs "Kind of Blue (50th
        Anniversary Edition)" — the extra parenthetical doesn't sink the
        match because token_set is set-based, not order-or-length-based.
        """
        # Same artist, with a title that overlaps the typed album.
        anniversary = make_library_item(
            id=20,
            artist="Miles Davis",
            title="Kind of Blue (50th Anniversary Edition)",
            call_letters="DA",
        )
        # Artist-only path: artist+song misses, artist-only returns the rich title.
        mock_library_db.search.side_effect = [
            [],
            [anniversary],
        ]

        parsed = ParsedRequest(
            song="So What",
            artist="Miles Davis",
            album="Kind of Blue",
            raw_message="So What by Miles Davis - Kind of Blue",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(mock_library_db, parsed, [])

        assert len(results) == 1
        assert results[0].id == 20
        assert fallback is True

    @pytest.mark.asyncio
    async def test_floor_skipped_when_typed_album_empty(self, mock_library_db):
        """No typed album = no contamination signal. The cascade behaves exactly
        as before — every artist-fallback candidate is returned for the
        downstream ``filter_results_by_track_validation`` sweep to verify.

        This is the regression pin against the 808 State / Flow Coma case
        (``test_artist_fallback_when_discogs_albums_not_in_library`` in
        test_orchestrator_helpers.py): no ``parsed.album`` means the floor
        doesn't fire, and downstream Discogs track validation does its job.
        """
        false_positive = make_library_item(
            id=958,
            artist="808 State",
            title="808 State",
            call_letters="Ei",
        )
        mock_library_db.search.side_effect = [
            [false_positive],  # artist+song fuzzy match
        ]

        parsed = ParsedRequest(
            song="Flow Coma",
            artist="808 State",
            # No album provided.
            raw_message="flow coma by 808 state",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(mock_library_db, parsed, [])

        # Floor doesn't fire (parsed.album is None) → existing behavior preserved.
        assert len(results) == 1
        assert results[0].id == 958
        assert fallback is True

    @pytest.mark.asyncio
    async def test_floor_skipped_when_typed_album_whitespace_only(self, mock_library_db):
        """A whitespace-only ``parsed.album`` doesn't carry any contamination
        signal — treat it as no-album so the floor doesn't fire."""
        candidate = make_library_item(
            id=30,
            artist="Outkast",
            title="Stankonia",
            call_letters="OU",
        )
        mock_library_db.search.side_effect = [
            [candidate],  # artist+song
        ]

        parsed = ParsedRequest(
            song="Ms. Jackson",
            artist="Outkast",
            album="   ",  # whitespace only
            raw_message="Ms. Jackson - Outkast",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(mock_library_db, parsed, [])

        assert len(results) == 1
        assert results[0].id == 30
        assert fallback is True

    @pytest.mark.asyncio
    async def test_floor_rejects_outkast_top_offender(self, mock_library_db):
        """Concrete prod offender from the #400 evidence table: Outkast's
        single Discogs release (``/release/1171368``) was inherited as
        metadata for 110 distinct DJ-typed album variants across 246 rows.

        With the floor, a DJ typing "Outkast / Aquemini" against a library
        that surfaces "Stankonia" via artist-fallback returns empty —
        Stankonia's title doesn't clear 80-floor against "Aquemini".
        """
        stankonia = make_library_item(id=40, artist="Outkast", title="Stankonia", call_letters="OU")
        mock_library_db.search.side_effect = [
            [],  # artist+song
            [stankonia],  # artist-only
        ]

        parsed = ParsedRequest(
            song="Rosa Parks",
            artist="Outkast",
            album="Aquemini",
            raw_message="Rosa Parks - Outkast - Aquemini",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(mock_library_db, parsed, [])

        assert results == []
        assert fallback is True

    @pytest.mark.asyncio
    async def test_floor_rejects_hank_williams_top_offender(self, mock_library_db):
        """Concrete prod offender: Hank Williams ``/release/14773378`` was
        inherited across 130 distinct (artist, album) variants. DJ types
        "Hank Williams / Lovesick Blues", library only surfaces "Moanin'
        the Blues" — token_set_ratio is below 80, candidate is rejected.

        ``token_set_ratio`` shares "Blues" between the two titles, but the
        rest of the tokens differ enough to keep the ratio below 80. Pin
        the live value with a sanity assert against rapidfuzz to guard
        against drift if the upstream tokenizer ever changes.
        """
        from rapidfuzz import fuzz
        from wxyc_etl.text import to_match_form

        # Sanity-check the live ratio so the test isn't relying on opaque
        # internals. If rapidfuzz ever drifts above 80 here we want to know.
        assert (
            fuzz.token_set_ratio(
                to_match_form("Lovesick Blues"),
                to_match_form("Moanin' the Blues"),
            )
            < _ALBUM_MATCH_FLOOR
        )

        moanin = make_library_item(
            id=50, artist="Hank Williams", title="Moanin' the Blues", call_letters="WI"
        )
        mock_library_db.search.side_effect = [
            [],  # artist+song
            [moanin],  # artist-only
        ]

        parsed = ParsedRequest(
            song="Lovesick Blues",
            artist="Hank Williams",
            album="Lovesick Blues",
            raw_message="Lovesick Blues - Hank Williams",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(mock_library_db, parsed, [])

        assert results == []
        assert fallback is True

    @pytest.mark.asyncio
    async def test_floor_handles_diacritics_via_normalization(self, mock_library_db):
        """``to_match_form`` strips diacritics before scoring, so accented
        album titles match the unaccented form (and vice versa)."""
        canonical = make_library_item(
            id=60,
            artist="Hermanos Gutiérrez",
            title="El Bueno Y El Malo",
            call_letters="GU",
        )
        mock_library_db.search.side_effect = [
            [],  # artist+song
            [canonical],  # artist-only
        ]

        # DJ typed ASCII-only album, library row carries accented variant.
        parsed = ParsedRequest(
            song="Tres Hermanos",
            artist="Hermanos Gutierrez",
            album="El Bueno y el Malo",  # diacritic-free + casing variant
            raw_message="Tres Hermanos - Hermanos Gutierrez",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(mock_library_db, parsed, [])

        assert len(results) == 1
        assert results[0].id == 60
        assert fallback is True

    @pytest.mark.asyncio
    async def test_artist_plus_song_branch_also_filtered(self, mock_library_db):
        """The artist+song branch (not just artist-only) is also a contamination
        source: ``search_library_with_fallback`` may return a row from
        ``f"{artist} {song}"`` keyword matching that has nothing to do with
        the typed album. The floor must apply to that branch too.
        """
        # FTS for "Nina Simone Sinnerman" surfaces "Pastel Blues" (which has
        # Sinnerman on it). But the DJ typed album="Wild Is the Wind".
        # token_set_ratio("Wild Is the Wind", "Pastel Blues") is below 80.
        pastel = make_library_item(
            id=70, artist="Nina Simone", title="Pastel Blues", call_letters="SI"
        )
        mock_library_db.search.side_effect = [
            [pastel],  # artist+song hit
            [pastel],  # artist-only hit
        ]

        parsed = ParsedRequest(
            song="Sinnerman",
            artist="Nina Simone",
            album="Wild Is the Wind",
            raw_message="Sinnerman by Nina Simone — Wild Is the Wind",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(mock_library_db, parsed, [])

        # Pastel Blues is a real Nina Simone album that contains Sinnerman, but
        # the DJ typed "Wild Is the Wind" — surfacing Pastel Blues would attach
        # Pastel Blues metadata onto a flowsheet row tagged Wild Is the Wind.
        # The floor drops it.
        assert results == []
        assert fallback is True
