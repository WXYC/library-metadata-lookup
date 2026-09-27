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
"""

import pytest

from lookup.matching import album_title_acceptable
from lookup.title_identity import (
    _va_series_title_match,
    title_token_gate_rejects,
    titles_differ_by_discriminating_token,
    volume_identifier,
    volume_identifiers,
)
from tests.factories import make_library_item

_SIBLINGS = ("art of field recording volume i", "art of field recording, vol. 2")


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
        assert volume_identifier(title) == expected

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
        assert volume_identifier(title) is None


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
        assert titles_differ_by_discriminating_token(left, right) is True

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
        assert titles_differ_by_discriminating_token(left, right) is False


class TestTheFlagGatesTheVeto:
    def test_default_off_never_rejects(self):
        assert title_token_gate_rejects(*_SIBLINGS) is False

    def test_on_rejects_siblings(self, enable_title_token_identity_gate):
        assert title_token_gate_rejects(*_SIBLINGS) is True


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
    def test_accepted_titles_stay_accepted(self, enable_title_token_identity_gate, query, result):
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


class TestVaSeriesTitleMatch:
    """``_va_series_title_match`` is reached as an ``or`` arm in
    ``search_album_fuzzy``, so it bypasses ``album_title_acceptable``
    entirely. Within a series it admitted unconditionally -- the gate has to
    be in this arm too or the fix routes around itself."""

    def _row(self, title):
        return make_library_item(
            id=1, artist="Various Artists - Folk - A", title=title, genre="Folk"
        )

    def test_flag_off_is_todays_behavior_siblings_included(self):
        assert _va_series_title_match(_SIBLINGS[0], self._row("Art of Field Recording, vol. 2"))

    def test_stays_a_pure_parser_with_the_gate_on(self, enable_title_token_identity_gate):
        """Review F8: the #531 arm has exactly one caller, ``search_album_fuzzy``,
        which ``or``s it with ``album_title_acceptable``. The gate belongs at
        that call site, once, ahead of the ``or`` -- not inside a parser that
        then carries a Settings coupling for no second caller. So with the
        gate ON this arm still admits the sibling; ``search_album_fuzzy``
        (below) is where it is refused."""
        assert _va_series_title_match(_SIBLINGS[0], self._row("Art of Field Recording, vol. 2"))

    def test_matching_volume_still_admitted(self, enable_title_token_identity_gate):
        assert _va_series_title_match(_SIBLINGS[0], self._row("Art of Field Recording, vol. 1"))

    def test_lml531_recall_case_still_admitted(self, enable_title_token_identity_gate):
        assert _va_series_title_match(
            "disco not disco (post punk, electro & leftfield disco classics 1974-1986)",
            self._row("Disco Not Disco, vol. 1"),
        )

    def test_non_compilation_artist_still_excluded(self, enable_title_token_identity_gate):
        """The LML#717 guard: this arm is only for V/A rows. A same-shaped
        non-V/A row must still be refused here, whatever the titles say."""
        assert (
            _va_series_title_match(
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
        assert volume_identifier(title) == expected

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
        assert volume_identifier(title) is None

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
        ],
    )
    def test_volume_identifiers_is_the_full_set(self, title, expected):
        assert volume_identifiers(title) == frozenset(expected)

    def test_single_identifier_is_none_for_a_multi_volume_phrase(self):
        """``volume_identifier`` answers "which one volume?"; a two-volume set
        has no one answer, and a caller that wants the set asks for it."""
        assert volume_identifier("nuggets vol. 1 & 2") is None


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
            # F5 at the predicate: mixed case must not read as one-sided.
            ("Art Of Field Recording Volume I", "Art of Field Recording, Vol. 1"),
        ],
    )
    def test_membership_is_agreement(self, left, right):
        assert titles_differ_by_discriminating_token(left, right) is False

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("nuggets vol. 1 & 2", "nuggets vol. 3"),
            ("nuggets vols. 1-2", "nuggets, vol. 4"),
            ("Art Of Field Recording Volume I", "Art of Field Recording, Vol. 2"),
        ],
    )
    def test_disjoint_sets_disagree(self, left, right):
        assert titles_differ_by_discriminating_token(left, right) is True
