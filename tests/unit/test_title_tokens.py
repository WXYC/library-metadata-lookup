"""LML#1369, the word axis: a one-word title disagreement must reject, and
nothing that is one title spelled two ways may.

``lookup/title_tokens.py`` is a leaf that compares content tokens; the
composed verdict a gate actually runs is ``title_identity.titles_name_different_releases``
(volume axis OR word axis), which is what these cases drive, so the corpus
pins the production path and not the leaf in isolation. The must-NOT-reject
half is the load-bearing one, because the first attempt at this axis rejected
on orthography: a possessive apostrophe, a punctuation-fused compound, an
abbreviation, a spelled number, a plural (#1378 review F1/F2/F3).
"""

import pytest

from lookup.title_identity import titles_name_different_releases


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
            ("doggystyle", "doggy style"),
            # A two-digit year abbreviation is the same year.
            ("atlantic rhythm and blues 1947-1974", "atlantic rhythm and blues 1947-74"),
            # Packaging vocabulary describes an edition, not its contents.
            (
                "trax records: the 20th anniversary edition",
                "trax records 20th anniversary collection",
            ),
            # Articles and prepositions never discriminate.
            ("dark side of the moon", "the dark side of the moon"),
            # Diacritics fold before tokens compare -- in either composition
            # form (review F12): NFC "café" and NFD "cafe" + U+0301 are one word.
            ("pequena vertigem de amor", "pequeña vertigem de amor"),
            ("café del mar", "café del mar"),
            ("café del mar", "cafe del mar"),
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
