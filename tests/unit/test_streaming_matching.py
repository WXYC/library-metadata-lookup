"""Unit tests for clients/streaming/matching.py."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from clients.streaming.matching import (
    album_subset_is_degenerate,
    is_acceptable_match,
    normalize_album_title,
    normalize_artist_name,
    score_match,
    score_match_track,
    strip_discogs_disambig,
    strip_format_suffix,
    strip_the_prefix,
    strip_track_suffix,
    title_subset_is_degenerate,
)
from discogs.matching import strip_discogs_suffix


class TestStripFormatSuffix:
    @pytest.mark.parametrize(
        "title, expected",
        [
            pytest.param('Automanikk 12"', "Automanikk", id="trailing-12-inch"),
            pytest.param('Conspiracy 7"', "Conspiracy", id="trailing-7-inch"),
            pytest.param('Round & Round 10"', "Round & Round", id="trailing-10-inch"),
            pytest.param("Abbey Road x 2", "Abbey Road", id="multi-disc-x2"),
            pytest.param("Confield x 3", "Confield", id="multi-disc-x3"),
            pytest.param("Parallel Lines LP", "Parallel Lines", id="trailing-lp"),
            pytest.param("Edits EP", "Edits", id="trailing-ep"),
            pytest.param("Confield", "Confield", id="no-suffix"),
            pytest.param("Aluminum Tunes", "Aluminum Tunes", id="no-suffix-multi-word"),
            pytest.param("Moon Pix (Deluxe Edition)", "Moon Pix", id="parenthetical-deluxe"),
            pytest.param("Homogenic (Remastered)", "Homogenic", id="parenthetical-remastered"),
            pytest.param("DOGA (Reissue)", "DOGA", id="parenthetical-reissue"),
            pytest.param("Honeybear (Expanded Edition)", "Honeybear", id="parenthetical-expanded"),
            pytest.param('7" And 7 Is', '7" And 7 Is', id="mid-title-quote-not-stripped"),
            pytest.param("", "", id="empty-string"),
            pytest.param("S/t", "S/t", id="self-titled-unchanged"),
            pytest.param(
                "Hotel Insomnia (Limited Edition)", "Hotel Insomnia", id="parenthetical-limited"
            ),
            pytest.param(
                "Gasoline (Anniversary Edition)", "Gasoline", id="parenthetical-anniversary"
            ),
            pytest.param("Me Myself and I [single]", "Me Myself and I", id="bracket-single"),
            pytest.param("Coma [single]", "Coma", id="bracket-single-short"),
            pytest.param("Noises [EP]", "Noises", id="bracket-ep"),
            pytest.param("Laminations [sampler EP]", "Laminations", id="bracket-sampler-ep"),
            pytest.param("College Karma [EP]", "College Karma", id="bracket-ep-two-words"),
        ],
    )
    def test_strip_format_suffix(self, title, expected):
        assert strip_format_suffix(title) == expected


class TestNormalizeAlbumTitle:
    @pytest.mark.parametrize(
        "title, expected",
        [
            pytest.param("Aluminum Tunes", "aluminum tunes", id="simple-lowercase"),
            pytest.param('Automanikk 12"', "automanikk", id="strip-then-normalize"),
            pytest.param("Café Tacvba EP", "cafe tacvba", id="diacritics-and-suffix"),
            pytest.param("", "", id="empty-string"),
        ],
    )
    def test_normalize_album_title(self, title, expected):
        assert normalize_album_title(title) == expected


class TestNormalizeArtistName:
    @pytest.mark.parametrize(
        "artist, expected",
        [
            pytest.param("Stereolab", "stereolab", id="simple-lowercase"),
            pytest.param("Björk", "bjork", id="diacritics"),
            pytest.param("Hüsker Dü", "husker du", id="multiple-diacritics"),
            pytest.param("", "", id="empty-string"),
        ],
    )
    def test_normalize_artist_name(self, artist, expected):
        assert normalize_artist_name(artist) == expected


class TestScoreMatch:
    def test_exact_match(self):
        assert score_match("Stereolab", "Stereolab") == 100.0

    def test_case_insensitive_match(self):
        assert score_match("stereolab", "Stereolab") == 100.0

    def test_diacritics_match(self):
        assert score_match("Bjork", "Björk") == 100.0

    def test_remastered_suffix_high_score(self):
        assert score_match("Confield", "Confield (Remastered)") >= 80.0

    def test_completely_different_low_score(self):
        assert score_match("Stereolab", "Stereo MC's") < 80.0

    def test_empty_strings(self):
        assert score_match("", "") == 100.0

    def test_one_empty_string(self):
        assert score_match("Stereolab", "") == 0.0

    def test_the_prefix_mismatch(self):
        """'Afros' should score well against 'The Afros'."""
        assert score_match("Afros", "The Afros") >= 80.0


class TestStripTrackSuffix:
    """Track-side suffix stripping for the LML#506 MB rescue song-sanity
    check. Mirrors ``strip_format_suffix`` but targets per-track variants
    (``(Live)``, ``(Remix)``, ``- Acoustic``...) instead of per-album
    indicators (``(Deluxe Edition)``, ``LP``, ``12"``).
    """

    @pytest.mark.parametrize(
        "title, expected",
        [
            pytest.param("Brakhage", "Brakhage", id="no-suffix"),
            pytest.param("Brakhage (Live)", "Brakhage", id="paren-live"),
            pytest.param("Brakhage (Live at Brixton)", "Brakhage", id="paren-live-at-venue"),
            pytest.param("Brakhage (Remix)", "Brakhage", id="paren-remix"),
            pytest.param("Brakhage (Single Mix)", "Brakhage", id="paren-single-mix"),
            pytest.param("Brakhage (Album Mix)", "Brakhage", id="paren-album-mix"),
            pytest.param("Brakhage (Radio Edit)", "Brakhage", id="paren-radio-edit"),
            pytest.param("Brakhage (Extended Mix)", "Brakhage", id="paren-extended-mix"),
            pytest.param("Brakhage (Acoustic Version)", "Brakhage", id="paren-acoustic-version"),
            pytest.param("Brakhage (Acoustic)", "Brakhage", id="paren-acoustic"),
            pytest.param("Brakhage (Instrumental)", "Brakhage", id="paren-instrumental"),
            pytest.param("Brakhage (Demo)", "Brakhage", id="paren-demo"),
            pytest.param("Brakhage (Bonus Track)", "Brakhage", id="paren-bonus-track"),
            pytest.param("Brakhage (feat. Some Artist)", "Brakhage", id="paren-feat-dot"),
            pytest.param("Brakhage (Featuring Some Artist)", "Brakhage", id="paren-featuring"),
            pytest.param("Brakhage - Live", "Brakhage", id="dash-live"),
            pytest.param("Brakhage - Live at Brixton", "Brakhage", id="dash-live-at-venue"),
            pytest.param("Brakhage - Remix", "Brakhage", id="dash-remix"),
            pytest.param("Brakhage - Acoustic", "Brakhage", id="dash-acoustic"),
            # Unicode dashes — MB surfaces six dash characters in real
            # metadata. All treated as dash-suffix separators.
            pytest.param("Brakhage – Live at Brixton", "Brakhage", id="en-dash-live"),
            pytest.param("Brakhage — Acoustic", "Brakhage", id="em-dash-acoustic"),
            pytest.param("Brakhage ‒ Live", "Brakhage", id="figure-dash-live"),
            pytest.param("Brakhage ― Live", "Brakhage", id="horizontal-bar-live"),
            pytest.param("Brakhage − Live", "Brakhage", id="minus-sign-live"),
            # Dash form keyword coverage — must include the same compound
            # forms the paren regex catches, otherwise the dash variant
            # asymmetrically over-rejects.
            pytest.param("Brakhage - Single Mix", "Brakhage", id="dash-single-mix"),
            pytest.param("Brakhage - Radio Edit", "Brakhage", id="dash-radio-edit"),
            pytest.param("Brakhage - feat. Other", "Brakhage", id="dash-feat-dot"),
            # Common MB variant forms added in iter 2 of the review —
            # fidelity markers (Mono/Stereo), session markers (BBC/Peel),
            # take-numbered alternates (digit-required to avoid "Take Five"),
            # compound mix/edit/version with explicit prefix lists,
            # rework/a cappella.
            # Note: bare ``mono`` / ``stereo`` are NOT in the keyword set —
            # they false-positive on real titles like "(Mono No Aware)" /
            # "(In Stereo)" / "(Pink Stereo)". Only compound forms strip:
            # "(Mono Version)" / "(Stereo Mix)" / etc.
            pytest.param("Brakhage (Mono Mix)", "Brakhage", id="paren-mono-mix"),
            pytest.param("Brakhage (Stereo Mix)", "Brakhage", id="paren-stereo-mix"),
            pytest.param("Brakhage (Take 1)", "Brakhage", id="paren-take-digit"),
            pytest.param("Brakhage (Take 12)", "Brakhage", id="paren-take-multi-digit"),
            # Note: bare ``session`` is NOT in the keyword set — it false-
            # positives on real titles like "(Session in Salt Lake City)"
            # and "(Session Highlights)". Only compound forms strip.
            pytest.param("Brakhage (BBC Session)", "Brakhage", id="paren-bbc-session"),
            pytest.param("Brakhage (Peel Session)", "Brakhage", id="paren-peel-session"),
            pytest.param("Brakhage (Radio Session)", "Brakhage", id="paren-radio-session"),
            pytest.param("Brakhage (Recording Session)", "Brakhage", id="paren-recording-session"),
            pytest.param("Brakhage (Mono Version)", "Brakhage", id="paren-mono-version"),
            pytest.param("Brakhage (Stereo Version)", "Brakhage", id="paren-stereo-version"),
            pytest.param("Brakhage (Single Version)", "Brakhage", id="paren-single-version"),
            pytest.param("Brakhage (Studio Version)", "Brakhage", id="paren-studio-version"),
            pytest.param("Brakhage (Original Version)", "Brakhage", id="paren-original-version"),
            pytest.param("Brakhage (Vocal Mix)", "Brakhage", id="paren-vocal-mix"),
            pytest.param("Brakhage (Club Mix)", "Brakhage", id="paren-club-mix"),
            pytest.param("Brakhage (Dub Mix)", "Brakhage", id="paren-dub-mix"),
            pytest.param("Brakhage (Radio Mix)", "Brakhage", id="paren-radio-mix"),
            pytest.param("Brakhage (Single Edit)", "Brakhage", id="paren-single-edit"),
            pytest.param("Brakhage (Reworked)", "Brakhage", id="paren-reworked"),
            pytest.param("Brakhage (a cappella)", "Brakhage", id="paren-a-cappella"),
            pytest.param("Brakhage (Alternate Take)", "Brakhage", id="paren-alternate-take"),
            # Bare-word triggers DELIBERATELY excluded — these are
            # over-broad and false-positive on legitimate parenthetical
            # phrases ("(Original Korean Version)" naming a release variant,
            # "(Reprise)" / "(Interlude)" naming distinct tracks). Over-
            # stripping would let the LML#506 sanity check accept the
            # wrong-edition tracklist (the exact failure it's preventing).
            # "(Take Five)" is the Brubeck standard; digit-required
            # ``take\s+\d+`` keyword avoids stripping it.
            pytest.param(
                "Brakhage (Original Korean Version)",
                "Brakhage (Original Korean Version)",
                id="no-strip-bare-version",
            ),
            pytest.param("Brakhage (Pre-mix)", "Brakhage (Pre-mix)", id="no-strip-bare-mix"),
            pytest.param("Brakhage (Reprise)", "Brakhage (Reprise)", id="no-strip-reprise"),
            pytest.param("Brakhage (Interlude)", "Brakhage (Interlude)", id="no-strip-interlude"),
            pytest.param(
                "Brakhage (Mix Master Edit)",
                "Brakhage (Mix Master Edit)",
                id="no-strip-mid-keyword-only",
            ),
            pytest.param(
                "Brakhage (Take Five)",
                "Brakhage (Take Five)",
                id="no-strip-take-non-digit",
            ),
            # Bare ``mono`` / ``stereo`` / ``session`` FP guards — these
            # CANNOT strip because they collide with real track-name
            # parentheticals. Iter 3 narrowed all three to compound-only.
            pytest.param(
                "Brakhage (Mono No Aware)",
                "Brakhage (Mono No Aware)",
                id="no-strip-bare-mono-japanese",
            ),
            pytest.param(
                "Brakhage (In Stereo)",
                "Brakhage (In Stereo)",
                id="no-strip-bare-stereo",
            ),
            pytest.param(
                "Brakhage (Pink Stereo)",
                "Brakhage (Pink Stereo)",
                id="no-strip-stereo-band-name",
            ),
            pytest.param(
                "Brakhage (Session in Salt Lake City)",
                "Brakhage (Session in Salt Lake City)",
                id="no-strip-bare-session-locative",
            ),
            pytest.param(
                "Brakhage (Stereo Sessions)",
                "Brakhage (Stereo Sessions)",
                id="no-strip-bare-session-plural",
            ),
            # Word-internal hyphens (no whitespace before the dash) must NOT
            # be treated as a dash suffix. iter-1 used ``\s*[-–—]\s*`` which
            # allowed zero whitespace; iter-2 requires ``\s+`` on the leading
            # side. ``Pre-Live`` / ``Re-Demo`` / ``Anti-Acoustic`` keep their
            # hyphenated form rather than truncating to the prefix word.
            pytest.param("Pre-Live", "Pre-Live", id="no-strip-word-internal-pre-live"),
            pytest.param("Re-Demo", "Re-Demo", id="no-strip-word-internal-re-demo"),
            pytest.param(
                "Anti-Acoustic", "Anti-Acoustic", id="no-strip-word-internal-anti-acoustic"
            ),
            # Hyphenated band/title + space-dash-suffix: leading hyphen is
            # preserved (it's part of the title), trailing dash-suffix strips.
            pytest.param("Be-Bop - Live", "Be-Bop", id="hyphenated-title-with-dash-suffix"),
            # Album-side suffixes are NOT stripped — different concept, different
            # caller. The MB resolver returns per-track titles; album-style
            # suffixes on a track title would be noise we'd want to leave alone
            # so the score reflects actual mismatch.
            pytest.param(
                "Brakhage (2024 Remaster)", "Brakhage (2024 Remaster)", id="no-strip-remaster"
            ),
            pytest.param(
                "Brakhage (Deluxe Edition)", "Brakhage (Deluxe Edition)", id="no-strip-deluxe"
            ),
            # Non-suffix parentheticals stay
            pytest.param("Brakhage (Part 1)", "Brakhage (Part 1)", id="no-strip-part"),
            pytest.param("", "", id="empty-string"),
        ],
    )
    def test_strip_track_suffix(self, title, expected):
        assert strip_track_suffix(title) == expected


class TestScoreMatchTrack:
    """``score_match_track`` adds track-side suffix stripping on top of the
    album-aware ``score_match``. The LML#506 sanity check uses it to detect
    "song requested by DJ does NOT appear in MB-rescued tracklist" without
    over-rejecting variants like ``"Brakhage" vs "Brakhage (Live)"``.
    """

    @pytest.mark.parametrize(
        "query, result",
        [
            pytest.param("Brakhage", "Brakhage (Live)", id="paren-live"),
            pytest.param("Brakhage", "Brakhage (Remix)", id="paren-remix"),
            pytest.param("Brakhage", "Brakhage (Single Mix)", id="paren-single-mix"),
            pytest.param("Brakhage", "Brakhage (Acoustic Version)", id="paren-acoustic"),
            pytest.param("Brakhage", "Brakhage - Live", id="dash-live"),
            pytest.param("Brakhage", "Brakhage – Live at Brixton", id="en-dash-live"),
            pytest.param("Brakhage", "Brakhage — Acoustic", id="em-dash-acoustic"),
            pytest.param("Brakhage", "Brakhage ‒ Live", id="figure-dash-live"),
            pytest.param("Brakhage", "Brakhage ― Live", id="horizontal-bar-live"),
            pytest.param("Brakhage", "Brakhage − Live", id="minus-sign-live"),
            pytest.param("Brakhage", "Brakhage - Radio Edit", id="dash-radio-edit"),
            pytest.param(
                "Cybeles Reverie",
                "Cybele's Reverie (Live at Brixton)",
                id="combined-apostrophe-and-suffix",
            ),
        ],
    )
    def test_track_variants_clear_acceptance_floor(self, query, result):
        # Pin: the existing 80 floor is preserved when a track-side suffix is
        # the only difference between the requested song and the MB result.
        # Without ``strip_track_suffix`` this fails — empirically:
        #   ``score_match("Brakhage", "Brakhage (Live)") == 69.6``
        assert score_match_track(query, result) >= 80.0

    def test_genuinely_different_titles_still_reject(self):
        # The whole point of the floor — make sure the suffix-stripper hasn't
        # bulldozed the signal. "Brakhage" and "Cybele's Reverie" are different
        # tracks on the same Stereolab album; the sanity check must catch this.
        assert score_match_track("Brakhage", "Cybele's Reverie") < 80.0

    @pytest.mark.parametrize(
        "query, result",
        [
            # Bare-word triggers — over-broad parenthetical-keyword leak
            # cases. These MUST score below the 80 floor so the LML#506
            # sanity check correctly REJECTS wrong-edition tracklists where
            # the only "match" is incidental keyword presence inside an
            # unrelated parenthetical phrase.
            pytest.param("Brakhage", "Brakhage (Original Korean Version)", id="bare-version"),
            pytest.param("Brakhage", "Brakhage (Pre-mix)", id="bare-mix"),
            pytest.param("Brakhage", "Brakhage (Reprise)", id="reprise"),
            pytest.param("Brakhage", "Brakhage (Interlude)", id="interlude"),
            pytest.param("Brakhage", "Brakhage (Take Five)", id="take-non-digit"),
            pytest.param("Brakhage", "Brakhage (Mix Master Edit)", id="mid-keyword-only"),
            # Iter 3: bare mono/stereo/session FP guards — must stay
            # rejected so the sanity check doesn't accept wrong-edition
            # tracklists where the only "match" is incidental occurrence
            # of `mono` / `stereo` / `session` inside an unrelated
            # parenthetical noun phrase.
            pytest.param("Brakhage", "Brakhage (Mono No Aware)", id="mono-japanese"),
            pytest.param("Brakhage", "Brakhage (In Stereo)", id="in-stereo"),
            pytest.param("Brakhage", "Brakhage (Session in Salt Lake City)", id="session-locative"),
        ],
    )
    def test_over_broad_keywords_do_not_strip(self, query, result):
        # Critical regression pin: an earlier draft of the regex included
        # bare `mix`, `edit`, `version`, `reprise`, `interlude` and let
        # these scores reach 100, which would defeat the entire sanity
        # check. The narrowed keyword set keeps these below 80.
        assert score_match_track(query, result) < 80.0

    def test_exact_match(self):
        assert score_match_track("Brakhage", "Brakhage") == 100.0

    def test_empty_strings(self):
        # Mirrors ``score_match`` rapidfuzz convention.
        assert score_match_track("", "") == 100.0


class TestIsAcceptableMatch:
    def test_both_high_accepted(self):
        assert is_acceptable_match(95.0, 90.0) is True

    def test_both_at_threshold_accepted(self):
        assert is_acceptable_match(80.0, 80.0) is True

    def test_artist_below_threshold_rejected(self):
        assert is_acceptable_match(75.0, 95.0) is False

    def test_title_below_threshold_rejected(self):
        assert is_acceptable_match(95.0, 60.0) is False

    def test_both_below_threshold_rejected(self):
        assert is_acceptable_match(50.0, 50.0) is False


class TestTitleSubsetIsDegenerate:
    """LML#719: the title-axis guard for ``token_set_ratio`` matchers. Separates
    a *degenerate* subset (short generic query buried in a long unrelated
    candidate, ``token_set_ratio`` == 100 with no real signal) from a *legitimate*
    one (differs only by a recognized variant/reissue suffix). The boundary is
    ``score_match_track`` (suffix-strip + ``token_sort_ratio``) below the floor.
    """

    def test_rejects_generic_title_buried_in_unrelated_candidate(self):
        # The production bug: `hound dog` ⊂ `black leather (the hound dog mix)`
        # scores token_set 100 but score_match_track 42.9.
        assert title_subset_is_degenerate("Hound Dog", "Black leather (The hound dog mix)") is True

    def test_retains_variant_suffix_subset_live(self):
        # `(Live)` is stripped by score_match_track → the sides match.
        assert title_subset_is_degenerate("la paradoja", "la paradoja (Live)") is False

    def test_retains_variant_suffix_subset_remaster(self):
        assert title_subset_is_degenerate("Back, Baby", "Back, Baby (Remastered)") is False

    def test_retains_feat_suffix_subset(self):
        assert (
            title_subset_is_degenerate("Call Your Name", "Call Your Name (feat. Someone)") is False
        )

    def test_retains_leading_stopword_drop(self):
        # DJ typed a shortened form dropping a leading article — not degenerate.
        assert title_subset_is_degenerate("Sentimental Mood", "In a Sentimental Mood") is False

    def test_identical_titles_not_degenerate(self):
        assert title_subset_is_degenerate("Back, Baby", "Back, Baby") is False


class TestAlbumSubsetIsDegenerate:
    """LML#721: the album-axis sibling of ``title_subset_is_degenerate``.
    Separates a *degenerate* subset (short generic query album buried in a
    long unrelated candidate album, ``token_set_ratio`` == 100 with no real
    signal) from a *legitimate* one (differs only by a recognized
    reissue/remaster suffix). The boundary is ``score_match`` (``token_sort_ratio``,
    with ``strip_format_suffix`` already applied) below the floor.
    """

    def test_rejects_generic_album_buried_in_unrelated_candidate(self):
        # The production shape: `live` ⊂ `live at the apollo, vol. 2` scores
        # token_set 100 but score_match (token_sort) 26.7.
        assert album_subset_is_degenerate("Live", "Live at the Apollo, Vol. 2") is True

    def test_rejects_generic_album_demos(self):
        assert album_subset_is_degenerate("Demos", "Demos and Rarities 1998-2004") is True

    def test_rejects_generic_album_singles(self):
        assert album_subset_is_degenerate("Singles", "Singles Going Steady") is True

    def test_retains_reissue_suffix_subset(self):
        # `(Deluxe Edition)` is stripped by strip_format_suffix → the sides match.
        assert album_subset_is_degenerate("Tzenni", "Tzenni (Deluxe Edition)") is False

    def test_retains_remaster_suffix_subset(self):
        assert album_subset_is_degenerate("Moon Pix", "Moon Pix (Remastered)") is False

    def test_identical_albums_not_degenerate(self):
        assert album_subset_is_degenerate("Aluminum Tunes", "Aluminum Tunes") is False


class TestStripDiscogsSuffix:
    @pytest.mark.parametrize(
        "name, expected",
        [
            pytest.param("DNA (22)", "DNA", id="numbered-suffix"),
            pytest.param("Asia (2)", "Asia", id="single-digit"),
            pytest.param("Björk", "Björk", id="no-suffix"),
            pytest.param("The Afros", "The Afros", id="no-suffix-the"),
            pytest.param("", "", id="empty"),
            pytest.param(
                "Moon Pix (Deluxe Edition)",
                "Moon Pix (Deluxe Edition)",
                id="non-numeric-parens-preserved",
            ),
        ],
    )
    def test_strip_discogs_suffix(self, name, expected):
        assert strip_discogs_suffix(name) == expected


class TestStripDiscogsDisambig:
    """Broad disambig strip (numeric + country/word qualifiers).

    Delegates to ``wxyc_etl.text.strip_discogs_disambiguation(name,
    broad=True)`` since LML#1042. There was no dedicated test class for
    this function before #1042 (only indirect coverage via
    ``normalize_artist_credit``'s narrow numeric form) — added here as
    part of pinning this call site's behavior before the swap.
    """

    @pytest.mark.parametrize(
        "name, expected",
        [
            pytest.param("Sessa (2)", "Sessa", id="numeric"),
            pytest.param("Stereolab (UK)", "Stereolab", id="country-code"),
            pytest.param("Sade (band)", "Sade", id="word-qualifier"),
            pytest.param("Björk", "Björk", id="no-suffix"),
            pytest.param("", "", id="empty"),
            pytest.param(
                "Csillagrablók (Unofficial Bootleg Rip)",
                "Csillagrablók (Unofficial Bootleg Rip)",
                id="too-long-qualifier-preserved",
            ),
            pytest.param("Sessa (2) ", "Sessa", id="trailing-whitespace-tolerated"),
        ],
    )
    def test_strip_discogs_disambig(self, name, expected):
        assert strip_discogs_disambig(name) == expected

    def test_space_after_open_paren_no_longer_stripped(self):
        """LML#1042 divergence pin: crate requires content flush against ``(``.

        The pre-#1042 local regex (``\\(\\s*[A-Za-z0-9]...``) tolerated a
        space right after the opening paren; the crate primitive does not.
        Discogs' own disambiguator format never has this space, so this is
        a safe narrowing, not a functional regression — pinned here so a
        future change to this call site's behavior is a deliberate diff,
        not a silent one.
        """
        assert strip_discogs_disambig("Foo ( UK)") == "Foo ( UK)"


class TestFindBestMatch:
    """Tests for the generic find_best_match function."""

    def test_finds_best_by_combined_score(self):
        from clients.streaming.matching import find_best_match

        results = [
            {"artist": "Stereolab", "album": "Dots and Loops", "url": "http://a"},
            {"artist": "Stereolab", "album": "Aluminum Tunes", "url": "http://b"},
        ]
        best = find_best_match(
            results,
            "Stereolab",
            "Aluminum Tunes",
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["album"],
            url_fn=lambda r: r["url"],
        )
        assert best is not None
        assert best["url"] == "http://b"
        assert best["matched_title"] == "Aluminum Tunes"

    def test_returns_per_axis_scores(self):
        """find_best_match surfaces the winning candidate's artist/title scores.

        LML#592: callers emit these to Sentry to measure how often a match
        clears the floor with a marginal artist score against a high title.
        Uses the Wand/Wanda short-name collision (artist 88.89, title 100).
        """
        from clients.streaming.matching import find_best_match, score_match

        results = [
            {"artist": "Wanda", "album": "DOGA", "url": "http://w"},
        ]
        best = find_best_match(
            results,
            "Wand",
            "DOGA",
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["album"],
            url_fn=lambda r: r["url"],
        )
        assert best is not None
        assert best["artist_score"] == score_match("Wand", "Wanda")
        assert best["title_score"] == score_match("DOGA", "DOGA")

    def test_returns_none_when_no_acceptable_match(self):
        from clients.streaming.matching import find_best_match

        results = [
            {"artist": "Cat Power", "album": "Moon Pix", "url": "http://x"},
        ]
        best = find_best_match(
            results,
            "Autechre",
            "Confield",
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["album"],
            url_fn=lambda r: r["url"],
        )
        assert best is None

    def test_returns_none_for_empty_results(self):
        from clients.streaming.matching import find_best_match

        best = find_best_match(
            [],
            "Stereolab",
            "Aluminum Tunes",
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["album"],
            url_fn=lambda r: r["url"],
        )
        assert best is None

    def test_includes_id_when_id_fn_provided(self):
        from clients.streaming.matching import find_best_match

        results = [
            {"artist": "Stereolab", "album": "Aluminum Tunes", "url": "http://b", "id": "abc123"},
        ]
        best = find_best_match(
            results,
            "Stereolab",
            "Aluminum Tunes",
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["album"],
            url_fn=lambda r: r["url"],
            id_fn=lambda r: r["id"],
        )
        assert best is not None
        assert best["id"] == "abc123"

    def test_no_id_key_when_id_fn_omitted(self):
        from clients.streaming.matching import find_best_match

        results = [
            {"artist": "Stereolab", "album": "Aluminum Tunes", "url": "http://b"},
        ]
        best = find_best_match(
            results,
            "Stereolab",
            "Aluminum Tunes",
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["album"],
            url_fn=lambda r: r["url"],
        )
        assert best is not None
        assert "id" not in best

    def test_spotify_field_extraction(self):
        """Verifies find_best_match works with Spotify's nested response format."""
        from clients.streaming.matching import find_best_match

        results = [
            {
                "name": "Aluminum Tunes",
                "artists": [{"name": "Stereolab"}],
                "external_urls": {"spotify": "https://open.spotify.com/album/xyz"},
                "id": "xyz",
            },
        ]
        best = find_best_match(
            results,
            "Stereolab",
            "Aluminum Tunes",
            artist_fn=lambda r: r.get("artists", [{}])[0].get("name", ""),
            title_fn=lambda r: r.get("name", ""),
            url_fn=lambda r: r.get("external_urls", {}).get("spotify", ""),
            id_fn=lambda r: r.get("id", ""),
        )
        assert best is not None
        assert best["url"] == "https://open.spotify.com/album/xyz"
        assert best["id"] == "xyz"
        assert best["confidence"] > 0


# The exact extractor shapes the adapters pass — replicated here so the guard is
# tested against the real subscript chains, not a paraphrase.
#   Spotify: clients/streaming/spotify.py:66-68
#   Deezer:  clients/streaming/deezer.py:39-41
_SPOTIFY_EXTRACTORS = {
    "artist_fn": lambda x: x["artists"][0]["name"],
    "title_fn": lambda x: x["name"],
    "url_fn": lambda x: x["external_urls"]["spotify"],
}
_DEEZER_EXTRACTORS = {
    "artist_fn": lambda x: x["artist"]["name"],
    "title_fn": lambda x: x["title"],
    "url_fn": lambda x: x["link"],
}

_SPOTIFY_GOOD = {
    "name": "DOGA",
    "artists": [{"name": "Juana Molina"}],
    "external_urls": {"spotify": "https://open.spotify.com/album/good"},
}
_DEEZER_GOOD = {
    "artist": {"name": "Juana Molina"},
    "title": "DOGA",
    "link": "https://www.deezer.com/album/good",
}


class TestFindBestMatchSparseRowGuard:
    """LML#640: a single sparse/malformed row must not erase every match.

    The Spotify/Deezer extractors subscript raw API dicts directly, so a row
    missing ``artists`` / ``external_urls`` / ``link`` (or carrying an empty
    ``artists`` list) raises ``KeyError``/``IndexError``/``TypeError`` mid-loop.
    Before the per-item guard, that exception propagated out of
    ``find_best_match`` and aborted the whole match — the well-formed rows in
    the same response were lost with it. The guard skips (and logs) just the
    bad row.

    A *sparse minority* is skipped, but *total* extraction failure (every row
    raises) re-raises: that is a systemic upstream break, not a sparse row, and
    the streaming orchestrator must see it as an errored source to retry
    (LML#376) rather than a persisted no-match.
    """

    @pytest.mark.parametrize(
        "sparse_row",
        [
            pytest.param({"name": "X", "external_urls": {"spotify": "http://x"}}, id="no-artists"),
            pytest.param(
                {"name": "X", "artists": [], "external_urls": {"spotify": "http://x"}},
                id="empty-artists-list",
            ),
            pytest.param(
                {"name": "X", "artists": [{}], "external_urls": {"spotify": "http://x"}},
                id="artist-without-name",
            ),
            pytest.param(
                {"artists": [{"name": "X"}], "external_urls": {"spotify": "http://x"}},
                id="no-title",
            ),
        ],
    )
    def test_spotify_sparse_row_skipped_good_row_survives(self, sparse_row):
        from clients.streaming.matching import find_best_match

        best = find_best_match(
            [sparse_row, _SPOTIFY_GOOD],
            "Juana Molina",
            "DOGA",
            **_SPOTIFY_EXTRACTORS,
        )
        assert best is not None
        assert best["url"] == "https://open.spotify.com/album/good"
        assert best["matched_title"] == "DOGA"

    @pytest.mark.parametrize(
        "sparse_row",
        [
            pytest.param({"title": "X", "link": "http://x"}, id="no-artist"),
            pytest.param({"artist": None, "title": "X", "link": "http://x"}, id="null-artist"),
            pytest.param(
                {"artist": {}, "title": "X", "link": "http://x"}, id="artist-without-name"
            ),
            pytest.param({"artist": {"name": "X"}, "link": "http://x"}, id="no-title"),
        ],
    )
    def test_deezer_sparse_row_skipped_good_row_survives(self, sparse_row):
        from clients.streaming.matching import find_best_match

        best = find_best_match(
            [sparse_row, _DEEZER_GOOD],
            "Juana Molina",
            "DOGA",
            **_DEEZER_EXTRACTORS,
        )
        assert best is not None
        assert best["url"] == "https://www.deezer.com/album/good"
        assert best["matched_title"] == "DOGA"

    def test_sparse_row_after_good_row_does_not_erase_winner(self):
        """Order independence: the bad row trailing a good row must not undo it."""
        from clients.streaming.matching import find_best_match

        best = find_best_match(
            [_DEEZER_GOOD, {"title": "X", "link": "http://x"}],
            "Juana Molina",
            "DOGA",
            **_DEEZER_EXTRACTORS,
        )
        assert best is not None
        assert best["url"] == "https://www.deezer.com/album/good"

    def test_floor_clearing_row_with_unextractable_url_is_skipped(self):
        """A row that clears the artist/title floor but whose ``url_fn`` raises
        is skipped, not fatal — and a lower-priority well-formed row still wins.

        ``url_fn`` is the one extractor evaluated only for a candidate that
        clears the floor, so this pins that the guard covers it too (not just
        the always-run artist/title extractors)."""
        from clients.streaming.matching import find_best_match

        # First row matches on artist/title but has no ``external_urls`` → url_fn
        # raises KeyError. Second row is fully well-formed.
        no_url = {"name": "DOGA", "artists": [{"name": "Juana Molina"}]}
        best = find_best_match(
            [no_url, _SPOTIFY_GOOD],
            "Juana Molina",
            "DOGA",
            **_SPOTIFY_EXTRACTORS,
        )
        assert best is not None
        assert best["url"] == "https://open.spotify.com/album/good"

    def test_all_rows_failing_extraction_reraises(self):
        """Total extraction failure re-raises (it is a systemic break, not a
        sparse row). Skipping every row and returning None would silently
        convert a broken upstream into a persisted no-match and suppress the
        LML#376 errored-source retry. Deliberately reverses the earlier
        all-sparse -> None behavior.
        """
        from clients.streaming.matching import find_best_match

        # Row 1 raises KeyError (no "artist"); row 2 raises TypeError (None
        # subscript). Asserting BOTH rows logged a warning — and that the LAST
        # row's error (TypeError) is what escapes — proves the loop caught each
        # row then re-raised at the aggregate, not that it aborted on row 1
        # (which would log zero warnings and raise KeyError).
        with patch("clients.streaming.matching.logger") as mock_logger:
            with pytest.raises(TypeError) as excinfo:
                find_best_match(
                    [
                        {"title": "X", "link": "http://x"},
                        {"artist": None, "title": "Y", "link": "http://y"},
                    ],
                    "Juana Molina",
                    "DOGA",
                    **_DEEZER_EXTRACTORS,
                )
        assert mock_logger.warning.call_count == 2
        # The genuine upstream error object is surfaced (the row-2 null subscript),
        # not a freshly synthesized exception — so the orchestrator's errored-source
        # diagnostics carry the real cause.
        assert "subscriptable" in str(excinfo.value)

    def test_partial_failure_does_not_reraise(self):
        """A sparse minority (at least one row extracts cleanly) is tolerated —
        only total failure re-raises."""
        from clients.streaming.matching import find_best_match

        best = find_best_match(
            [{"title": "X", "link": "http://x"}, _DEEZER_GOOD],
            "Juana Molina",
            "DOGA",
            **_DEEZER_EXTRACTORS,
        )
        assert best is not None
        assert best["url"] == "https://www.deezer.com/album/good"

    def test_clean_floor_rejected_row_is_not_an_extraction_failure(self):
        """The re-raise boundary is ``extraction_failures == total``, NOT
        ``best is None``. A row that extracts cleanly but loses the 80/80 floor
        is a *successful* extraction, so pairing it with a malformed row must
        return None WITHOUT re-raising — a legitimate no-match, not a systemic
        break. Guards against a future `if best is None: raise` mis-refactor.
        """
        from clients.streaming.matching import find_best_match

        # Extracts fine, but the title is for a different album → fails the floor.
        clean_but_wrong = {
            "artist": {"name": "Juana Molina"},
            "title": "Halo",
            "link": "http://other",
        }
        malformed = {"title": "X", "link": "http://x"}  # KeyError in artist_fn
        best = find_best_match(
            [clean_but_wrong, malformed],
            "Juana Molina",
            "DOGA",
            **_DEEZER_EXTRACTORS,
        )
        assert best is None  # floor-rejected + one skipped row → no match, no raise

    def test_id_fn_raise_is_skipped_not_fatal(self):
        """``id_fn`` is evaluated inside the guard and feeds the output dict, so
        a row whose only fault is a raising ``id_fn`` must be skipped, not abort
        the match — pins that id_fn extraction stays inside the try.

        ``bad_id`` is an EXACT match (would win the floor on a tie as the first
        row), so if ``id_fn`` were evaluated lazily on the winner instead of
        eagerly inside the guard, it would be selected and then crash. The
        tracking ``id_fn`` also asserts it actually ran on the bad row — i.e.
        the skip is caused by id_fn raising, not by the row losing on score.
        """
        from clients.streaming.matching import find_best_match

        # Clears artist/title/url but has no "id" key -> id_fn raises KeyError.
        # Placed first and exact so it is the would-be winner.
        bad_id = {
            "name": "DOGA",
            "artists": [{"name": "Juana Molina"}],
            "external_urls": {"spotify": "http://bad"},
        }
        seen_ids = []

        def tracking_id_fn(x):
            seen_ids.append(x.get("external_urls", {}).get("spotify"))
            return x["id"]  # raises KeyError on bad_id

        best = find_best_match(
            [bad_id, {**_SPOTIFY_GOOD, "id": "good-id"}],
            "Juana Molina",
            "DOGA",
            **_SPOTIFY_EXTRACTORS,
            id_fn=tracking_id_fn,
        )
        assert best is not None
        assert best["id"] == "good-id"
        # id_fn ran on the bad row (proving it is inside the per-row guard, not
        # deferred to the winner) and on the good row.
        assert "http://bad" in seen_ids

    def test_skip_does_not_perturb_winner(self):
        """Skipping a bad row leaves the winner and its combined score identical
        to the no-skip case — the 80/80 floor math is untouched."""
        from clients.streaming.matching import find_best_match

        with_skip = find_best_match(
            [{"title": "X", "link": "http://x"}, _DEEZER_GOOD],
            "Juana Molina",
            "DOGA",
            **_DEEZER_EXTRACTORS,
        )
        without_skip = find_best_match([_DEEZER_GOOD], "Juana Molina", "DOGA", **_DEEZER_EXTRACTORS)
        assert with_skip is not None and without_skip is not None
        assert with_skip["confidence"] == without_skip["confidence"]
        assert with_skip["url"] == without_skip["url"]

    def test_skipped_rows_logged_once_per_bad_row(self):
        """Cardinality: one warning per skipped row, not one per call. Two bad
        rows alongside a good one -> exactly two warnings."""
        from clients.streaming.matching import find_best_match

        with patch("clients.streaming.matching.logger") as mock_logger:
            find_best_match(
                [
                    {"title": "X", "link": "http://x"},
                    {"artist": None, "title": "Y", "link": "http://y"},
                    _DEEZER_GOOD,
                ],
                "Juana Molina",
                "DOGA",
                **_DEEZER_EXTRACTORS,
            )
        assert mock_logger.warning.call_count == 2

    def test_skipped_row_is_logged(self):
        from clients.streaming.matching import find_best_match

        with patch("clients.streaming.matching.logger") as mock_logger:
            find_best_match(
                [{"title": "X", "link": "http://x"}, _DEEZER_GOOD],
                "Juana Molina",
                "DOGA",
                **_DEEZER_EXTRACTORS,
            )
        mock_logger.warning.assert_called_once()

    def test_well_formed_response_does_not_log(self):
        """No spurious warnings when every row is well-formed (no behavior change)."""
        from clients.streaming.matching import find_best_match

        with patch("clients.streaming.matching.logger") as mock_logger:
            find_best_match(
                [_SPOTIFY_GOOD],
                "Juana Molina",
                "DOGA",
                **_SPOTIFY_EXTRACTORS,
            )
        mock_logger.warning.assert_not_called()

    def test_typed_match_sparse_row_skipped(self):
        """The sibling matcher on the orchestrator lookup path has the same
        unguarded shape — a malformed candidate must be skipped, not fatal."""
        from clients.streaming.matching import find_best_typed_match

        good = {"artist": {"name": "Juana Molina"}, "title": "DOGA"}
        bad = {"title": "DOGA"}  # no ``artist`` key → KeyError in artist_fn
        best = find_best_typed_match(
            [bad, good],
            query_artist="Juana Molina",
            query_title="DOGA",
            artist_fn=lambda x: x["artist"]["name"],
            title_fn=lambda x: x["title"],
        )
        assert best is good

    def test_typed_match_attribute_error_is_skipped(self):
        """The real orchestrator callers use attribute extractors
        (``lambda r: r.artist``) on objects; a ``None``/wrong-type item raises
        ``AttributeError`` — which must be caught, else the guard is inert for
        its only production call shape."""
        from clients.streaming.matching import find_best_typed_match

        good = SimpleNamespace(artist="Juana Molina", album="DOGA")
        best = find_best_typed_match(
            [None, good],  # None.artist -> AttributeError
            query_artist="Juana Molina",
            query_title="DOGA",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is good

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param({"artist": [], "title": "DOGA"}, id="index-error"),
            pytest.param({"artist": None, "title": "DOGA"}, id="type-error-null-subscript"),
        ],
    )
    def test_typed_match_index_and_type_errors_skipped(self, bad):
        """The typed guard must cover the same IndexError/TypeError shapes as
        ``find_best_match`` — its error set must not drift from the sibling's."""
        from clients.streaming.matching import find_best_typed_match

        good = {"artist": {"name": "Juana Molina"}, "title": "DOGA"}
        best = find_best_typed_match(
            [bad, good],
            query_artist="Juana Molina",
            query_title="DOGA",
            artist_fn=lambda x: (
                x["artist"][0] if isinstance(x["artist"], list) else x["artist"]["name"]
            ),
            title_fn=lambda x: x["title"],
        )
        assert best is good

    def test_typed_match_skip_on_variant_list_query_path(self):
        """The production caller (orchestrator) passes variant LISTS for the
        query, not scalars — exercise the guard on that branch too."""
        from clients.streaming.matching import find_best_typed_match

        good = {"artist": {"name": "Various Artists"}, "title": "Disco Not Disco"}
        bad = {"title": "Disco Not Disco"}  # KeyError in artist_fn
        best = find_best_typed_match(
            [bad, good],
            query_artist=["Various", "Various Artists"],
            query_title=["Disco Not Disco"],
            artist_fn=lambda x: x["artist"]["name"],
            title_fn=lambda x: x["title"],
        )
        assert best is good

    def test_typed_match_total_failure_returns_none_not_raises(self):
        """The typed matcher must NOT re-raise on total extraction failure —
        unlike ``find_best_match``. Its callers are the lookup pipeline
        (``lookup/orchestrator.py``): ``fetch_one`` would swallow a raise back
        to None and ``_library_miss_discogs_search`` would let it 500 the
        request, and neither maps it to an errored-source retry (LML#376 lives
        on the streaming path that find_best_match feeds). So a wholesale-
        malformed response must degrade to a graceful None, with every item
        still skipped+logged.
        """
        from clients.streaming.matching import find_best_typed_match

        with patch("clients.streaming.matching.logger") as mock_logger:
            best = find_best_typed_match(
                [None, None],  # each item: None.artist -> AttributeError, caught
                query_artist="Juana Molina",
                query_title="DOGA",
                artist_fn=lambda r: r.artist,
                title_fn=lambda r: r.album,
            )
        assert best is None
        assert mock_logger.warning.call_count == 2  # both caught, neither fatal


class TestRecordMatchTelemetry:
    """LML#592: emit the winning match's per-axis scores to Sentry.

    Instrumentation only — measures how often a match clears the 80/80 floor
    with a marginal artist score (80-90) against a high title (~100), the
    short-name-collision signature (Wand/Wanda). No threshold change.
    """

    def test_noop_without_active_transaction(self):
        """Outside a Sentry transaction the helper must not raise."""
        from clients.streaming.matching import record_match_telemetry

        # The real sentry_sdk.start_span returns a no-op span when uninitialized;
        # the helper must tolerate that (and any error) silently.
        record_match_telemetry(
            artist_score=88.89, title_score=100.0, service="apple_music", surface="album"
        )

    def test_emits_expected_span_data(self):
        from clients.streaming.matching import record_match_telemetry

        with patch("clients.streaming.matching.sentry_sdk") as mock_sentry:
            mock_span = MagicMock()
            mock_sentry.start_span.return_value.__enter__.return_value = mock_span
            record_match_telemetry(
                artist_score=88.89, title_score=100.0, service="apple_music", surface="album"
            )

        assert mock_sentry.start_span.call_args.kwargs["op"] == "matcher.match"
        data = {c.args[0]: c.args[1] for c in mock_span.set_data.call_args_list}
        assert data["matcher.service"] == "apple_music"
        assert data["matcher.surface"] == "album"
        assert data["matcher.artist_score"] == 88.89
        assert data["matcher.title_score"] == 100.0
        assert data["matcher.marginal_artist_clear"] is True

    def test_telemetry_failure_is_swallowed(self):
        """A telemetry error must never propagate into the lookup path."""
        from clients.streaming.matching import record_match_telemetry

        with patch("clients.streaming.matching.sentry_sdk") as mock_sentry:
            mock_sentry.start_span.side_effect = RuntimeError("sentry down")
            # Must not raise.
            record_match_telemetry(
                artist_score=88.89, title_score=100.0, service="spotify", surface="album"
            )

    @pytest.mark.parametrize(
        "artist_score, title_score, expected",
        [
            pytest.param(88.89, 100.0, True, id="wand-wanda-marginal"),
            pytest.param(85.71, 100.0, True, id="hyd-hyde-marginal"),
            pytest.param(95.0, 100.0, False, id="artist-clearly-above-band"),
            pytest.param(85.0, 80.0, False, id="title-not-high"),
            pytest.param(80.0, 80.0, False, id="both-at-floor"),
            pytest.param(88.0, 96.0, True, id="inside-both-bands"),
            pytest.param(90.0, 100.0, False, id="artist-at-ceiling-excluded"),
        ],
    )
    def test_marginal_artist_clear_classification(self, artist_score, title_score, expected):
        from clients.streaming.matching import record_match_telemetry

        with patch("clients.streaming.matching.sentry_sdk") as mock_sentry:
            mock_span = MagicMock()
            mock_sentry.start_span.return_value.__enter__.return_value = mock_span
            record_match_telemetry(
                artist_score=artist_score,
                title_score=title_score,
                service="apple_music",
                surface="track",
            )

        data = {c.args[0]: c.args[1] for c in mock_span.set_data.call_args_list}
        assert data["matcher.marginal_artist_clear"] is expected

    def test_marginal_clear_attaches_query_and_matched_strings(self):
        """LML#592 labeling: a marginal clear carries the actual name pair.

        The score alone can't separate an organic false match (Wand -> Wanda)
        from a legitimate one (Wand -> WAND with a casing/punctuation diff) —
        both can land at ~88. Attaching the request-vs-matched strings to the
        marginal sample is what lets us hand-label the false-positive rate
        before deciding whether to tighten the floor (Part 2).
        """
        from clients.streaming.matching import record_match_telemetry

        with patch("clients.streaming.matching.sentry_sdk") as mock_sentry:
            mock_span = MagicMock()
            mock_sentry.start_span.return_value.__enter__.return_value = mock_span
            record_match_telemetry(
                artist_score=88.89,
                title_score=100.0,
                service="apple_music",
                surface="album",
                query_artist="Wand",
                matched_artist="Wanda",
                query_title="DOGA",
                matched_title="DOGA",
            )

        data = {c.args[0]: c.args[1] for c in mock_span.set_data.call_args_list}
        assert data["matcher.marginal_artist_clear"] is True
        assert data["matcher.query_artist"] == "Wand"
        assert data["matcher.matched_artist"] == "Wanda"
        assert data["matcher.query_title"] == "DOGA"
        assert data["matcher.matched_title"] == "DOGA"

    def test_marginal_clear_without_label_strings_warns(self):
        """A marginal clear missing its label strings must not be silent.

        ``service=`` was made a *required* kwarg (commit 6cf01c1) so a forgotten
        label fails loudly rather than rotting telemetry silently. The label
        strings stay optional — they are used only on the marginal subset, so
        requiring them on every call would over-couple non-marginal callers —
        but the same silent-null failure mode is guarded here: a marginal clear
        that reaches the span without labels emits an observable warning instead
        of four ``None`` fields with no signal. The helper must never *raise*
        (telemetry is best-effort), so a warning is the loudest safe signal.
        """
        from clients.streaming.matching import record_match_telemetry

        with (
            patch("clients.streaming.matching.sentry_sdk") as mock_sentry,
            patch("clients.streaming.matching.logger") as mock_logger,
        ):
            mock_span = MagicMock()
            mock_sentry.start_span.return_value.__enter__.return_value = mock_span
            record_match_telemetry(
                artist_score=88.89,  # marginal artist
                title_score=100.0,  # high title -> marginal clear
                service="apple_music",
                surface="album",
                # query_/matched_ strings omitted -> the labelability gap
            )

        mock_logger.warning.assert_called_once()
        # Pin that it warned about THIS gap (not some unrelated future warning),
        # and that the message carries the attribution needed to find the caller.
        fmt, *args = mock_logger.warning.call_args.args
        assert "without label strings" in fmt
        assert "apple_music" in args and "album" in args

    def test_non_marginal_clear_without_strings_does_not_warn(self):
        """The warning fires only on the labelable (marginal) path.

        A non-marginal clear never carries labels by design, so omitting them is
        correct, not a gap — it must not warn.
        """
        from clients.streaming.matching import record_match_telemetry

        with (
            patch("clients.streaming.matching.sentry_sdk") as mock_sentry,
            patch("clients.streaming.matching.logger") as mock_logger,
        ):
            mock_span = MagicMock()
            mock_sentry.start_span.return_value.__enter__.return_value = mock_span
            record_match_telemetry(
                artist_score=100.0,  # strong artist -> not marginal
                title_score=100.0,
                service="apple_music",
                surface="album",
            )

        mock_logger.warning.assert_not_called()

    def test_non_marginal_clear_omits_strings(self):
        """Strings ride only the marginal sample — not every winning match.

        A strong artist score (>= ceiling) is not the collision signature, so
        attaching names there would add name data to the whole match stream for
        no labeling value. Keep the common path scores-only.
        """
        from clients.streaming.matching import record_match_telemetry

        with patch("clients.streaming.matching.sentry_sdk") as mock_sentry:
            mock_span = MagicMock()
            mock_sentry.start_span.return_value.__enter__.return_value = mock_span
            record_match_telemetry(
                artist_score=100.0,
                title_score=100.0,
                service="apple_music",
                surface="album",
                query_artist="Stereolab",
                matched_artist="Stereolab",
                query_title="Aluminum Tunes",
                matched_title="Aluminum Tunes",
            )

        data = {c.args[0]: c.args[1] for c in mock_span.set_data.call_args_list}
        assert data["matcher.marginal_artist_clear"] is False
        assert "matcher.query_artist" not in data
        assert "matcher.matched_artist" not in data
        assert "matcher.query_title" not in data
        assert "matcher.matched_title" not in data


class TestFindBestSourceMatchEmits:
    """LML#592: the album chokepoint emits match telemetry for the winner."""

    _WAND = [{"artist": "Wanda", "album": "DOGA", "url": "http://w"}]

    def test_emits_marginal_clear_for_album_winner(self):
        from clients.streaming.matching import find_best_source_match

        with patch("clients.streaming.matching.sentry_sdk") as mock_sentry:
            mock_span = MagicMock()
            mock_sentry.start_span.return_value.__enter__.return_value = mock_span
            match = find_best_source_match(
                self._WAND,
                "Wand",
                "DOGA",
                artist_fn=lambda r: r["artist"],
                title_fn=lambda r: r["album"],
                url_fn=lambda r: r["url"],
                service="deezer",
            )

        assert match is not None  # still clears — this PR instruments, it does not reject
        data = {c.args[0]: c.args[1] for c in mock_span.set_data.call_args_list}
        assert data["matcher.surface"] == "album"
        assert data["matcher.service"] == "deezer"
        assert data["matcher.marginal_artist_clear"] is True
        # LML#592 labeling: the request value and the matched-row value, so a
        # production sample of these marginal clears can be hand-labeled.
        assert data["matcher.query_artist"] == "Wand"
        assert data["matcher.matched_artist"] == "Wanda"
        assert data["matcher.query_title"] == "DOGA"
        assert data["matcher.matched_title"] == "DOGA"

    def test_no_emit_when_nothing_clears(self):
        from clients.streaming.matching import find_best_source_match

        with patch("clients.streaming.matching.sentry_sdk") as mock_sentry:
            match = find_best_source_match(
                [{"artist": "Cat Power", "album": "Moon Pix", "url": "http://x"}],
                "Autechre",
                "Confield",
                artist_fn=lambda r: r["artist"],
                title_fn=lambda r: r["album"],
                url_fn=lambda r: r["url"],
                service="deezer",
            )
        assert match is None
        mock_sentry.start_span.assert_not_called()

    def test_total_extraction_failure_propagates_and_skips_telemetry(self):
        """The wrapper is the real streaming seam (every adapter funnels through
        it), so pin that a total extraction failure propagates the re-raise out
        of ``find_best_source_match`` — that is what reaches the orchestrator's
        errored-source mapping (LML#376) — and does NOT emit match telemetry
        (there is no winner to record).
        """
        from clients.streaming.matching import find_best_source_match

        with patch("clients.streaming.matching.sentry_sdk") as mock_sentry:
            with pytest.raises((KeyError, IndexError, TypeError, AttributeError)):
                find_best_source_match(
                    [{"title": "X", "link": "http://x"}],  # all malformed (Deezer shape)
                    "Juana Molina",
                    "DOGA",
                    artist_fn=lambda x: x["artist"]["name"],
                    title_fn=lambda x: x["title"],
                    url_fn=lambda x: x["link"],
                    service="deezer",
                )
        mock_sentry.start_span.assert_not_called()

    def test_service_is_required(self):
        """``service`` must be a required keyword arg, never silently defaulted.

        A defaulted ``service="unknown"`` once let an adapter's album matches
        (Apple's) drop into an unattributable telemetry bucket instead of
        failing loudly — telemetry is best-effort and would have swallowed the
        mistake at runtime (LML#592). Pin the required contract so re-adding a
        default is caught here, not in production data.
        """
        from clients.streaming.matching import find_best_source_match

        with pytest.raises(TypeError):
            find_best_source_match(
                self._WAND,
                "Wand",
                "DOGA",
                artist_fn=lambda r: r["artist"],
                title_fn=lambda r: r["album"],
                url_fn=lambda r: r["url"],
            )  # no service= → TypeError


class TestAcceptanceFloorUnchanged:
    """LML#592 is instrument-only: the 80/80 floor is NOT tightened in this PR.

    These pins fail loudly if a future edit changes acceptance while only
    telemetry was intended. The Wand/Wanda short-name collision must still
    CLEAR — we measure it now; rejecting it is a gated follow-up.
    """

    def test_floor_constant_unchanged(self):
        from clients.streaming.matching import SCORE_MATCH_ACCEPTANCE_FLOOR

        assert SCORE_MATCH_ACCEPTANCE_FLOOR == 80.0

    def test_wand_wanda_still_clears_predicate(self):
        from clients.streaming.matching import is_acceptable_match, score_match

        artist = score_match("Wand", "Wanda")
        assert artist == pytest.approx(88.89, abs=0.01)
        assert is_acceptable_match(artist, score_match("DOGA", "DOGA")) is True

    def test_wand_wanda_still_selected_as_album_match(self):
        from clients.streaming.matching import find_best_source_match

        match = find_best_source_match(
            [{"artist": "Wanda", "album": "DOGA", "url": "http://w"}],
            "Wand",
            "DOGA",
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["album"],
            url_fn=lambda r: r["url"],
            service="deezer",
        )
        assert match is not None
        assert match.url == "http://w"


class TestFindBestTypedMatch:
    """Tests for find_best_typed_match — returns the original typed object."""

    def test_returns_highest_combined_score_original(self):
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        slightly_off = make_discogs_result(
            release_id=1, album="Aluminium Tunes", artist="Stereolab"
        )
        exact = make_discogs_result(release_id=2, album="Aluminum Tunes", artist="Stereolab")
        best = find_best_typed_match(
            [slightly_off, exact],
            query_artist="Stereolab",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is exact

    def test_returns_none_when_no_candidate_clears_floor(self):
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        candidates = [
            make_discogs_result(release_id=1, album="Moon Pix", artist="Cat Power"),
            make_discogs_result(release_id=2, album="Greatest Hits", artist="Queen"),
        ]
        best = find_best_typed_match(
            candidates,
            query_artist="Autechre",
            query_title="Confield",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is None

    def test_returns_none_for_empty_iterable(self):
        from clients.streaming.matching import find_best_typed_match

        best = find_best_typed_match(
            [],
            query_artist="Stereolab",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: "",
            title_fn=lambda r: "",
        )
        assert best is None

    def test_handles_none_fields_on_candidate(self):
        """A candidate whose artist or title extractor returns None scores as 0
        and is rejected — not raised."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        # DiscogsSearchResult.artist defaults to None; verify it doesn't blow up.
        broken = make_discogs_result(release_id=1, album="Aluminum Tunes", artist=None)
        good = make_discogs_result(release_id=2, album="Aluminum Tunes", artist="Stereolab")
        best = find_best_typed_match(
            [broken, good],
            query_artist="Stereolab",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is good

    def test_first_result_wins_on_tie(self):
        """Ties resolve to the first candidate reaching the score — mirrors
        find_best_match's order-preserving behavior."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        first = make_discogs_result(release_id=1, album="Aluminum Tunes", artist="Stereolab")
        second = make_discogs_result(release_id=2, album="Aluminum Tunes", artist="Stereolab")
        best = find_best_typed_match(
            [first, second],
            query_artist="Stereolab",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is first

    def test_query_artist_accepts_variant_list_and_takes_max(self):
        """When the search-query form ('Various') differs from the canonical
        form Discogs returns ('Various Artists'), passing both variants lets
        the score clear the floor — score_match('Various', 'Various Artists')
        is 63.6, but score_match('Various Artists', 'Various Artists') is
        100, and the max wins."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        candidate = make_discogs_result(
            release_id=42, album="Disco Not Disco", artist="Various Artists"
        )
        best = find_best_typed_match(
            [candidate],
            query_artist=["Various", "Various Artists"],
            query_title="Disco Not Disco",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is candidate

    def test_query_title_accepts_variant_list_and_takes_max(self):
        """When the search-query form (a long canonical title) differs from
        the form the candidate carries (short library title), passing both
        variants lets the score clear the floor."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        candidate = make_discogs_result(release_id=42, album="Disco Not Disco", artist="Various")
        best = find_best_typed_match(
            [candidate],
            query_artist="Various",
            query_title=[
                "Disco Not Disco (Post Punk, Electro & Leftfield Disco Classics)",
                "Disco Not Disco",
            ],
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is candidate

    def test_returns_none_when_query_artist_is_empty_string(self):
        """score_match('','') returns 100 by rapidfuzz convention. Without a
        guard, a candidate with r.artist=None (coerced to '') and r.album=''
        would trivially clear 100/100 against an empty query. Defense against
        sparse library rows + malformed Discogs candidates."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        # Both candidate fields parse as empty/None.
        junk = make_discogs_result(release_id=99, album="", artist=None)
        best = find_best_typed_match(
            [junk],
            query_artist="",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is None

    def test_returns_none_when_query_title_is_empty_string(self):
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        junk = make_discogs_result(release_id=99, album=None, artist="")
        best = find_best_typed_match(
            [junk],
            query_artist="Stereolab",
            query_title="",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is None

    def test_returns_none_when_every_variant_is_empty(self):
        """Variant lists with only empty strings are equivalent to an empty
        query and short-circuit to no-match."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        junk = make_discogs_result(release_id=99, album="", artist="")
        best = find_best_typed_match(
            [junk],
            query_artist=["", ""],
            query_title="Foo",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is None

    def test_returns_none_when_query_artist_is_whitespace_only(self):
        """``score_match(" ", "")`` returns 100 because the title normalizer
        strips whitespace before scoring. The empty-variant filter rejects
        whitespace-only strings (not just literal empty strings) so a
        sparse-but-not-empty library field can't sneak past the floor."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        junk = make_discogs_result(release_id=99, album="Aluminum Tunes", artist="")
        best = find_best_typed_match(
            [junk],
            query_artist=" ",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is None

    def test_returns_none_when_every_variant_is_whitespace(self):
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        junk = make_discogs_result(release_id=99, album="Aluminum Tunes", artist="Stereolab")
        best = find_best_typed_match(
            [junk],
            query_artist=["  ", "\t"],
            query_title="Aluminum Tunes",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is None

    def test_mixed_variants_keeps_non_empty(self):
        """When variants are mixed (empty/whitespace + real), the real ones
        are kept and used for scoring — the empty/whitespace entries are
        dropped silently rather than blocking the call."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        candidate = make_discogs_result(release_id=42, album="Aluminum Tunes", artist="Stereolab")
        best = find_best_typed_match(
            [candidate],
            query_artist=["", "Stereolab", "  "],
            query_title=["Aluminum Tunes", ""],
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is candidate


class TestFindBestTypedMatchCandidateVariants:
    """Candidate-side artist variants (LML#784 A2).

    The PG cache arm presents the aggregated release credit ("Fust, Merce
    Lemon") plus the per-credit list (``artist_credits``). ``artist_fn`` may
    return an iterable of variants; the artist axis scores as the max across
    them — mirroring the query-side variant support. This is what lets a
    single-credit query (library item artist "Fust") keep matching a
    multi-artist release after the aggregation, without loosening any floor:
    a variant passes only when the queried artist is genuinely one of the
    release's credits.
    """

    def test_single_credit_query_passes_via_per_credit_variant(self):
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        joined = make_discogs_result(
            release_id=36830641,
            album="Cup Of Loneliness / Choices",
            artist="Fust, Merce Lemon",
        )
        best = find_best_typed_match(
            [joined],
            query_artist="Fust",
            query_title="Cup Of Loneliness / Choices",
            artist_fn=lambda r: [r.artist, "Fust", "Merce Lemon"],
            title_fn=lambda r: r.album,
        )
        assert best is joined

    def test_joined_credit_query_passes_via_joined_variant(self):
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        joined = make_discogs_result(
            release_id=36830641,
            album="Cup Of Loneliness / Choices",
            artist="Fust, Merce Lemon",
        )
        best = find_best_typed_match(
            [joined],
            query_artist="Merce Lemon & Fust",
            query_title="Cup of Loneliness / Choices",
            artist_fn=lambda r: [r.artist, "Fust", "Merce Lemon"],
            title_fn=lambda r: r.album,
        )
        assert best is joined

    def test_unrelated_query_still_rejected_with_variants(self):
        """Variants only admit genuine credits — an unrelated artist still fails."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        joined = make_discogs_result(
            release_id=36830641,
            album="Cup Of Loneliness / Choices",
            artist="Fust, Merce Lemon",
        )
        best = find_best_typed_match(
            [joined],
            query_artist="Jessica Pratt",
            query_title="Cup Of Loneliness / Choices",
            artist_fn=lambda r: [r.artist, "Fust", "Merce Lemon"],
            title_fn=lambda r: r.album,
        )
        assert best is None

    def test_empty_variant_iterable_scores_zero(self):
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        candidate = make_discogs_result(release_id=1, album="Aluminum Tunes", artist="Stereolab")
        best = find_best_typed_match(
            [candidate],
            query_artist="Stereolab",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: [],
            title_fn=lambda r: r.album,
        )
        assert best is None

    def test_none_and_empty_variants_are_filtered(self):
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        candidate = make_discogs_result(release_id=1, album="Aluminum Tunes", artist="Stereolab")
        best = find_best_typed_match(
            [candidate],
            query_artist="Stereolab",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: [None, "", "  ", r.artist],
            title_fn=lambda r: r.album,
        )
        assert best is candidate

    def test_plain_str_extractor_behavior_unchanged(self):
        """Non-regression pin: a plain-`str` extractor scores exactly as before —
        floor-pass and floor-fail cases both preserved."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        passing = make_discogs_result(release_id=1, album="DOGA", artist="Juana Molina")
        best = find_best_typed_match(
            [passing],
            query_artist="Juana Molina",
            query_title="DOGA",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is passing

        failing = make_discogs_result(
            release_id=2, album="Cup Of Loneliness / Choices", artist="Merce Lemon"
        )
        best = find_best_typed_match(
            [failing],
            query_artist="Merce Lemon & Fust",
            query_title="Cup Of Loneliness / Choices",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is None


class TestStripThePrefix:
    @pytest.mark.parametrize(
        "name, expected",
        [
            pytest.param("The Afros", "Afros", id="strips-the"),
            pytest.param("Stereolab", "Stereolab", id="no-the"),
            pytest.param("The The", "The", id="the-the"),
            pytest.param("Theodore", "Theodore", id="the-substring-preserved"),
            pytest.param("", "", id="empty"),
        ],
    )
    def test_strip_the_prefix(self, name, expected):
        assert strip_the_prefix(name) == expected

    def test_scope_stays_narrower_than_crate_leading_article_strip(self):
        """LML#1042 landmine 5 pin: only "The " is stripped here, not "A"/"An".

        ``wxyc_etl.text.strip_leading_article`` drops "the"/"a"/"an", but
        this function backs ``score_match``'s fallback rescoring, which
        picks the streaming candidate that gets persisted as the winning
        URL. Widening the scope would change which candidate wins on
        "A Tribe Called Quest" vs "Tribe Called Quest"-shaped ties — a live
        match-quality behavior change that deserves its own measurement,
        not a silent ride-along in a normalization-consolidation refactor.
        See the module-level comment above ``_THE_PREFIX_RE``.
        """
        assert strip_the_prefix("A Tribe Called Quest") == "A Tribe Called Quest"
        assert strip_the_prefix("An Cat Dubh") == "An Cat Dubh"
