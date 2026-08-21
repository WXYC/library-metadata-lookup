"""Unit tests for ``_select_rowless_artist_release`` / ``_own_release_credit``.

LML#1244 follow-through: the SONG_AS_ARTIST row-less pick (LML#631) gates on
the typed token "normalizing-equal to the resolved Discogs artist name". That
equality runs under the same ``normalize_for_comparison`` (``to_match_form``)
that lowercases and folds diacritics but preserves punctuation -- so a typed
token "Melt Banana" never equals a Discogs credit "Melt-Banana", and the
row-less carry-through drops a release it should surface. This is the same
asymmetry ``artist_matches_item`` had (fixed in ``lookup/matching.py``), on
the non-library half of the same lane.
"""

from lookup.rowless import _select_rowless_artist_release
from tests.factories import make_discogs_result


class TestSelectRowlessArtistReleasePunctuation:
    """Punctuation tolerance for the row-less artist-credit gate (LML#1244)."""

    def test_punctuation_free_token_matches_hyphenated_discogs_credit(self):
        """Typed 'Melt Banana' must select a release credited to 'Melt-Banana'."""
        release = make_discogs_result(
            release_id=37332,
            artist="Melt-Banana",
            album="Cell Scape",
            confidence=0.4,
        )
        result = _select_rowless_artist_release("Melt Banana", [release])

        assert result is not None
        item, resolved = result
        assert item.artist == "Melt-Banana"
        assert item.title == "Cell Scape"
        assert resolved.release_id == 37332

    def test_punctuated_token_matches_punctuation_free_discogs_credit(self):
        """Reverse direction: typed 'Melt-Banana' selects a release credited
        to the punctuation-free 'Melt Banana'."""
        release = make_discogs_result(
            release_id=37332,
            artist="Melt Banana",
            album="Cell Scape",
            confidence=0.4,
        )
        result = _select_rowless_artist_release("Melt-Banana", [release])

        assert result is not None
        item, _resolved = result
        assert item.artist == "Melt Banana"

    def test_self_titled_packed_credit_tolerates_punctuation(self):
        """The LML#663 packed-title recovery path (title-derived-empty
        ``r.artist``, self-titled release packed into ``r.album``) also
        tolerates a punctuation-only difference from the typed token."""
        release = make_discogs_result(
            release_id=999,
            artist="",
            album="Melt-Banana",
            confidence=0.4,
        )
        result = _select_rowless_artist_release("Melt Banana", [release])

        assert result is not None
        item, _resolved = result
        assert item.artist == "Melt-Banana"
        assert item.title == "Melt-Banana"

    def test_packed_title_separator_credit_tolerates_punctuation(self):
        """The LML#663 packed ``"<artist> - <album>"`` recovery path also
        tolerates a punctuation-only difference from the typed token."""
        release = make_discogs_result(
            release_id=1000,
            artist="",
            album="Melt-Banana - Cell Scape",
            confidence=0.4,
        )
        result = _select_rowless_artist_release("Melt Banana", [release])

        assert result is not None
        item, _resolved = result
        assert item.artist == "Melt-Banana"
        assert item.title == "Cell Scape"

    def test_genuinely_different_artist_still_rejected(self):
        """Punctuation tolerance must not loosen the gate into a fuzzy match --
        a release credited to a wholly different artist still drops."""
        release = make_discogs_result(
            release_id=1,
            artist="Boredoms",
            album="Vision Creation Newsun",
            confidence=0.4,
        )
        result = _select_rowless_artist_release("Melt Banana", [release])

        assert result is None

    def test_various_artists_credit_still_rejected(self):
        """A V/A compilation credit must not be swept in by the punctuation
        fold -- 'Various' shares no punctuation-stripped overlap with a real
        artist token, so this should already hold, but pin it explicitly."""
        release = make_discogs_result(
            release_id=2,
            artist="Various",
            album="Some Compilation",
            confidence=0.4,
        )
        result = _select_rowless_artist_release("Melt Banana", [release])

        assert result is None
