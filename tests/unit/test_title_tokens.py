"""LML#1369, the word axis: a one-word title disagreement must reject, and
nothing that is one title spelled two ways may.

``lookup/title_tokens.py`` is a leaf that compares content tokens; the
composed verdict a gate actually runs is ``title_identity.titles_name_different_releases``
(volume axis OR word axis), which is what these cases drive, so the corpus
pins the production path and not the leaf in isolation. The must-NOT-reject
half is the load-bearing one, because the first attempt at this axis rejected
on orthography: a possessive apostrophe, a punctuation-fused compound, an
abbreviation, a spelled number, a plural (#1378 review F1/F2/F3).

The leaf's own contract is pinned separately below (LML#1382 item 5):
literal-tuple tables for ``content_tokens`` and ``tokens_disagree``, and a
``NumberFolder`` that is not the volume classifier, so a change to the fold or
the alignment shows up as a changed tuple rather than only as a flipped verdict.
"""

import pytest

from lookup.title_identity import _fold_number, titles_name_different_releases
from lookup.title_tokens import content_tokens, tokens_disagree


def _no_numbers(_token: str) -> str | None:
    return None


class TestDiscriminatingWordDisagreement:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            # Shape C -- the prod case and the shortened Discogs form the
            # issue measured at ratio 85.7 with a 0.909 length ratio.
            ("the monterey international pop festival", "the international guitar festival"),
            ("the international pop festival", "the international guitar festival"),
            # LML#24's corpus, rejected on token identity as well as on the
            # <=5-char remainder guard that could not reach volume titles.
            ("chicago 16", "chicago v"),
            ("led zeppelin iv", "led zeppelin ii"),
            ("live 1969", "live 1970"),
            # The volume axis still composes in.
            ("art of field recording volume i", "art of field recording, vol. 2"),
        ],
    )
    def test_one_discriminating_word_disagrees(self, left, right):
        assert titles_name_different_releases(left, right) is True

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            # One-sided: the other side has nothing the first lacks. This is
            # also #973's own repro -- its extra "& 60s" is one-sided, so the
            # token gate declines and leaves it to #973's length guard.
            ("aluminum tunes", "aluminum tunes (remastered)"),
            ("songs for drella", "songs for drella - a fiction"),
            ("greatest hits of the 50's", "greatest hits of the 50s & 60s"),
            # Review F1: a possessive apostrophe is not a token boundary.
            ("greatest hits of the 50's", "greatest hits of the 50s"),
            # Review F3: intra-token punctuation joins, it does not split.
            ("title e.p.", "title ep"),
            ("hip-hop classics", "hiphop classics"),
            ("post-punk classics", "post punk classics"),
            ("rock'n'roll party", "rock n roll party"),
            # A conjunction that survives as a letter on one side and vanishes
            # on the other ("and" is a stopword, "&" is punctuation) leaves a
            # 2-token join one character short of the fused token; the join
            # tolerates that the way single tokens do (#1378 review).
            ("rock and roll party", "rock'n'roll party"),
            ("rock & roll party", "rock'n'roll party"),
            ("drum & bass classics", "drum'n'bass classics"),
            # Review F2: abbreviation and number-form variants of one title.
            ("kill bill pt. 2", "kill bill part 2"),
            ("kill bill pt. 2", "kill bill part two"),
            ("st. elsewhere", "saint elsewhere"),
            ("blues brothers", "blues bros."),
            # An abbreviation with two readings folds every reading onto the
            # abbreviation, so neither reading is rejected against it
            # ("st." is Saint and Street, "dr." Doctor and Drive; #1378 review).
            ("exile on main st.", "exile on main street"),
            ("mulholland dr.", "mulholland drive"),
            ("two sevens clash", "2 sevens clash"),
            ("the 3rd album", "the third album"),
            ("dance mix", "dance mixes"),
            # A spelling variant is not a discriminating token.
            ("aluminum tunes", "aluminium tunes"),
            ("rumours", "rumors"),
            # No shared token at all: nothing to align, leave it to the ratio.
            # A lone letter is not a shared token either (#1378 review).
            ("doggystyle", "doggy style"),
            ("plan b live", "scheme"),
            # A two-digit year abbreviation is the same year, and a decade
            # spelled two ways is one decade (#1378 review).
            ("atlantic rhythm and blues 1947-1974", "atlantic rhythm and blues 1947-74"),
            ("hits of the 50s", "hits of the 1950s"),
            ("hits of the 50's", "hits of the 1950's"),
            # A thousands separator is not a token boundary (#1378 review).
            ("1,000 hours", "1000 hours"),
            ("10,000 maniacs live", "10000 maniacs live"),
            # A pluralised number word folds like its singular (#1378 review).
            ("loved ones", "loved one"),
            # Packaging vocabulary describes an edition, not its contents.
            (
                "trax records: the 20th anniversary edition",
                "trax records 20th anniversary collection",
            ),
            ("moon pix (mono)", "moon pix (stereo)"),
            ("moon pix (bonus disc)", "moon pix (promo cd)"),
            # Articles and prepositions never discriminate.
            ("dark side of the moon", "the dark side of the moon"),
            # Diacritics fold before tokens compare -- in either composition
            # form (review F12): NFC "café" and NFD "cafe" + U+0301 are one word.
            ("pequena vertigem de amor", "pequeña vertigem de amor"),
            ("café del mar", "café del mar"),
            ("café del mar", "cafe del mar"),
            # Review R4: a Discogs title transliterated to ASCII against a library
            # row keeping the letter is one word twice, not two words -- "bølgen"
            # against "bolgen" is 83.3 under the 85 floor once the fold keeps ø.
            ("bølgen live", "bolgen live"),
            ("łódź sessions", "lodz sessions"),
            ("ßtraße tapes", "strasse tapes"),
            # A lone letter never discriminates, so a lettered series is
            # adjudicated for none of its letters (review F9 consistency).
            ("series vol. a", "series vol. b"),
            ("series vol. c", "series vol. d"),
            # The volume axis's own asymmetry still holds through the composition.
            ("disco not disco", "disco not disco, vol. 1"),
            ("nuggets vol. 1 & 2", "nuggets vol. 2"),
        ],
    )
    def test_agreeing_titles_are_not_rejected(self, left, right):
        assert titles_name_different_releases(left, right) is False


class TestContentTokens:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            # Articles and prepositions drop; content words keep their order.
            ("The Dark Side of the Moon", ("dark", "side", "moon")),
            # Apostrophes and periods are removed inside a token, not split on.
            ("Greatest Hits of the 50's", ("greatest", "hits", "50s")),
            ("Title E.P.", ("title",)),
            # Diacritics fold before anything else compares.
            ("Café del Mar", ("cafe", "mar")),
            # A thousands comma joins; punctuation between two years separates.
            ("1,000 Hours", ("1000", "hours")),
            (
                "Atlantic Rhythm and Blues 1947-1974",
                ("atlantic", "rhythm", "blues", "1947", "1974"),
            ),
            # Abbreviations fold onto the short form, every reading of it.
            ("Exile on Main Street", ("exile", "main", "st")),
            ("Kill Bill Part 2", ("kill", "bill", "pt", "2")),
            # Ordinals fold to digits without consulting the number folder.
            ("The 3rd Album", ("3", "album")),
            ("Third Album", ("3", "album")),
            # Packaging vocabulary never survives.
            ("Moon Pix (Bonus Disc)", ("moon", "pix")),
        ],
    )
    def test_folds_a_title_to_its_comparison_tokens(self, title, expected):
        assert content_tokens(title, _no_numbers) == expected

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Kill Bill Pt. Two", ("kill", "bill", "pt", "2")),
            ("Led Zeppelin IV", ("led", "zeppelin", "4")),
            ("Chicago V", ("chicago", "5")),
            # A pluralised number word folds like its singular.
            ("Loved Ones", ("loved", "1")),
        ],
    )
    def test_the_volume_classifier_folds_bare_number_tokens(self, title, expected):
        assert content_tokens(title, _fold_number) == expected


class TestTokensDisagree:
    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            # Two-sided: each side carries a token the other lacks, one shared.
            (("international", "pop", "festival"), ("international", "guitar", "festival"), True),
            (("live", "1969"), ("live", "1970"), True),
            (("chicago", "16"), ("chicago", "5"), True),
            # Identical or empty: nothing to adjudicate.
            (("moon", "pix"), ("moon", "pix"), False),
            ((), ("moon", "pix"), False),
            (("moon", "pix"), (), False),
            # One-sided: an extra token never rejects.
            (("aluminum", "tunes"), ("aluminum", "tunes", "remastered"), False),
            # Nothing shared: no aligned remainder to reason about.
            (("doggystyle",), ("scheme",), False),
            # A lone letter is never counted in the remainder.
            (("series", "a"), ("series", "b"), False),
            # Plural, near-spelling and two-digit-year tolerance.
            (("dance", "mix"), ("dance", "mixes"), False),
            (("rumours", "live"), ("rumors", "live"), False),
            (("hits", "50s"), ("hits", "1950s"), False),
            (("blues", "1947", "74"), ("blues", "1947", "1974"), False),
            # Two- and three-token joins, judged with the same tolerance.
            (("doggy", "style", "classics"), ("doggystyle", "classics"), False),
            (("rock", "n", "roll", "party"), ("rocknroll", "party"), False),
            (("rock", "roll", "party"), ("rocknroll", "party"), False),
        ],
    )
    def test_literal_token_tuples(self, left, right, expected):
        assert tokens_disagree(left, right) is expected
        assert tokens_disagree(right, left) is expected


class TestNumberFolderContract:
    def test_a_caller_supplied_folder_is_applied_to_bare_tokens(self):
        """Any ``NumberFolder`` works, not only ``title_identity``'s classifier:
        it sees each bare token after the punctuation and abbreviation folds,
        its answer replaces the token, a plural retries without the "s", and
        an ordinal is folded before the folder is consulted."""
        seen: list[str] = []

        def dozens(token: str) -> str | None:
            seen.append(token)
            return "12" if token == "dozen" else None

        assert content_tokens("A Dozen Roses, Dozens More: 2nd St.", dozens) == (
            "12",
            "roses",
            "12",
            "more",
            "2",
            "st",
        )
        assert "dozen" in seen and "st" in seen
        assert "2nd" not in seen and "a" not in seen
        assert (
            tokens_disagree(
                content_tokens("A Dozen Roses", dozens), content_tokens("12 Roses", _no_numbers)
            )
            is False
        )
