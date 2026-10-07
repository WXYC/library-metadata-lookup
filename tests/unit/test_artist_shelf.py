"""``lookup/artist_shelf.py`` against a real SQLite catalog (LML#1406).

A real ``library_fts`` index, built with the repo's own DDL, rather than a
mocked ``LibraryDB``: the defect this module fixes lived in the gap between
what the full-text index returns and what the lane keeps, which a mock cannot
show.
"""

import pytest

from lookup.artist_shelf import artist_spellings, rows_for_artist
from lookup.matching import _FETCH_LIMIT
from tests.factories import crowd_rows, make_library_catalog


class TestCrowdedOutArtists:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("artist", ["Love", "The The", "Heart", "The Band"])
    async def test_artist_behind_a_full_search_window_gets_its_own_rows(self, tmp_path, artist):
        """The artist's rows sit after 60 rows that share its name's words.
        The 50-row window holds none of them; the artist-keyed read holds all,
        and nothing else."""
        db = await make_library_catalog(
            tmp_path,
            [*crowd_rows(artist), (artist, "First Album"), (artist, "Second Album")],
        )
        try:
            window = await db.search(query=artist, limit=_FETCH_LIMIT)
            assert [row for row in window if row.artist == artist] == []

            rows = await rows_for_artist(db, artist)
        finally:
            await db.close()

        assert [(row.artist, row.title) for row in rows] == [
            (artist, "First Album"),
            (artist, "Second Album"),
        ]


class TestThisArtistOnly:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "catalog", "expected"),
        [
            pytest.param(
                "Can",
                [("Canibus", "Can-I-Bus"), ("Can", "Ege Bamyasi"), ("Tin Can Phone", "Hello")],
                [("Can", "Ege Bamyasi")],
                id="prefix-and-containing-names-are-other-artists",
            ),
            pytest.param(
                "Low",
                [("Low Profile", "We're in This Together"), ("The Low Numbers", "Twist Again")],
                [],
                id="only-longer-names-shelved",
            ),
            pytest.param(
                "Sun Ra",
                [("Sun Ra", "Lanquidity"), ("Sun Ra Arkestra", "Swirling")],
                [("Sun Ra", "Lanquidity")],
                id="longer-credit-is-a-different-artist",
            ),
            pytest.param(
                "nilufer yanya",
                [("Nilüfer Yanya", "PAINLESS"), ("NILÜFER YANYA", "My Method Actor")],
                [("Nilüfer Yanya", "PAINLESS"), ("NILÜFER YANYA", "My Method Actor")],
                id="case-and-diacritics-are-one-artist",
            ),
            pytest.param(
                "Stereolab",
                [("Stereolab", "Dots and Loops"), ("Cat Power", "Stereolab Covers")],
                [("Stereolab", "Dots and Loops")],
                id="name-in-another-artists-title-is-not-a-row",
            ),
            pytest.param("   ", [("Stereolab", "Dots and Loops")], [], id="blank-artist"),
            pytest.param("!!!", [("Stereolab", "Dots and Loops")], [], id="no-searchable-term"),
        ],
    )
    async def test_exact_rung(self, tmp_path, typed, catalog, expected):
        db = await make_library_catalog(tmp_path, catalog)
        try:
            rows = await rows_for_artist(db, typed)
        finally:
            await db.close()

        assert [(row.artist, row.title) for row in rows] == expected


class TestTolerantRungs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "catalog", "expected"),
        [
            pytest.param(
                "Clientele",
                [("The Clientele", "Suburban Light"), ("Clientele Records", "Sampler")],
                ["The Clientele"],
                id="article-dropped-by-the-listener",
            ),
            pytest.param(
                "The Stereolab",
                [("Stereolab", "Dots and Loops")],
                ["Stereolab"],
                id="article-added-by-the-listener",
            ),
            pytest.param(
                "Melt Banana",
                [("Melt-Banana", "Fetch")],
                ["Melt-Banana"],
                id="punctuation-dropped-by-the-listener",
            ),
            pytest.param(
                "Chuquimamani Condori",
                [("Chuquimamani-Condori", "Edits")],
                ["Chuquimamani-Condori"],
                id="hyphen-typed-as-a-space",
            ),
            pytest.param(
                "Clientele!",
                [("The Clientele", "Suburban Light")],
                ["The Clientele"],
                id="article-and-punctuation-together",
            ),
            pytest.param(
                "The Girls",
                [("Girls", "Album"), ("The Girls", "Reunion")],
                ["The Girls"],
                id="exact-spelling-beats-the-article-rung",
            ),
            pytest.param(
                "Girls",
                [("Girls", "Album"), ("The Girls", "Reunion")],
                ["Girls"],
                id="exact-spelling-beats-the-article-rung-reversed",
            ),
            pytest.param(
                "Frames",
                [("A Frames", "Black Forest"), ("The Frames", "Fitzcarraldo")],
                [],
                id="article-rung-matching-two-artists-returns-neither",
            ),
            pytest.param(
                "Alaska",
                [("Alaska!", "Emotions"), ("Alaska?", "Rescue")],
                [],
                id="punctuation-rung-matching-two-artists-returns-neither",
            ),
            pytest.param(
                "A-Bones",
                [("Bones", "Laughing")],
                [],
                id="initial-is-not-an-article",
            ),
            pytest.param("The", [("The The", "Soul Mining")], [], id="bare-article-is-not-a-name"),
            pytest.param(
                "Stereolab " * 13,
                [("Stereolab", "Dots and Loops")],
                [],
                id="overlong-name-is-not-read",
            ),
        ],
    )
    async def test_variant_spellings(self, tmp_path, typed, catalog, expected):
        db = await make_library_catalog(tmp_path, catalog)
        try:
            rows = await rows_for_artist(db, typed)
        finally:
            await db.close()

        assert [row.artist for row in rows] == expected

    @pytest.mark.asyncio
    async def test_tolerant_rung_keeps_case_variants_of_one_artist(self, tmp_path):
        """Two stored casings are one artist under the exact rung, so a
        tolerant rung matching both is not ambiguous."""
        db = await make_library_catalog(
            tmp_path, [("The Clientele", "Suburban Light"), ("THE CLIENTELE", "Strange Geometry")]
        )
        try:
            rows = await rows_for_artist(db, "Clientele")
        finally:
            await db.close()

        assert [row.title for row in rows] == ["Suburban Light", "Strange Geometry"]

    @pytest.mark.asyncio
    async def test_every_row_comes_back_in_catalog_order(self, tmp_path):
        """No window on the rows either, and two stored casings interleave by
        id rather than grouping by spelling."""
        catalog = [("Stereolab" if i % 2 else "STEREOLAB", f"Album {i}") for i in range(1, 61)]
        db = await make_library_catalog(tmp_path, catalog)
        try:
            rows = await rows_for_artist(db, "Stereolab")
        finally:
            await db.close()

        assert [row.id for row in rows] == list(range(1, 61))


class TestArtistSpellings:
    """The rung pick alone, before any row is read (LML#1421 reads rows its own way)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "catalog", "expected"),
        [
            pytest.param(
                "Stereolab",
                [("Stereolab", "Dots and Loops"), ("STEREOLAB", "Emperor Tomato Ketchup")],
                ["Stereolab", "STEREOLAB"],
                id="every-casing-of-the-artist",
            ),
            pytest.param(
                "Clientele", [("The Clientele", "Suburban Light")], ["The Clientele"], id="tolerant"
            ),
            pytest.param(
                "Frames",
                [("A Frames", "Black Forest"), ("The Frames", "Fitzcarraldo")],
                [],
                id="ambiguous-tolerant-rung",
            ),
            pytest.param("Sun Ra", [("Sun Ra Arkestra", "Lanquidity")], [], id="not-shelved"),
            pytest.param(
                "Alaska",
                [("Alaska!", "Emotions"), ("Alaska", "Emotions")],
                ["Alaska", "Alaska!"],
                id="exact-then-punctuation-variant",
            ),
            pytest.param(
                "Alaska!",
                [("Alaska", "Emotions"), ("Alaska!", "Emotions")],
                ["Alaska!", "Alaska"],
                id="variant-typed-first",
            ),
            pytest.param(
                "Girls",
                [("Girls", "Album"), ("The Girls", "Other")],
                ["Girls"],
                id="article-stays-strict-girls",
            ),
            pytest.param(
                "The Girls",
                [("Girls", "Album"), ("The Girls", "Other")],
                ["The Girls"],
                id="article-stays-strict-the-girls",
            ),
            pytest.param(
                "Alaska",
                [("Alaska", "Emotions"), ("The Alaska!", "Other")],
                ["Alaska"],
                id="variant-with-article-is-another-act",
            ),
        ],
    )
    async def test_artist_spellings(self, tmp_path, typed, catalog, expected):
        db = await make_library_catalog(tmp_path, catalog)
        try:
            spellings = await artist_spellings(db, typed)
        finally:
            await db.close()

        assert sorted(spellings) == sorted(expected)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("typed", ["Alaska", "Alaska!"])
    async def test_typed_spelling_leads_its_variants(self, tmp_path, typed):
        db = await make_library_catalog(tmp_path, [("Alaska!", "Emotions"), ("Alaska", "Emotions")])
        try:
            spellings = await artist_spellings(db, typed)
        finally:
            await db.close()

        assert spellings[0].lower() == typed.lower() and len(spellings) == 2
