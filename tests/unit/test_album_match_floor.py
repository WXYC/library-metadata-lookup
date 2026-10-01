"""Unit tests for the artist-fallback album-match floor (LML#400, LML#1391).

When ``search_library_with_fallback`` falls through to the artist-only branch
(``song_not_found=True``), it historically returned an arbitrary library row
for the requested artist regardless of how that row's title related to the
DJ-typed album. Downstream ``enrich_artwork_results`` then attached the
matched Discogs release's ``release_year`` / ``apple_music_url`` /
``spotify_url`` / ``discogs_url`` / ``artwork_url`` and Backend-Service wrote
those onto the flowsheet row — contaminating ~184k free-text rows in prod
(see #400 evidence). #390 and #398 tightened the iTunes-result verification;
this guard tightened the LML lookup result itself by DROPPING any row whose
title didn't clear ``token_set_ratio >= _ALBUM_MATCH_FLOOR`` against the typed
album — which meant a typed album that matched nothing emptied the artist's
whole shelf (LML#1391): "Led Zeppelin" / "How Many More Times" / "Epon." came
back with zero rows even though the library holds 11 Led Zeppelin releases.

Since #400, LML#477 / LML#487 moved the metadata-contamination guard to the
enrichment serve gate (``lookup/enrichment/item.py::
compute_row_title_matches_requested_album`` / ``library_row_acceptable``),
which withholds artwork and curated streaming links per row regardless of
search-layer ranking (pinned in ``TestEnrichmentServeGateStillWithholdsMetadata``
below). So the search-layer floor no longer needs to drop rows to prevent
contamination — it only needs to rank floor-clearing rows first. Behavior
under test: when ``parsed.album`` is non-empty, candidate rows are
stable-partitioned — floor-clearing rows first, the rest after — never
dropped.

Mirrors the 80-floor + ``rapidfuzz.fuzz.token_set_ratio`` +
``wxyc_etl.text.to_match_form`` primitives used by ``_fetch_apple_music_url``
(#390 / #398).
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock

import pytest

from discogs.models import DiscogsSearchResult, ReleaseMetadataResponse
from lookup.binding_floor import (
    floor_row_binding,
    promotable_stash_rows,
    track_confirmed_row_ids,
)
from lookup.enrichment import enrich_artwork_results
from lookup.fallback_title_floors import _ALBUM_MATCH_FLOOR
from lookup.orchestrator import LookupState, _build_result_items
from lookup.release_resolution import ResolvedRelease
from lookup.rowless import ROWLESS_LIBRARY_ID
from lookup.strategies.artist_plus_album import search_library_with_fallback
from services.parser import MessageType, ParsedRequest
from tests.factories import make_discogs_result, make_library_item


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
    async def test_artist_fallback_ranks_but_keeps_rows_when_typed_album_misses_library_titles(
        self, mock_library_db
    ):
        """LML#1391 contract: Miles Davis / Kind of Blue against a library that
        has Miles Davis but no Kind of Blue. Neither shelved title clears the
        80-floor against "Kind of Blue", but the artist IS shelved — the
        cascade must return both rows (so the caller's "other albums by Miles
        Davis" message has something to show) rather than emptying the list.
        Search-layer ranking is a no-op here (both rows fail the floor, so
        original order is preserved); the #400 contamination concern this
        floor used to guard against is now enforced downstream at the
        enrichment serve gate (see ``TestEnrichmentServeGateStillWithholdsMetadata``).
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
        #   2) artist-only → returns both shelved candidates
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

        # Neither candidate clears the floor, but both survive — the artist's
        # shelf must not go empty just because the typed album matched nothing.
        assert {r.id for r in results} == {10, 11}
        # fallback (song_not_found) stays True: the typed album still wasn't found.
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
    async def test_outkast_top_offender_still_surfaces_the_shelf(self, mock_library_db):
        """Concrete prod offender from the #400 evidence table: Outkast's
        single Discogs release (``/release/1171368``) was inherited as
        metadata for 110 distinct DJ-typed album variants across 246 rows —
        because the search layer used to drop the row before it ever reached
        enrichment, which made the eventual #477/#487 serve gate untestable
        against this exact shape.

        A DJ typing "Outkast / Aquemini" against a library that only shelves
        "Stankonia" now gets that row back (LML#1391 — the artist IS on the
        shelf), with the #400 contamination risk enforced downstream: see
        ``TestEnrichmentServeGateStillWithholdsMetadata`` below.
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

        assert [r.id for r in results] == [40]
        assert fallback is True

    @pytest.mark.asyncio
    async def test_hank_williams_top_offender_still_surfaces_the_shelf(self, mock_library_db):
        """Concrete prod offender: Hank Williams ``/release/14773378`` was
        inherited across 130 distinct (artist, album) variants. DJ types
        "Hank Williams / Lovesick Blues", library only shelves "Moanin' the
        Blues" — token_set_ratio is below 80, so the row ranks last (a no-op
        here, it's the only row) but is no longer dropped (LML#1391).

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

        assert [r.id for r in results] == [50]
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
    async def test_artist_plus_song_branch_also_ranks_not_drops(self, mock_library_db):
        """The artist+song branch (not just artist-only) shares the same
        stable-partition ranking: ``search_library_with_fallback`` may return
        a row from ``f"{artist} {song}"`` keyword matching whose title has
        nothing to do with the typed album, and that row must still surface
        (LML#1391) rather than being dropped.
        """
        # FTS for "Nina Simone Sinnerman" surfaces "Pastel Blues" (which has
        # Sinnerman on it). But the DJ typed album="Wild Is the Wind".
        # token_set_ratio("Wild Is the Wind", "Pastel Blues") is below 80, so
        # the row doesn't clear the floor — it still comes back (this branch
        # returns as soon as it has anything), and any per-row metadata risk
        # is enforced downstream at the enrichment serve gate.
        pastel = make_library_item(
            id=70, artist="Nina Simone", title="Pastel Blues", call_letters="SI"
        )
        mock_library_db.search.side_effect = [
            [pastel],  # artist+song hit
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

        assert [r.id for r in results] == [70]
        assert fallback is True

    @pytest.mark.asyncio
    async def test_floor_ranks_clearing_rows_ahead_of_the_rest(self, mock_library_db):
        """LML#1391 repro shape: the artist-only fallback surfaces the whole
        shelf, and rows whose title clears the floor against the typed album
        come first, the rest following in their original order.
        """
        doga = make_library_item(id=1, artist="Juana Molina", title="DOGA", call_letters="MO")
        halo = make_library_item(
            id=2,
            artist="Juana Molina",
            title="Halo",
            call_letters="MO",
            release_call_number=2,
        )
        segundo = make_library_item(
            id=3,
            artist="Juana Molina",
            title="Segundo",
            call_letters="MO",
            release_call_number=3,
        )
        mock_library_db.search.side_effect = [
            [],  # artist+song
            [halo, segundo, doga],  # artist-only, deliberately not floor-sorted
        ]

        parsed = ParsedRequest(
            song="la paradoja",
            artist="Juana Molina",
            album="DOGA",
            raw_message="la paradoja - juana molina - doga",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(mock_library_db, parsed, [])

        # The typed album clears the floor only against its own exact title;
        # the rest keep their original relative order behind it.
        assert [r.id for r in results] == [1, 2, 3]
        assert fallback is True

    @pytest.mark.asyncio
    async def test_songless_artist_only_fallback_still_empties_on_a_typed_album_miss(
        self, mock_library_db
    ):
        """LML#1391 is scoped to the song-bearing lane. Without a song, the
        artist-only fallback must keep dropping floor-failing rows exactly as
        before: surfacing an artist's unrelated shelf on a songless request
        pre-empts the library-miss Discogs probe (`_step_library_miss_probe`)
        that would otherwise resolve the exact typed pair -- the LML#717
        regression this test guards against (pinned end-to-end in the golden
        corpus as `lml717-lone-galaxy-garden`). The songless lane's own fix is
        tracked separately as LML#1393.
        """
        unrelated = make_library_item(
            id=80, artist="Lone", title="Always Inside Your Head", call_letters="LO"
        )
        mock_library_db.search.side_effect = [
            [unrelated],  # artist-only (songless never tries artist+song)
        ]

        parsed = ParsedRequest(
            artist="Lone",
            album="Galaxy Garden",
            raw_message="Lone - Galaxy Garden",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(mock_library_db, parsed, [])

        assert results == []
        assert fallback is False


class TestEnrichmentServeGateStillWithholdsMetadata:
    """Regression guard for LML#400: once the search-layer floor stopped
    dropping rows (LML#1391), the metadata-contamination guard it used to
    provide must still hold — just at the enrichment serve gate instead
    (``lookup/enrichment/item.py::compute_row_title_matches_requested_album``
    / ``library_row_acceptable``). Parameterized over the #400 top-offender
    fixtures (Miles Davis, Outkast, Hank Williams) from this module.

    Scope: this gate is only half the guard, because it runs only on paths that
    reach step 4b. The other half is the response-assembly chokepoint pinned by
    ``TestResponseAssemblyChokepoint`` below, which re-applies the same per-row
    rule on every path (the degraded tail shed included).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("artist", "shelved_title", "typed_album"),
        [
            ("Miles Davis", "Bitches Brew", "Kind of Blue"),
            ("Outkast", "Stankonia", "Aquemini"),
            ("Hank Williams", "Moanin' the Blues", "Lovesick Blues"),
        ],
    )
    async def test_floor_failing_fallback_row_serves_without_artwork_or_links(
        self, artist, shelved_title, typed_album
    ):
        item = make_library_item(id=99, artist=artist, title=shelved_title, call_letters="XX")
        # A confident Discogs match for the row's OWN title -- exactly what the
        # artist-only fallback would have bound before this row ever reaches
        # enrichment. Its release_id/URL must not leak onto a response tagged
        # with a different typed album.
        artwork = make_discogs_result(
            release_id=555,
            artist=artist,
            album=shelved_title,
            artwork_url="https://example.com/wrong-album.jpg",
            release_year=1970,
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=555,
            title=shelved_title,
            artist=artist,
            year=1970,
            artist_id=None,
            release_url="https://discogs.com/release/555",
        )

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={
                "spotify_url": "https://open.spotify.com/album/wrong-album",
                "apple_music_url": "https://music.apple.com/us/album/wrong-album/1",
                "youtube_music_url": None,
                "bandcamp_url": None,
                "soundcloud_url": None,
            }
        )

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            album=typed_album,
            library_db=library_db,
        )

        _, enriched = results[0]
        assert enriched is not None
        # The #400 guard, now enforced at the serve layer: no release id, no
        # curated streaming links leak onto a row tagged with a mismatched
        # typed album.
        assert enriched.release_id == 0
        assert enriched.artwork_url != "https://example.com/wrong-album.jpg"
        assert enriched.release_year != 1970
        assert enriched.spotify_url != "https://open.spotify.com/album/wrong-album"
        assert enriched.apple_music_url != "https://music.apple.com/us/album/wrong-album/1"
        library_db.get_streaming_links.assert_not_awaited()


_COMP_RELEASE = ResolvedRelease(
    release_id=58611,
    release_url="https://www.discogs.com/release/58611",
    is_compilation=True,
    album_title="Buenos Aires Underground",
)


def _bound(release_id: int, title: str) -> DiscogsSearchResult:
    """A row bound to its OWN Discogs release, as step 4 or step 4b leaves it."""
    return make_discogs_result(
        release_id=release_id,
        artist="Juana Molina",
        album=title,
        artwork_url=f"https://example.com/{release_id}.jpg",
        release_year=2017,
        spotify_url=f"https://open.spotify.com/album/{release_id}",
    )


_HALO = make_library_item(id=31, artist="Juana Molina", title="Halo", call_letters="M")
_DOGA = make_library_item(id=32, artist="Juana Molina", title="DOGA", call_letters="M")
_COMP = make_library_item(
    id=40, artist="Various Artists - Rock - B", title="Buenos Aires Underground", call_letters="V"
)
_ROWLESS = make_library_item(
    id=ROWLESS_LIBRARY_ID, artist="Juana Molina", title="Buenos Aires Underground"
)


class TestResponseAssemblyChokepoint:
    """LML#1391 / LML#400: the serve rule is enforced where every path assembles
    the response (``lookup/orchestrator.py::_build_result_items``), not only in
    the step-4b serve gate, so a row reaching the wire by a path that skipped
    that gate (the degraded tail shed) still cannot carry a release nothing
    vouches for.

    A row keeps its own binding when its title clears the typed album, when it
    is the row-less carry-through (LML#628), or when the song was track-confirmed
    on it: by step-3b validation (``track_validated_ids``) or by the strategy
    that bound it (``discogs_titles`` with ``track_confirmed``). Each is decided
    per row -- never for every row of a ``found_on_compilation`` response.
    """

    @pytest.mark.parametrize(
        ("item", "artwork", "album", "discogs_titles", "validated_ids", "kept"),
        [
            pytest.param(_HALO, _bound(222, "Halo"), "Epon.", {}, (), False, id="floor-failing"),
            pytest.param(_DOGA, _bound(111, "DOGA"), "DOGA", {}, (), True, id="floor-clearing"),
            pytest.param(_HALO, _bound(222, "Halo"), None, {}, (), True, id="no-typed-album"),
            pytest.param(
                _ROWLESS, _bound(58611, "Buenos Aires Underground"), "Epon.", {}, (), True,
                id="rowless-carry-through",
            ),
            pytest.param(
                _COMP, _bound(58611, "Buenos Aires Underground"), "Epon.",
                {_COMP.id: _COMP_RELEASE}, (), True, id="compilation-hit",
            ),
            pytest.param(
                _HALO, _bound(222, "Halo"), "Epon.", {_COMP.id: _COMP_RELEASE}, (), False,
                id="floor-failing-in-compilation-response",
            ),
            # An album-level-only binding (the LML#1318 degrade) never vouched
            # for the track, so even the compilation row itself loses it.
            pytest.param(
                _COMP, _bound(58611, "Buenos Aires Underground"), "Epon.",
                {_COMP.id: dataclasses.replace(_COMP_RELEASE, track_confirmed=False)}, (), False,
                id="compilation-row-not-track-confirmed",
            ),
            # Step 3b confirmed the song on this row, so its release is the right
            # metadata for the track whatever album was typed.
            pytest.param(
                _DOGA, _bound(111, "DOGA"), "Epon.", {}, (_DOGA.id,), True,
                id="track-validated-shelf-row",
            ),
            # ...and that is per row: a sibling's confirmation vouches for nothing.
            pytest.param(
                _HALO, _bound(222, "Halo"), "Epon.", {}, (_DOGA.id,), False,
                id="floor-failing-beside-a-validated-row",
            ),
            # Every other gate reads a whitespace-only album as no album at all.
            pytest.param(_HALO, _bound(222, "Halo"), "  ", {}, (), True, id="whitespace-album"),
            # Step 4b already collapsed this row to the BS#1185 sentinel; the
            # artwork it carries is the Apple probe's pick for the REQUESTED album
            # (LML#487), not the row's own release, so it passes through untouched.
            pytest.param(
                _HALO,
                DiscogsSearchResult(
                    release_id=0, release_url="", artwork_url="https://example.com/probe.jpg"
                ),
                "Epon.", {}, (), True, id="already-collapsed-sentinel",
            ),
        ],
    )  # fmt: skip
    @pytest.mark.parametrize("found_on_compilation", [False, True])
    def test_row_binding_survives_only_when_its_title_or_provenance_vouches_for_it(
        self, item, artwork, album, discogs_titles, validated_ids, kept, found_on_compilation
    ):
        """``found_on_compilation`` is crossed in to pin that it decides nothing:
        the verdict is the row's own, on a compilation response or off one."""
        state = LookupState(
            items_with_artwork=[(item, artwork)],
            found_on_compilation=found_on_compilation,
            discogs_titles=discogs_titles,
            track_validated_ids=frozenset(validated_ids),
        )

        [result] = _build_result_items(state, {}, album)

        if kept:
            assert result.artwork == artwork.to_match_result()
        else:
            assert result.artwork is not None
            assert result.artwork.release_id == 0
            assert result.artwork.release_url == ""
            assert result.artwork.artwork_url is None
            assert result.artwork.release_year is None
            assert result.artwork.spotify_url is None

    def test_rowless_seam_entry_confirms_no_library_row(self):
        """The row-less key (id 0) names no shelf row, so it never enters the
        confirmed set; the carry-through keeps its binding by its own clause."""
        titles = {ROWLESS_LIBRARY_ID: _COMP_RELEASE, _COMP.id: _COMP_RELEASE}

        assert track_confirmed_row_ids(titles, frozenset({_DOGA.id})) == {_COMP.id, _DOGA.id}


_KIND_OF_BLUE = make_library_item(id=51, artist="Miles Davis", title="Kind of Blue")
_SKETCHES = make_library_item(id=52, artist="Miles Davis", title="Sketches of Spain")


class TestCompilationStashPromotionSharesTheServeRule:
    """LML#1391 review rounds 3-4: which stashed artist rows step 3b may prepend
    ahead of a compilation is decided by the serve rule itself, after validation,
    so no row can lead a compilation response only to be stripped at assembly."""

    @pytest.mark.parametrize(
        ("album", "confirmed", "promoted"),
        [
            # A token subset: token_set_ratio 100, score_match 61.5. Only the
            # track confirmation carries it (the edition-name request).
            pytest.param("Kind of Blue Legacy Edition", (51,), [_KIND_OF_BLUE], id="confirmed"),
            # Kept unvalidated on a breaker shed: nothing vouches for it.
            pytest.param("Kind of Blue Legacy Edition", (), [], id="token-subset-unconfirmed"),
            pytest.param("Kind of Blue", (), [_KIND_OF_BLUE], id="title-clears-unconfirmed"),
            pytest.param("Zzyzx Road", (52,), [_SKETCHES], id="album-miss-confirmed-row-only"),
            pytest.param(None, (), [_KIND_OF_BLUE, _SKETCHES], id="no-typed-album"),
            pytest.param("  ", (), [_KIND_OF_BLUE, _SKETCHES], id="whitespace-album"),
        ],
    )
    def test_promotes_exactly_the_rows_the_chokepoint_would_serve(self, album, confirmed, promoted):
        stash = [_KIND_OF_BLUE, _SKETCHES]
        confirmed_ids = frozenset(confirmed)

        assert promotable_stash_rows(album, stash, confirmed_ids) == promoted
        for item in stash:
            bound = _bound(1000 + item.id, item.title)
            served = floor_row_binding(album, item, bound, confirmed_ids) is bound
            assert served == (item in promoted)


class TestServeGateHonorsTrackConfirmationPerRow:
    """LML#1391 round 4: step 4b applies the same per-row rule, so a row the
    chokepoint keeps is not collapsed to the sentinel one step earlier."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("confirmed", "kept"),
        [
            pytest.param((32,), True, id="track-confirmed-row-keeps-its-release"),
            pytest.param((31,), False, id="a-sibling-confirmation-vouches-for-nothing"),
            pytest.param((), False, id="unconfirmed-row-collapses"),
        ],
    )
    async def test_floor_failing_row_keeps_its_release_only_when_confirmed(self, confirmed, kept):
        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=111,
            title="DOGA",
            artist="Juana Molina",
            year=2017,
            artist_id=None,
            release_url="https://discogs.com/release/111",
        )

        results = await enrich_artwork_results(
            [(_DOGA, _bound(111, "DOGA"))],
            discogs_service,
            album="Zzyzx Road",
            track_confirmed_ids=frozenset(confirmed),
        )

        _, enriched = results[0]
        assert enriched is not None
        assert (enriched.release_id == 111) is kept
        assert (enriched.artwork_url == "https://example.com/111.jpg") is kept
