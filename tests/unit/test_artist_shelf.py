"""``lookup/artist_shelf.py`` against a real SQLite catalog (LML#1406).

A real ``library_fts`` index, built with the repo's own DDL, rather than a
mocked ``LibraryDB``: the defect this module fixes lived in the gap between
what the full-text index returns and what the lane keeps, which a mock cannot
show.
"""

from unittest.mock import AsyncMock

import pytest

from lookup.artist_shelf import artist_spellings, rows_for_artist
from lookup.matching import _FETCH_LIMIT
from tests.factories import crowd_rows, make_library_catalog, make_library_item, shelve


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
                ["Alaska!", "Alaska?"],
                id="punctuation-rung-matching-one-act-on-one-call-letters",
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

    @pytest.mark.asyncio
    async def test_the_typed_spelling_comes_first_whatever_the_catalog_order(self, tmp_path):
        db = await make_library_catalog(
            tmp_path, [("STEREOLAB", "Emperor Tomato Ketchup"), ("Stereolab", "Dots and Loops")]
        )
        try:
            spellings = await artist_spellings(db, "Stereolab")
        finally:
            await db.close()

        assert spellings == ["Stereolab", "STEREOLAB"]


class TestPunctuationVariants:
    """One act filed under spellings that differ only in punctuation, on the
    same call letters (LML#1449); a different act on its own letters."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            pytest.param("Mark Almond", ["Mark Almond"], id="solo-records-only"),
            pytest.param("Mark-Almond", ["Mark-Almond"], id="band-only"),
            pytest.param("Mark_Almond", [], id="tolerant-rung-still-ambiguous"),
        ],
    )
    async def test_a_variant_on_other_call_letters_is_another_act(self, tmp_path, typed, expected):
        """Marc Almond's solo records are filed as "Mark Almond" under AL; the
        1970s band Mark-Almond is filed under MA."""
        db = await make_library_catalog(
            tmp_path,
            [("Mark-Almond", "Rising"), ("Mark Almond", "The Stars We Are")],
            call_letters={"Mark Almond": "AL", "Mark-Almond": "MA"},
        )
        try:
            rows = await rows_for_artist(db, typed)
        finally:
            await db.close()

        assert [row.artist for row in rows] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            pytest.param("Mark Almond", ["The Stars We Are"], id="one-letter-set"),
            pytest.param("Mark-Almond", ["Rising", "To the Heart"], id="two-letter-set"),
        ],
    )
    async def test_sharing_one_call_letter_is_not_the_same_letters(self, tmp_path, typed, expected):
        """The band has one row misfiled under AL beside its MA rows: {AL, MA}
        is not {AL}, so the two spellings stay two acts."""
        db = await make_library_catalog(
            tmp_path,
            [
                ("Mark-Almond", "Rising"),
                ("Mark Almond", "The Stars We Are"),
                ("Mark-Almond", "To the Heart"),
            ],
            call_letters={
                "Mark Almond": "AL",
                "Mark-Almond": "MA",
                ("Mark-Almond", "Rising"): "AL",
            },
        )
        try:
            rows = await rows_for_artist(db, typed)
        finally:
            await db.close()

        assert [row.title for row in rows] == expected

    @pytest.mark.asyncio
    async def test_an_unstored_spelling_reads_both_spellings_of_one_act(self, tmp_path):
        """ "Alaska?" matches "Alaska" and "Alaska!" on the punctuation rung: two
        rung-1 keys, one act on one call letters, so not ambiguous."""
        db = await make_library_catalog(tmp_path, [("Alaska!", "Wide Awake"), ("Alaska", "Rescue")])
        try:
            rows = await rows_for_artist(db, "Alaska?")
        finally:
            await db.close()

        assert [(row.artist, row.title) for row in rows] == [
            ("Alaska", "Rescue"),
            ("Alaska!", "Wide Awake"),
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("catalog", "queried"),
        [
            pytest.param([("Stereolab", "Dots and Loops")], False, id="no-sibling-no-query"),
            pytest.param(
                [("STEREOLAB", "Mars Audiac Quintet"), ("Stereolab", "Dots and Loops")],
                False,
                id="case-sibling-no-query",
            ),
            pytest.param(
                [("Stereolab", "Dots and Loops"), ("Stereolab!", "Margerine Eclipse")],
                True,
                id="punctuation-sibling-queries",
            ),
        ],
    )
    async def test_call_letters_are_read_only_for_a_punctuation_sibling(self, catalog, queried):
        db = shelve(
            AsyncMock(),
            [make_library_item(id=i, artist=a, title=t) for i, (a, t) in enumerate(catalog)],
        )

        await artist_spellings(db, "Stereolab")

        assert db.artist_call_letters.await_count == int(queried)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            pytest.param(
                "Cherry Point",
                [
                    ("The Cherry Point", "Night of the Bloody Tapes"),
                    ("The Cherry Point,", "Black Witchery"),
                ],
                id="article-rung-reads-the-variant",
            ),
            pytest.param(
                "The Cherry Point,",
                [
                    ("The Cherry Point,", "Black Witchery"),
                    ("The Cherry Point", "Night of the Bloody Tapes"),
                ],
                id="typed-spelling-leads-its-variant",
            ),
            pytest.param(
                "The Cherry Point",
                [
                    ("The Cherry Point", "Night of the Bloody Tapes"),
                    ("The Cherry Point,", "Black Witchery"),
                ],
                id="variant-filed-first-still-follows",
            ),
            pytest.param(
                "Cherry Point!",
                [
                    ("The Cherry Point", "Night of the Bloody Tapes"),
                    ("The Cherry Point,", "Black Witchery"),
                ],
                id="unstored-spelling-reads-the-whole-act",
            ),
        ],
    )
    async def test_variant_rows_follow_the_picked_spellings_rows(self, tmp_path, typed, expected):
        db = await make_library_catalog(
            tmp_path,
            [
                ("The Cherry Point,", "Black Witchery"),
                ("The Cherry Point", "Night of the Bloody Tapes"),
            ],
        )
        try:
            rows = await rows_for_artist(db, typed)
        finally:
            await db.close()

        assert [(row.artist, row.title) for row in rows] == expected
