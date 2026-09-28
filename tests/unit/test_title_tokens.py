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
        it sees each bare token after the abbreviation fold ("street" as "st"),
        its answer replaces the token, a plural retries without the "s", and
        an ordinal is folded before the folder is consulted."""
        seen: list[str] = []

        def dozens(token: str) -> str | None:
            seen.append(token)
            return "12" if token == "dozen" else None

        assert content_tokens("A Dozen Roses, Dozens More: 2nd Street", dozens) == (
            "12",
            "roses",
            "12",
            "more",
            "2",
            "st",
        )
        assert "dozen" in seen and "st" in seen
        assert "street" not in seen and "2nd" not in seen and "a" not in seen
        assert (
            tokens_disagree(
                content_tokens("A Dozen Roses", dozens), content_tokens("12 Roses", _no_numbers)
            )
            is False
        )


class TestNumbersCompareByEquality:
    """LML#1382 item 1a: two all-digit tokens are one number only when equal
    (or a two-digit year of the four-digit one), never by the spelling floor
    -- "100"/"1000" scores 85.7 and "1969"/"1968" 75, the same as "grey"/"gray"."""

    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            # Must reject: a different number is a different release.
            ("100 broken windows live", "1000 broken windows live", True),
            ("live at the fillmore 1969", "live at the fillmore 1968", True),
            ("peel sessions 1979", "peel sessions 1997", True),
            ("2000 light years live", "20000 light years live", True),
            # Must not reject: one number written two ways.
            ("hits of '74", "hits of 1974", False),
            ("hits of the 50s", "hits of the 1950s", False),
            ("hits of the 50's", "hits of the 50s", False),
            ("1,000 hours", "1000 hours", False),
        ],
    )
    def test_number_rows(self, left, right, expected):
        assert titles_name_different_releases(left, right) is expected

    @pytest.mark.parametrize(
        ("left", "right"),
        [(("broken", "100"), ("broken", "1000")), (("fillmore", "1969"), ("fillmore", "1968"))],
    )
    def test_the_leaf_rejects_unequal_numbers(self, left, right):
        assert tokens_disagree(left, right) is True

    def test_through_the_gate_with_both_flags_on(self, set_title_gate_flags):
        from lookup.title_identity import title_token_gate_rejects

        set_title_gate_flags(master=True, word=True)
        assert title_token_gate_rejects("100 broken windows live", "1000 broken windows live")
        set_title_gate_flags(master=True, word=False)
        assert not title_token_gate_rejects("100 broken windows live", "1000 broken windows live")


class TestHyphenAndSlashSplit:
    """LML#1382 item 3: a hyphen or slash separates two tokens instead of
    fusing them ("in-utero" was "inutero" against "in utero"'s "utero"), and
    the two-and-three-token join recovers a compound ("hip" + "hop" is
    "hiphop"). A hyphen-attached stopword is kept as join material ("a" + "ha"
    is "aha") but, like a lone letter, never counted in a remainder or as the
    shared token."""

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("In-Utero Demos", ("in", "utero", "demos")),
            ("In\u2013Utero Demos", ("in", "utero", "demos")),
            ("Hip-Hop Classics", ("hip", "hop", "classics")),
            ("AC/DC Live", ("ac", "dc", "live")),
            ("On/Off Sessions", ("on", "off", "sessions")),
            ("Bum cd-1", ("bum", "cd", "1")),
            # A free stopword still drops.
            ("The Hip-Hop of the 80s", ("hip", "hop", "80s")),
            # Apostrophes and periods are still removed, not split on.
            ("Rock'n'Roll E.P.", ("rocknroll",)),
        ],
    )
    def test_content_tokens_split_on_hyphen_and_slash(self, title, expected):
        assert content_tokens(title, _no_numbers) == expected

    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            # Must not reject: one title, hyphenated and not.
            ("in-utero demos", "in utero demos", False),
            ("in\u2013utero demos", "in utero demos", False),
            ("on/off sessions", "on off sessions", False),
            ("bum cd-1", "bum cd 1", False),
            ("hip-hop classics", "hip hop classics", False),
            ("hip-hop classics", "hiphop classics", False),
            ("hip hop classics", "hiphop classics", False),
            ("x-ray vision live", "xray vision live", False),
            ("x-ray vision live", "x ray vision live", False),
            ("ac/dc live", "acdc live", False),
            ("ac/dc live", "ac dc live", False),
            ("rock-and-roll party", "rock'n'roll party", False),
            # A hyphen-attached stopword joins back into the fused spelling.
            ("a-ha live", "aha live", False),
            ("a-ok sessions", "aok sessions", False),
            ("de-tuned live", "detuned live", False),
            # Must still reject: a different word next to the compound.
            ("x-ray vision live", "x-ray spex live", True),
            ("post-punk classics", "post-rock classics", True),
        ],
    )
    @pytest.mark.parametrize("swap", [False, True], ids=["as-written", "swapped"])
    def test_hyphen_rows(self, left, right, expected, swap):
        if swap:
            left, right = right, left
        assert titles_name_different_releases(left, right) is expected

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            # An unmatched hyphen-attached stopword is not a discriminating token
            # (the right side's "live" is, so a counted "in" would make it two-sided)...
            (("in", "utero", "demos"), ("utero", "demos", "live")),
            # ...nor, matched, a shared one: nothing else is shared here...
            (("in", "utero"), ("in", "bloom")),
            # ...whichever side it is on: "cds"/"cd" pair a content token with an
            # attached packaging word, so neither side may read the pair as shared.
            (("nuggets", "8", "cds"), ("tribute", "wes", "2", "cd")),
        ],
    )
    def test_an_attached_stopword_neither_counts_nor_is_shared(self, left, right):
        assert tokens_disagree(left, right) is False
        assert tokens_disagree(right, left) is False


#: Pairs whose verdict once depended on argument order, or could: an attached
#: stopword or packaging piece matched to a content token on the other side.
_ORDER_PAIRS = [
    ("Nuggets box set 8 cds (A-H)", "Tribute to Wes Montgomery (2-cd set)"),
    ("Nuggets box set 8 cds (A-H)", "Arkology (3-cd box set)"),
    ("Complete Fantasy Records [3 volumes of 3 cd's each]", "Toxygene cd-4"),
    ("Christiansands (cd-single)", "The Tomato Collection (2 cd's)"),
    ("in-utero demos", "in utero demos live"),
    ("in-utero", "in-bloom"),
    ("a-ha live", "aha live"),
    ("hip-hop classics vol. 2", "hiphop classics"),
    ("rock-and-roll party", "rock and roll party tonight"),
    ("on/off sessions", "off the record sessions"),
    ("the international pop festival", "the international guitar festival"),
    ("doggy style live", "doggystyle"),
    ("x-ray spex live", "xray vision"),
    ("de-tuned live", "detuned sessions"),
    # A bracket annotation matched to a plain token is not a shared pair either.
    ("aluminum tunes [live]", "live at the fillmore"),
    ("elvis presley live", "the king [elvis presley]"),
    ("fire music [4-cd box]", "fire music: the complete sessions"),
    ("goodbye, babylon [vintage gospel] (discs 1,2)", "goodbye, babylon (vintage gospel)"),
    ("elvis [rca 1956]", "elvis presley"),
    ("the international pop festival [2-cd set]", "the international guitar festival"),
]


class TestArgumentOrderSymmetry:
    """On these pairs the verdict does not depend on which title is passed
    first: every production caller passes the library title second, so an
    order-dependent rule silently applies to one side only (#1386/#1387 review)."""

    @pytest.mark.parametrize(("left", "right"), _ORDER_PAIRS)
    def test_verdict_does_not_depend_on_argument_order(self, left, right):
        a, b = content_tokens(left, _fold_number), content_tokens(right, _fold_number)
        assert tokens_disagree(a, b) == tokens_disagree(b, a)
        assert titles_name_different_releases(left, right) == titles_name_different_releases(
            right, left
        )


class TestBracketAnnotations:
    """LML#1382 item 4: a library title's square-bracket annotation ("[4-CD
    box]", "[missing 8/04]", "[RCA 1956]") describes the copy, not the
    release, so against a Discogs subtitle it must not make the pair
    two-sided. Bracket content is stripped from what can discriminate (or be
    the shared token) on either side -- the leaf does not know which side is
    the library -- but still absorbs a counterpart the other side spells out."""

    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            # Must not reject: an annotation against a Discogs subtitle.
            ("fire music [4-cd box]", "fire music: the complete sessions", False),
            ("elvis [rca 1956]", "elvis presley", False),
            ("wonderfulness [missing 8/04]", "wonderfulness (live at the hungry i)", False),
            ("red house painters [45 minutes long]", "red house painters (rollercoaster)", False),
            # Either side: the leaf strips both.
            ("fire music: the complete sessions", "fire music [4-cd box]", False),
            # The annotation still absorbs the same words written as a subtitle.
            (
                "goodbye, babylon [vintage gospel] (discs 1,2)",
                "goodbye, babylon (vintage gospel)",
                False,
            ),
            # A bracket word is not the shared token either.
            ("aluminum tunes [live]", "live at the fillmore", False),
            # Nor is a pair with an annotation at one end, even when each side
            # has such a pair ("tracks"/[tracks], [8]/"eight"): a per-side check
            # reads both sides as shared and rejects.
            (
                "Tracks to Tsumbliwa [missing 8/04]",
                "The Eight Legged Groove Machine [with extra tracks]",
                False,
            ),
            # Must reject: a different word outside the brackets.
            (
                "the international pop festival [2-cd set]",
                "the international guitar festival",
                True,
            ),
            ("live at the fillmore 1969 [2-cd]", "live at the fillmore 1968", True),
        ],
    )
    @pytest.mark.parametrize("swap", [False, True], ids=["as-written", "swapped"])
    def test_bracket_rows(self, left, right, expected, swap):
        if swap:
            left, right = right, left
        assert titles_name_different_releases(left, right) is expected

    def test_through_the_gate_with_the_library_title_second(self, set_title_gate_flags):
        """Every production caller passes the library title second."""
        from lookup.title_identity import title_token_gate_rejects

        set_title_gate_flags(master=True, word=True)
        assert not title_token_gate_rejects("live at the fillmore", "aluminum tunes [live]")
        assert not title_token_gate_rejects(
            "fire music: the complete sessions", "fire music [4-cd box]"
        )

    def test_bracket_content_stays_in_the_tokens_for_matching(self):
        """Kept, in order, so it can absorb a counterpart; only the verdict ignores it."""
        assert content_tokens("Elvis [RCA 1956] Live", _no_numbers) == (
            "elvis",
            "rca",
            "1956",
            "live",
        )
        assert (
            tokens_disagree(content_tokens("Elvis [RCA 1956]", _no_numbers), ("elvis", "presley"))
            is False
        )


class TestRemainingCatalogShapes:
    """LML#1382 item 4, the remaining shapes: one fixed, two pinned as
    accepted misses so a later change to them is a decision, not an accident."""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("songs from the mountain live", "songs from the mt. live"),
            ("smoky mountains sessions", "smoky mts. sessions"),
        ],
    )
    def test_mountain_folds_onto_mt(self, left, right):
        assert titles_name_different_releases(left, right) is False

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("mountain songs live", "mountian songs live"),
            ("blue mountain live", "bluemountain live"),
            ("mountain-top live", "mountaintop live"),
        ],
    )
    def test_the_mt_fold_costs_misspelt_and_fused_mountain(self, left, right):
        """Accepted cost of folding mountain onto mt (no catalog title has these shapes)."""
        assert titles_name_different_releases(left, right) is True

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("50 words for snow", "fifty words for snow"),
            ("100 broken windows", "one hundred broken windows"),
        ],
    )
    def test_number_words_past_twenty_are_an_accepted_miss(self, left, right):
        """Accepted miss per LML#1382: number words past twenty are not folded."""
        assert titles_name_different_releases(left, right) is True

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("dr. octagon", "dr. octagonecologyst"),
            ("the monterey international pop fest", "the monterey international pop festival"),
        ],
    )
    def test_mid_token_prefix_or_truncation_is_an_accepted_miss(self, left, right):
        """Accepted miss per LML#1382: a token is not matched on a prefix of another."""
        assert titles_name_different_releases(left, right) is True
