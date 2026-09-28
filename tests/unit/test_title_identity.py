"""LML#1369 (volume axis): the album-title gates must reject a sibling volume.

``fuzz.ratio`` between "Art Of Field Recording Volume I" and "Art of Field
Recording, vol. 2" is 87.1 -- the token that says *which release this is* is
one character inside a thirty-character title, far under any ratio floor. The
tests here pin the question the gates should ask instead: do the two titles
carry *different* volume identifiers?

Two things are pinned with equal weight to the rejection itself:

* the gate is behind ``LML_TITLE_TOKEN_IDENTITY_GATE`` and default OFF -- with
  the flag off every existing verdict is byte-for-byte today's, siblings
  included -- because the flip waits on a prod recall measurement;
* a volume on ONE side only is LML#531's recall case, not a disagreement, and
  keeps matching with the flag on.

The word axis sits on the nested ``LML_TITLE_WORD_IDENTITY_GATE`` (LML#1382),
and both flags on rejects a superset of the master alone. So a volume
must-reject test requests the master only (the first rollout stage), while a
word-axis test or a must-not-reject test requests both -- each the strongest
form of its claim.
"""

import pytest

from lookup.matching import album_title_acceptable
from lookup.title_identity import (
    title_token_gate_rejects,
    titles_name_different_releases,
    titles_name_different_volumes,
    va_series_base,
    va_series_title_match,
    volume_identifiers,
)
from tests.factories import make_library_item

_SIBLINGS = ("art of field recording volume i", "art of field recording, vol. 2")
# LML#1369 shape C: one discriminating word on each side, no volume on either.
_SHAPE_C = ("the monterey international pop festival", "the international guitar festival")
_STUDIO_ONE = (
    "soul jazz records presents studio one rockers",
    "soul jazz records presents studio one roots",
)


class TestVolumeIdentifier:
    """The library writes ``vol. 2`` where Discogs writes ``Volume II``, and
    this catalog also holds rows spelled ``Volume One`` / ``Volume three`` /
    ``Volume Six`` / ``Volume Seven``. All spellings must fold to one value."""

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("art of field recording, vol. 2", "2"),
            ("art of field recording vol 2", "2"),
            ("art of field recording vol.2", "2"),
            ("art of field recording volume 2", "2"),
            ("art of field recording volume ii", "2"),
            ("art of field recording volume two", "2"),
            ("jean redpath, volume one", "1"),
            ("jean redpath, volume three", "3"),
            ("jean redpath, volume six", "6"),
            ("jean redpath, volume seven", "7"),
            ("pebbles, volume 10", "10"),
            ("pebbles, volume x", "10"),
            ("secret museum of mankind, vol. 2a", "2a"),
            # Mid-title, the Discogs habit: "<base> Volume I: <subtitle>".
            ("art of field recording volume i: fifty years of traditional music", "1"),
        ],
    )
    def test_recognised_volume_forms(self, title, expected):
        assert volume_identifiers(title) == {expected}

    @pytest.mark.parametrize(
        "title",
        [
            "aluminum tunes",
            # "vol"-initial words that are not volume markers.
            "jefferson airplane volunteers",
            "volume dealers",
            # A bare roman numeral with no volume keyword is not a volume.
            "led zeppelin iv",
            # A permissive roman accumulator reads "livid" as 443; the strict
            # canonical-form check must not.
            "vol. livid",
        ],
    )
    def test_titles_without_a_volume_identifier(self, title):
        assert volume_identifiers(title) == frozenset()


class TestVolumeDisagreement:
    """The pure predicate, flag-independent."""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            _SIBLINGS,
            tuple(reversed(_SIBLINGS)),
            ("art of field recording volume one", "art of field recording, vol. 2"),
            ("art of field recording, vol. 2", "art of field recording volume three"),
            ("jean redpath volume six", "jean redpath volume seven"),
            ("pebbles, volume 1", "pebbles, volume 10"),
            ("the r&b box, vol. 3", "the r&b box volume v"),
        ],
    )
    def test_volume_siblings_disagree(self, left, right):
        assert titles_name_different_volumes(left, right) is True

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            # LML#531's recall case: the volume is information the library has
            # and Discogs does not, not a contradiction.
            (
                "disco not disco (post punk, electro & leftfield disco classics 1974-1986)",
                "disco not disco, vol. 1",
            ),
            ("disco not disco", "disco not disco, vol. 2"),
            # Same volume, divergent spellings.
            ("art of field recording volume i", "art of field recording, vol. 1"),
            ("art of field recording volume two", "art of field recording, vol. 2"),
            ("jean redpath volume seven", "jean redpath, vol. 7"),
            # No volume anywhere.
            ("aluminum tunes", "aluminum tunes (remastered)"),
            ("moon pix", "moon pix"),
        ],
    )
    def test_agreeing_titles_are_not_rejected(self, left, right):
        assert titles_name_different_volumes(left, right) is False


class TestTheFlagGatesTheVeto:
    def test_default_off_never_rejects(self):
        assert title_token_gate_rejects(*_SIBLINGS) is False

    def test_on_rejects_siblings(self, enable_title_token_identity_gate):
        assert title_token_gate_rejects(*_SIBLINGS) is True

    @pytest.mark.parametrize(
        ("master", "word", "pair", "rejects"),
        [
            (False, False, _SIBLINGS, False),
            (False, False, _SHAPE_C, False),
            # The word flag is nested: on its own it changes nothing.
            (False, True, _SIBLINGS, False),
            (False, True, _SHAPE_C, False),
            # Master alone is the volume axis only -- the first rollout stage.
            (True, False, _SIBLINGS, True),
            (True, False, _SHAPE_C, False),
            # Both is the composed verdict, volume and word axes together.
            (True, True, _SIBLINGS, True),
            (True, True, _SHAPE_C, True),
        ],
    )
    def test_word_axis_is_nested_under_the_master(
        self, set_title_gate_flags, master, word, pair, rejects
    ):
        """LML#1382: the word axis is hand-curated where the volume axis was
        measured, so it rides its own flag -- a prod recall drop can then be
        attributed to one axis, and rolling the word axis back keeps the
        volume axis. The composed verdict itself stays flag-independent."""
        set_title_gate_flags(master=master, word=word)

        assert title_token_gate_rejects(*pair) is rejects
        assert titles_name_different_releases(*pair) is True

    @pytest.mark.parametrize(
        "pair",
        [
            (
                "super hits of the 70s: have a nice day, vol. 12",
                "super hits of the 70s, vol 1-24 (a-x)",
            ),
            (
                "super hits of the 70s, vol 1-24 (a-x)",
                "super hits of the 70s: have a nice day, vol. 12",
            ),
        ],
    )
    def test_an_unexpandable_range_never_rejects_at_stage_1(self, set_title_gate_flags, pair):
        """LML#1382 stage-1 replay: the 24-volume box's one library row read as
        vol. 1, so the master gate disjoint-rejected it for every query naming
        volumes 7-24 (18 of 3,248 sampled flowsheet lookups). The master alone
        is the rollout stage that must not; the word axis is judged on its own."""
        set_title_gate_flags(master=True, word=False)

        assert title_token_gate_rejects(*pair) is False

    @pytest.mark.parametrize("word", [False, True])
    def test_every_gate_is_volume_only_with_the_master_alone(self, set_title_gate_flags, word):
        """Stage 1 must hold at all three gates, not just the shared helper: a
        gate that called the composed verdict directly would apply the word
        axis with the master alone. Each pair clears its gate's own floors with
        every flag off (the Studio One pair scores 93 / 0.956 on the carve-out),
        so only the word axis can refuse it."""
        from lookup.compilation_title_floor import compilation_title_carveout_verdict

        set_title_gate_flags(master=True, word=word)

        assert album_title_acceptable(*_SHAPE_C) is not word
        assert compilation_title_carveout_verdict(*_STUDIO_ONE).admitted is not word


class TestAlbumTitleAcceptable:
    """The shared matcher is where the fix lands, so every caller of
    ``album_title_acceptable`` -- ``SONG_AS_TRACK`` and
    ``track_release_matching`` included -- inherits it."""

    def test_flag_off_is_todays_behavior_siblings_included(self):
        """Byte-for-byte pin of the pre-#1369 verdict: 87.1 clears the floor."""
        assert album_title_acceptable(*_SIBLINGS) is True

    def test_volume_siblings_rejected(self, enable_title_token_identity_gate):
        assert album_title_acceptable(*_SIBLINGS) is False

    def test_volume_one_is_not_a_prefix_of_volume_ten(self, enable_title_token_identity_gate):
        """The prefix branch returns True for any title that literally starts
        with the query, which ``pebbles, volume 1`` does against ``pebbles,
        volume 10``. The gate has to run before it."""
        assert album_title_acceptable("pebbles, volume 1", "pebbles, volume 10") is False

    @pytest.mark.parametrize(
        ("query", "result"),
        [
            ("chicago 16", "chicago v"),
            ("chicago 16", "chicago ix"),
            ("led zeppelin iv", "led zeppelin ii"),
        ],
    )
    def test_lml24_corpus_still_rejected(self, enable_title_token_identity_gate, query, result):
        assert album_title_acceptable(query, result) is False

    @pytest.mark.parametrize(
        ("query", "result"),
        [
            ("chicago 16", "chicago 16"),
            ("aluminum tunes", "aluminum tunes (remastered)"),
            ("rumours", "rumors"),
            ("dark side of the moon", "the dark side of the moon"),
            # LML#531's recall case must still clear the gate.
            ("disco not disco", "disco not disco, vol. 1"),
            ("art of field recording volume i", "art of field recording, vol. 1"),
        ],
    )
    def test_accepted_titles_stay_accepted(
        self, enable_title_token_identity_gate, enable_title_word_identity_gate, query, result
    ):
        assert album_title_acceptable(query, result) is True


class TestSearchAlbumFuzzyGatesBothArms:
    """Review F8: one ``title_token_gate_rejects`` filter in
    ``_search_and_filter`` ahead of the ``or`` covers both the #531 V/A arm
    and ``album_title_acceptable`` -- and is the only place the V/A arm is
    gated (see ``TestVaSeriesTitleMatch``)."""

    def _db(self):
        from unittest.mock import AsyncMock

        rows = [
            make_library_item(
                id=58620,
                artist="Various Artists - Folk - A",
                title="Art of Field Recording, vol. 1",
            ),
            make_library_item(
                id=58621,
                artist="Various Artists - Folk - A",
                title="Art of Field Recording, vol. 2",
            ),
        ]
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=rows)
        return db

    @pytest.mark.asyncio
    async def test_flag_off_admits_both_va_rows(self):
        from lookup.strategies.track_release_matching import search_album_fuzzy

        results = await search_album_fuzzy(self._db(), "Art Of Field Recording Volume I")

        assert {r.id for r in results} == {58620, 58621}

    @pytest.mark.asyncio
    async def test_flag_on_refuses_the_sibling_through_the_va_arm(
        self, enable_title_token_identity_gate
    ):
        from lookup.strategies.track_release_matching import search_album_fuzzy

        results = await search_album_fuzzy(self._db(), "Art Of Field Recording Volume I")

        assert {r.id for r in results} == {58620}

    def _paren_db(self):
        """FTS5 answers nothing for a title carrying ``(``; the paren-strip
        retry's plain query answers with both volumes."""
        from unittest.mock import AsyncMock

        rows = [
            make_library_item(id=1, artist="Various Artists", title="Pebbles, volume 1"),
            make_library_item(id=10, artist="Various Artists", title="Pebbles, volume 10"),
        ]
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(side_effect=lambda query, limit: [] if "(" in query else rows)
        return db

    @pytest.mark.asyncio
    async def test_flag_off_paren_strip_retry_is_todays_behavior(self):
        from lookup.strategies.track_release_matching import search_album_fuzzy

        results = await search_album_fuzzy(self._paren_db(), "Pebbles (Volume 10)")

        assert {r.id for r in results} == {1, 10}

    @pytest.mark.asyncio
    async def test_flag_on_gates_the_paren_strip_retry_on_the_full_title(
        self, enable_title_token_identity_gate
    ):
        """The retry strips the parenthetical before searching, and the gate
        used to judge that stripped query -- ``pebbles`` against ``pebbles,
        volume 1`` is one-sided, so the #531 arm admitted the sibling and the
        strategy bound Volume 10's release to it. The gate judges the full
        Discogs title, whichever query retrieved the row."""
        from lookup.strategies.track_release_matching import search_album_fuzzy

        results = await search_album_fuzzy(self._paren_db(), "Pebbles (Volume 10)")

        assert {r.id for r in results} == {10}


class TestVaSeriesTitleMatch:
    """``va_series_title_match`` is reached as an ``or`` arm in
    ``search_album_fuzzy``, so it bypasses ``album_title_acceptable``
    entirely. Within a series it admitted unconditionally -- the gate has to
    be in this arm too or the fix routes around itself."""

    def _row(self, title):
        return make_library_item(
            id=1, artist="Various Artists - Folk - A", title=title, genre="Folk"
        )

    def test_flag_off_is_todays_behavior_siblings_included(self):
        assert va_series_title_match(_SIBLINGS[0], self._row("Art of Field Recording, vol. 2"))

    def test_stays_a_pure_parser_with_the_gate_on(self, enable_title_token_identity_gate):
        """Review F8: the #531 arm has exactly one caller, ``search_album_fuzzy``,
        which ``or``s it with ``album_title_acceptable``. The gate belongs at
        that call site, once, ahead of the ``or`` -- not inside a parser that
        then carries a Settings coupling for no second caller. So with the
        gate ON this arm still admits the sibling; ``search_album_fuzzy``
        (below) is where it is refused."""
        assert va_series_title_match(_SIBLINGS[0], self._row("Art of Field Recording, vol. 2"))

    def test_matching_volume_still_admitted(self, enable_title_token_identity_gate):
        assert va_series_title_match(_SIBLINGS[0], self._row("Art of Field Recording, vol. 1"))

    def test_lml531_recall_case_still_admitted(self, enable_title_token_identity_gate):
        assert va_series_title_match(
            "disco not disco (post punk, electro & leftfield disco classics 1974-1986)",
            self._row("Disco Not Disco, vol. 1"),
        )

    def test_non_compilation_artist_still_excluded(self, enable_title_token_identity_gate):
        """The LML#717 guard: this arm is only for V/A rows. A same-shaped
        non-V/A row must still be refused here, whatever the titles say."""
        assert (
            va_series_title_match(
                "live sessions (acoustic recordings from the greek theatre 1998-2002)",
                make_library_item(id=2, artist="Some Band", title="Live Sessions, vol. 2"),
            )
            is False
        )


class TestVolumeIdentifierReviewFindings:
    """Pins from the #1377 review: the volume parser must be case-insensitive
    (F5), must not read a lone letter or a real word as a roman numeral (F9),
    and must read a multi-volume phrase as the set it names (F4)."""

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            # F5: every caller happens to lower() first; nothing enforced it,
            # and a one-sided None reads as "no disagreement".
            ("Art Of Field Recording Volume II", "2"),
            ("Art of Field Recording, Vol. 2", "2"),
            ("PEBBLES VOL.10", "10"),
        ],
    )
    def test_case_insensitive(self, title, expected):
        assert volume_identifiers(title) == {expected}

    @pytest.mark.parametrize(
        "title",
        [
            # F9: canonical roman accepted single letters and real words --
            # "vol. c" read as 100, "vol. mix" as 1009, "vol. cd" as 400.
            "sun ra vol. c",
            "vol. mix",
            "vol. cd",
            "vol. l",
            "vol. d",
            "vol. m",
            # A lettered series (A-G, the LIBRARY_RELEASE.CALL_LETTERS shape) is
            # not a numbered one; adjudicate none of it rather than some of it.
            "atlantic rhythm and blues vol. a",
            "atlantic rhythm and blues vol. b",
        ],
    )
    def test_letters_and_words_are_not_roman_volumes(self, title):
        assert volume_identifiers(title) == frozenset()

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("pebbles volume x", {"10"}),
            ("pebbles volume xiv", {"14"}),
            ("pebbles volume xxxix", {"39"}),
            # F4: a multi-volume release names every volume it contains.
            ("nuggets vol. 1 & 2", {"1", "2"}),
            ("nuggets vols. 1-2", {"1", "2"}),
            ("nuggets vols. 1, 2 and 3", {"1", "2", "3"}),
            ("nuggets vols. 3-5", {"3", "4", "5"}),
            ("nuggets vol. i & ii", {"1", "2"}),
            # The list stops at the first word that is not a volume number.
            ("nuggets vol. 2, the best of", {"2"}),
            ("aluminum tunes", set()),
            # Every phrase counts, not only the first: a two-in-one filed as two
            # phrases (catalog row 16344) names both volumes, and "& vol. 2"
            # restarts the marker rather than continuing the list.
            ("urmur bile trax volume 1 volume 2", {"1", "2"}),
            ("nuggets vol. 1 & vol. 2", {"1", "2"}),
            # A year after the separator is not a further volume, and a year
            # range is not a run of volumes: "vol. 2, 1967-1974" is one volume,
            # not nine. Nine catalog titles carry this shape.
            ("posh hits, vol. 1, 1983", {"1"}),
            ("country funk, vol. 2, 1967-1974", {"2"}),
            ("valaida volume 1, 1935-37", {"1"}),
            # A list continues only in the phrase's own notation: a spelled
            # number opening the subtitle is not a further volume, and a
            # spelled list is still a list. "and"/"to" are whole words.
            ("rare soul vol. 2, one night only", {"2"}),
            ("vol. 1 and one more", {"1"}),
            ("volume one and two", {"1", "2"}),
            ("nuggets vols. i to iii", {"1", "2", "3"}),
            ("vol. i tox", {"1"}),
            # A spelled number past the vocabulary is not its first word.
            ("jean redpath, volume twenty one", set()),
            ("jean redpath, volume twenty-one", set()),
            # A dash that cannot be a run of volumes -- descending, or a letter
            # suffix -- ends the phrase at its first identifier: "12-3" joins a
            # catalog or disc number to volume 12, not volumes.
            ("series vol. 12-3", {"12"}),
            ("series vol. 2a-3", {"2a"}),
            # An ascending run too wide to expand names NO volume (LML#1382): a
            # box set ("vol 1-24", catalog row 47607) and a catalog number
            # ("vol. 2-63") are the same shape, and reading the low end rejected
            # the box set against its own volumes 2-24. The widest run expands.
            ("super hits of the 70s, vol 1-24 (a-x)", set()),
            ("Super Hits of the 70s, vol 1-24 (a-x)", set()),
            ("series vol. 2-63", set()),
            ("series vols. 1-100", set()),
            ("series vols. 1, 2-40", set()),
            ("series vols. 1-22", set()),
            ("series vols. 1-21", {str(n) for n in range(1, 22)}),
            # An unknown phrase makes the whole title unknown: a second phrase
            # must not narrow a box set back to the volumes it happens to spell.
            ("series vol. 1-24, vol. 30", set()),
            ("super hits vol 1-24 & vol. 25", set()),
            # "+" is a list joiner too ("Vol. 1 + 2", a double-set spelling).
            ("nuggets vol. 1 + 2", {"1", "2"}),
            # A spelled range is a range; only "twenty" compounds with a units word.
            ("series volumes one-two", {"1", "2"}),
            ("series volume one - two", {"1", "2"}),
        ],
    )
    def test_volume_identifiers_is_the_full_set(self, title, expected):
        assert volume_identifiers(title) == frozenset(expected)


class TestMultiVolumeDisagreement:
    """F4, at the predicate: membership is agreement. A library vol. 2 row is
    *contained* in Discogs's two-volume set and must not be rejected against
    it, while a volume the set does not contain still is."""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("nuggets vol. 1 & 2", "nuggets vol. 2"),
            ("nuggets vol. 2", "nuggets vol. 1 & 2"),
            ("nuggets vols. 1-2", "nuggets, vol. 1"),
            ("nuggets vols. 1-3", "nuggets, vol. 2"),
            ("nuggets vol. 1 + 2", "nuggets, vol. 2"),
            # F5 at the predicate: mixed case must not read as one-sided.
            ("Art Of Field Recording Volume I", "Art of Field Recording, Vol. 1"),
            # A second volume phrase is part of the set: the two-in-one row
            # agrees with either of its volumes.
            ("urmur bile trax volume 1 volume 2", "urmur bile trax volume 2"),
            ("nuggets vol. 1 & vol. 2", "nuggets, vol. 2"),
            # Past-vocabulary spellings read as no volume, never as their
            # first word: a one-sided None is agreement, not a false reject.
            ("jean redpath, volume twenty one", "jean redpath volume 21"),
            # An unexpandable range reads as no volume, so it agrees with every
            # volume -- its own (LML#1382) and, the accepted cost, a catalog
            # number's second half.
            ("super hits of the 70s, vol 1-24 (a-x)", "super hits of the 70s, vol. 12"),
            ("series, vol. 63", "series vol. 2-63"),
            # Review of #1389: emptying only the unexpandable phrase left the
            # other phrase's volume, which newly disagreed with the low end.
            ("series vol. 1-24, vol. 30", "series vol. 1"),
            ("series vol. 30, vol. 1-24", "series, vol. 1"),
            ("super hits vol 1-24 & vol. 25", "super hits vol. 12"),
        ],
    )
    def test_membership_is_agreement(self, left, right):
        assert titles_name_different_volumes(left, right) is False

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("nuggets vol. 1 & 2", "nuggets vol. 3"),
            ("nuggets vols. 1-2", "nuggets, vol. 4"),
            ("Art Of Field Recording Volume I", "Art of Field Recording, Vol. 2"),
            # Overlapping year spans must not read as shared volumes.
            ("country funk, vol. 1, 1969-1975", "country funk, vol. 2, 1967-1974"),
            # A subtitle's number word must not smuggle the sibling into the set.
            ("rare soul, vol. 1", "rare soul vol. 2, one night only"),
            # A catalog-number dash must not assert its second half as a volume.
            ("series, vol. 3", "series vol. 12-3"),
        ],
    )
    def test_disjoint_sets_disagree(self, left, right):
        assert titles_name_different_volumes(left, right) is True


class TestVaSeriesBaseIsOneDefinitionOfAVolume:
    """Review F5/F9 acceptance: the #531 series parser and the #1369 volume
    parser must agree on what a volume is. ``va_series_base`` now recovers the
    base through the same phrase parser ``volume_identifiers`` uses -- the
    phrase must run to the end of the title -- so the two cannot drift.

    The table covers the forms the #531 docstring itself listed and had
    pinned only one of (``, vol. N``), plus the shapes the finders showed the
    old suffix regex mis-parsing: no space after the dot (41 catalog titles,
    26 of them V/A rows), a dash or colon separator surviving into the base,
    and any ``\\w+`` tail being read as a volume ("low volume music" -> "low").
    """

    @pytest.mark.parametrize(
        ("title", "base"),
        [
            # The #531 docstring's own forms.
            ("disco not disco, vol. 1", "disco not disco"),
            ("disco not disco, volume 1", "disco not disco"),
            ("disco not disco vol. 1", "disco not disco"),
            ("disco not disco volume 1", "disco not disco"),
            ("disco not disco vol 1", "disco not disco"),
            ("disco not disco, vol. iii", "disco not disco"),
            ("disco not disco, vol. 2a", "disco not disco"),
            ("jean redpath, volume seven", "jean redpath"),
            # F5 addendum: no space after the dot ("Aerial, vol.2" .. "vol.6").
            ("disco not disco, vol.2", "disco not disco"),
            ("disco not disco, vol.ii", "disco not disco"),
            ("aerial, vol.2", "aerial"),
            # A dash or colon separator must not survive into the base.
            ("disco not disco - vol. 2", "disco not disco"),
            ("disco not disco: vol. 2", "disco not disco"),
            # A multi-volume filing is still a series filing.
            ("nuggets, vols. 1-2", "nuggets"),
            # Case-insensitive, like the rest of the module.
            ("Disco Not Disco, Vol. 2", "disco not disco"),
        ],
    )
    def test_series_filings_parse(self, title, base):
        assert va_series_base(title) == base

    @pytest.mark.parametrize(
        "title",
        [
            # Any word after "volume" used to be a volume.
            "low volume music",
            "turn up the volume now",
            "hits, vol. livid",
            "the volume dealers",
            # An empty base is not a series: nothing before the phrase, or only
            # the separator the base would be stripped of.
            "  vol. 2",
            "volume 2",
            ", vol. 1",
            "",
            # A volume that is not at the end is a title, not a filing.
            "art of field recording volume i: fifty years of traditional music",
            "aluminum tunes",
            # A year after the volume is a tail, not a further volume, so the
            # phrase does not run to the end (LML#531's regex agreed: None).
            "posh hits, vol. 1, 1983",
            "country funk, vol. 2, 1967-1974",
            # An unexpandable range is no filing either: its phrase still ends
            # at the low end, so the dash is a tail (unchanged by LML#1382).
            "super hits of the 70s, vol 1-24 (a-x)",
            "series vols. 1-24",
        ],
    )
    def test_non_series_titles_do_not_parse(self, title):
        assert va_series_base(title) is None

    @pytest.mark.parametrize(
        ("title", "base"),
        [
            (
                "island records 1964-1969: rhythm & blues beat, volume 2 r&b beat vol. 2",
                "island records 1964-1969: rhythm & blues beat, volume 2 r&b beat",
            ),
            ("urmur bile trax volume 1 volume 2", "urmur bile trax volume 1"),
            (
                "sun papa and the fan club orchestra vol. 1 & vol. 2",
                "sun papa and the fan club orchestra vol. 1 &",
            ),
        ],
    )
    def test_a_filing_is_judged_on_its_trailing_phrase(self, title, base):
        """LML#531's suffix regex was ``$``-anchored, so a title carrying two
        volume phrases was a filing on its *last* one. Judging the first phrase
        instead turned catalog row 36837 -- a V/A row the #531 arm admitted --
        into a non-filing: the one flag-independent rejection the F5 addendum
        would otherwise introduce. The base is whatever precedes the trailing
        phrase, exactly as before."""
        assert va_series_base(title) == base


class TestVaSeriesTitleMatchBoundary:
    """Review F18: the base-prefix collision guard is the shared
    ``next_char_is_boundary`` from ``lookup/name_folding.py`` -- the same
    primitive ``article_stem_hit`` uses, so LML#1250's "any non-alphanumeric
    continuation" rule cannot drift between the two again."""

    def _row(self):
        return make_library_item(id=1, artist="Various Artists", title="Disco Not Disco, vol. 1")

    def test_exact_base_is_a_boundary(self):
        assert va_series_title_match("disco not disco", self._row()) is True

    def test_punctuation_continuation_is_a_boundary(self):
        assert va_series_title_match("disco not disco: the collection", self._row()) is True

    def test_letter_continuation_is_not(self):
        assert va_series_title_match("disco not discotheque", self._row()) is False


class TestAlbumTitleAcceptableWordAxis:
    """Shape C through the shared matcher: the word axis composes into the
    same gate the volume axis uses, behind the nested word flag (LML#1382), so
    every caller of ``album_title_acceptable`` inherits it."""

    def test_flag_off_is_todays_behavior_shape_c_included(self):
        assert album_title_acceptable(*_SHAPE_C)

    def test_shape_c_single_word_rejected(
        self, enable_title_token_identity_gate, enable_title_word_identity_gate
    ):
        assert album_title_acceptable(*_SHAPE_C) is False

    @pytest.mark.parametrize(
        ("query", "result"),
        [
            ("kill bill pt. 2", "kill bill part two"),
            ("greatest hits of the 50's", "greatest hits of the 50s"),
            ("hip-hop classics", "hiphop classics"),
        ],
    )
    def test_variant_spellings_stay_accepted(
        self, enable_title_token_identity_gate, enable_title_word_identity_gate, query, result
    ):
        assert album_title_acceptable(query, result) is True


class TestTitleProfileCache:
    """Review F10: with the flag on, ``_filter_release_matches`` compares one
    release title against every row; the release side is profiled once and
    cached, and caching changes no verdict."""

    _PAIRS = [
        ("art of field recording volume i", "art of field recording, vol. 2"),
        ("the monterey international pop festival", "the international guitar festival"),
        ("disco not disco", "disco not disco, vol. 1"),
        ("greatest hits of the 50's", "greatest hits of the 50s & 60s"),
        ("kill bill pt. 2", "kill bill part two"),
    ]

    def test_verdicts_are_identical_cold_and_warm(self):
        from lookup.title_identity import _profile, titles_name_different_releases

        cold = []
        for left, right in self._PAIRS:
            _profile.cache_clear()
            cold.append(titles_name_different_releases(left, right))
        warm = [titles_name_different_releases(left, right) for left, right in self._PAIRS]
        warm_again = [titles_name_different_releases(left, right) for left, right in self._PAIRS]

        assert cold == warm == warm_again == [True, True, False, False, False]

    def test_release_side_is_profiled_once_per_title(self):
        from lookup.title_identity import _profile, titles_name_different_releases

        _profile.cache_clear()
        release = "art of field recording volume i"
        rows = [f"art of field recording, vol. {n}" for n in range(1, 6)]
        for row in rows:
            titles_name_different_releases(release, row)

        info = _profile.cache_info()
        assert info.misses == 1 + len(rows), info
        assert info.hits == len(rows) - 1, info

    def test_master_only_gate_runs_no_word_axis_code(self, set_title_gate_flags):
        """Stage 1 of the LML#1382 rollout (master on, word axis off) is the
        configuration prod runs longest. Its branch parses each distinct title
        once, on a volume-only cache, and never reaches ``_profile`` -- whose
        word tokenizer the word flag exists to take off the path."""
        from lookup.title_identity import _profile, title_token_gate_rejects, volume_identifiers

        set_title_gate_flags(master=True, word=False)
        _profile.cache_clear()
        volume_identifiers.cache_clear()
        release = "art of field recording volume i"
        rows = [f"art of field recording, vol. {n}" for n in range(1, 6)]
        verdicts = [title_token_gate_rejects(release, row) for row in rows]

        assert verdicts == [False, True, True, True, True]
        info = volume_identifiers.cache_info()
        assert info.misses == 1 + len(rows), info
        assert info.hits == len(rows) - 1, info
        assert _profile.cache_info().currsize == 0
