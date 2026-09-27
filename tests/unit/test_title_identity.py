"""LML#1369: the album-title gates must reject on token identity, not ratio.

``fuzz.ratio`` between "Art Of Field Recording Volume I" and "Art of Field
Recording, vol. 2" is 87.1 -- the token that says *which release this is* is
one character inside a thirty-character title, far under any ratio floor. The
tests here pin the other question: do the two titles disagree on a
discriminating token?

Two asymmetries are load-bearing and have their own cases below, because the
naive symmetric rule would break recall rather than improve precision:

* a volume on ONE side only is LML#531's recall case, not a disagreement;
* two titles sharing no token at all have no aligned remainder to reason
  about, and are left to the ratio floors.
"""

import pytest

from lookup.matching import album_title_acceptable
from lookup.title_identity import (
    _va_series_title_match,
    titles_differ_by_discriminating_token,
    volume_identifier,
)
from tests.factories import make_library_item


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
        ],
    )
    def test_recognised_volume_forms(self, title, expected):
        assert volume_identifier(title) == expected

    @pytest.mark.parametrize(
        "title",
        [
            "aluminum tunes",
            # "vol"-prefixed words that are not volume markers.
            "jefferson airplane volunteers",
            "volume dealers",
            # A bare roman numeral with no volume keyword is not a volume.
            "led zeppelin iv",
        ],
    )
    def test_titles_without_a_volume_identifier(self, title):
        assert volume_identifier(title) is None


class TestDiscriminatingTokenDisagreement:
    """The core predicate: both sides carry a distinguishing token, and the
    tokens differ."""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            # Shape A -- volume siblings, in every spelling pair the catalog
            # actually holds, and in both directions.
            ("art of field recording volume i", "art of field recording, vol. 2"),
            ("art of field recording, vol. 2", "art of field recording volume i"),
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
            # Shape C -- a single discriminating word, the prod case and the
            # shortened Discogs form the issue measured at ratio 85.7.
            (
                "the monterey international pop festival",
                "the international guitar festival",
            ),
            ("the international pop festival", "the international guitar festival"),
            # LML#24's corpus, now rejected on token identity rather than on
            # the <=5-char remainder guard that could not reach volume titles.
            ("chicago 16", "chicago v"),
            ("led zeppelin iv", "led zeppelin ii"),
        ],
    )
    def test_one_discriminating_word_disagrees(self, left, right):
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
            # One-sided suffixes.
            ("aluminum tunes", "aluminum tunes (remastered)"),
            ("moon pix", "moon pix"),
            # A spelling variant is not a discriminating token.
            ("aluminum tunes", "aluminium tunes"),
            # No shared token at all: nothing to align, leave it to the ratio.
            ("doggystyle", "doggy style"),
            # A two-digit year abbreviation is the same year.
            ("atlantic rhythm and blues 1947-1974", "atlantic rhythm and blues 1947-74"),
        ],
    )
    def test_agreeing_titles_are_not_rejected(self, left, right):
        assert titles_differ_by_discriminating_token(left, right) is False


class TestAlbumTitleAcceptableInheritsTheGate:
    """The shared matcher is where the fix lands, so every caller of
    ``album_title_acceptable`` -- ``SONG_AS_TRACK`` and
    ``track_release_matching`` included -- inherits it."""

    def test_volume_siblings_rejected(self):
        assert (
            album_title_acceptable(
                "art of field recording volume i", "art of field recording, vol. 2"
            )
            is False
        )

    def test_volume_one_is_not_a_prefix_of_volume_ten(self):
        """The prefix branch returns True for any title that literally starts
        with the query, which ``pebbles, volume 1`` does against ``pebbles,
        volume 10``. The token gate has to run before it."""
        assert album_title_acceptable("pebbles, volume 1", "pebbles, volume 10") is False

    def test_shape_c_single_word_rejected(self):
        assert (
            album_title_acceptable(
                "the monterey international pop festival", "the international guitar festival"
            )
            is False
        )

    @pytest.mark.parametrize(
        ("query", "result"),
        [
            # LML#24 must-keeps.
            ("chicago 16", "chicago v"),
            ("chicago 16", "chicago ix"),
            ("led zeppelin iv", "led zeppelin ii"),
        ],
    )
    def test_lml24_corpus_still_rejected(self, query, result):
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
    def test_accepted_titles_stay_accepted(self, query, result):
        assert album_title_acceptable(query, result) is True


class TestVaSeriesTitleMatchInheritsTheGate:
    """``_va_series_title_match`` is reached as an ``or`` arm in
    ``search_album_fuzzy``, so it bypasses ``album_title_acceptable``
    entirely. Within a series it admitted unconditionally -- the gate has to
    be in this arm too or the fix routes around itself."""

    def _row(self, title):
        return make_library_item(
            id=1, artist="Various Artists - Folk - A", title=title, genre="Folk"
        )

    def test_sibling_volume_no_longer_admitted(self):
        assert (
            _va_series_title_match(
                "art of field recording volume i", self._row("Art of Field Recording, vol. 2")
            )
            is False
        )

    def test_matching_volume_still_admitted(self):
        assert (
            _va_series_title_match(
                "art of field recording volume i", self._row("Art of Field Recording, vol. 1")
            )
            is True
        )

    def test_lml531_recall_case_still_admitted(self):
        assert (
            _va_series_title_match(
                "disco not disco (post punk, electro & leftfield disco classics 1974-1986)",
                self._row("Disco Not Disco, vol. 1"),
            )
            is True
        )

    def test_non_compilation_artist_still_excluded(self):
        """The LML#717 guard: this arm is only for V/A rows. A same-shaped
        non-V/A row must still be refused here, whatever the titles say."""
        assert (
            _va_series_title_match(
                "live sessions (acoustic recordings from the greek theatre 1998-2002)",
                make_library_item(id=2, artist="Some Band", title="Live Sessions, vol. 2"),
            )
            is False
        )
