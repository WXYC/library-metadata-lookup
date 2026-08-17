"""Unit tests for lookup/wikipedia_candidates.py — the Wikipedia candidate
scoring/rejection policy extracted from lookup/wikipedia_url.py (LML#1192
review round 6, once that module grew past its module-budget ceiling).

Covers URL/title parsing (moved verbatim from lookup/wikipedia_url.py, no
behavior change), plus two review round 6 fixes and their regression guards:

* P2-2 — the hard-reject denylist's fetch-capable/synchronous asymmetry
  (``include_denylisted``): a denylisted slug (``Film``, ``Single``, ...) is
  still excluded outright for the synchronous path (the default), but
  admitted-and-deprioritized for a fetch-capable caller.
* P2-3 — ``normalize_lang_subdomain``: a mobile host (``en.m``) normalizes
  to its real language code, fixing the ``lang == "en"`` sort tiebreak, the
  REST API host, and the stored ``lang`` column all at once.
"""

from __future__ import annotations

import pytest

from lookup.wikipedia_candidates import (
    ScoredCandidate,
    candidate_sort_key,
    is_hard_rejected_slug,
    normalize_lang_subdomain,
    score_candidates,
    wikipedia_title_from_url,
)

# ---------------------------------------------------------------------------
# wikipedia_title_from_url — the URL -> title conversion the warm task and
# the offline drain both need to call clients.wikipedia.WikipediaClient
# (moved verbatim from tests/unit/test_wikipedia_url.py)
# ---------------------------------------------------------------------------


class TestWikipediaTitleFromUrl:
    def test_extracts_decoded_space_form_title(self):
        assert (
            wikipedia_title_from_url("https://en.wikipedia.org/wiki/Duke_Ellington")
            == "Duke Ellington"
        )

    def test_url_encoded_characters_are_decoded(self):
        assert (
            wikipedia_title_from_url("https://en.wikipedia.org/wiki/Sessa_%282%29") == "Sessa (2)"
        )

    def test_strips_query_and_fragment(self):
        assert (
            wikipedia_title_from_url(
                "https://en.wikipedia.org/wiki/Stereolab?action=history#History"
            )
            == "Stereolab"
        )

    def test_non_wikipedia_url_returns_none(self):
        assert wikipedia_title_from_url("https://stereolab.example") is None

    def test_empty_slug_returns_none(self):
        assert wikipedia_title_from_url("https://en.wikipedia.org/wiki/") is None


# ---------------------------------------------------------------------------
# P2-2: is_hard_rejected_slug -- the regex itself is UNCHANGED by this round
# (still over-broad: rejects a bare word that IS a plausible artist name,
# not just a genuine trailing type-qualifier phrase). That is documented as
# a known, accepted property here -- the round-6 fix is in how the callers
# below USE this classification, never in narrowing the regex itself (a
# smarter regex still can't distinguish "artist literally named Film" from
# "Sessa's eponymous soundtrack page" without the live payload).
# ---------------------------------------------------------------------------


class TestIsHardRejectedSlug:
    @pytest.mark.parametrize(
        "slug",
        [
            # LML#1192 review round 6, P2-2 -- the exact reviewer-executed
            # strings, each confirmed True against the shipped code.
            "Film",
            "Single",
            "Ep",
            "Mixtape",
            "Compilation",
            "Discography",
            "Simple Song",
        ],
    )
    def test_still_classifies_these_as_denylisted(self, slug):
        assert is_hard_rejected_slug(slug) is True

    def test_an_ordinary_artist_name_is_not_denylisted(self):
        assert is_hard_rejected_slug("Stereolab") is False


# ---------------------------------------------------------------------------
# P2-3: normalize_lang_subdomain
# ---------------------------------------------------------------------------


class TestNormalizeLangSubdomain:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("en", "en"),
            # LML#1192 review round 6, P2-3 -- the exact reviewer-executed
            # case: a mobile host's dotted subdomain normalizes to the
            # REAL language code that precedes ".m", not left as "en.m".
            ("en.m", "en"),
            ("es.m", "es"),
            # LML#1192 review round 4, P0-7 -- must still hold.
            ("www", "en"),
            # Legitimate editions with no dot at all must pass through
            # untouched -- neither a hyphenated code nor a single-label
            # edition name is a "prefix label glued onto a real code".
            ("simple", "simple"),
            ("zh-min-nan", "zh-min-nan"),
            # Defensive: a bare "m" with nothing before it is not stripped
            # to an empty string.
            ("m", "m"),
        ],
    )
    def test_normalizes_to_the_real_language_code(self, raw, expected):
        assert normalize_lang_subdomain(raw) == expected


# ---------------------------------------------------------------------------
# P2-3 end-to-end: the exact reviewer-executed ranking regression
# ---------------------------------------------------------------------------


class TestMobileHostRankingRegression:
    def test_mobile_host_no_longer_loses_the_english_tiebreak(self):
        # LML#1192 review round 6, P2-3: before the fix, ranking
        # ["https://en.m.wikipedia.org/wiki/Sun_Ra",
        #  "https://es.wikipedia.org/wiki/Sun_Ra"] against "Sun Ra" put the
        # Spanish URL first -- lang="en.m" != "en" lost the tiebreak it
        # should have won. The same two URLs minus ".m" correctly rank
        # English first; this must now match.
        mobile_urls = [
            "https://en.m.wikipedia.org/wiki/Sun_Ra",
            "https://es.wikipedia.org/wiki/Sun_Ra",
        ]
        ranked = sorted(
            score_candidates(mobile_urls, "Sun Ra"), key=candidate_sort_key, reverse=True
        )
        assert ranked[0].url == "https://en.m.wikipedia.org/wiki/Sun_Ra"
        assert ranked[0].lang == "en"

    def test_matches_the_non_mobile_control_ranking(self):
        control_urls = [
            "https://en.wikipedia.org/wiki/Sun_Ra",
            "https://es.wikipedia.org/wiki/Sun_Ra",
        ]
        mobile_urls = [
            "https://en.m.wikipedia.org/wiki/Sun_Ra",
            "https://es.wikipedia.org/wiki/Sun_Ra",
        ]
        control_ranked = sorted(
            score_candidates(control_urls, "Sun Ra"), key=candidate_sort_key, reverse=True
        )
        mobile_ranked = sorted(
            score_candidates(mobile_urls, "Sun Ra"), key=candidate_sort_key, reverse=True
        )
        assert [c.lang for c in control_ranked] == [c.lang for c in mobile_ranked]


# ---------------------------------------------------------------------------
# P2-2: score_candidates's include_denylisted asymmetry
# ---------------------------------------------------------------------------


class TestScoreCandidatesDenylistAsymmetry:
    def test_default_excludes_a_denylisted_candidate_exactly_like_round_5(self):
        # LML#1192 review round 6, P2-2: include_denylisted defaults to
        # False, preserving the synchronous request path's decisive
        # hard-reject byte for byte -- a caller that never opts in sees the
        # exact round-5 behavior.
        assert score_candidates(["https://en.wikipedia.org/wiki/Film"], "Film") == []

    def test_opting_in_admits_the_candidate_flagged_denylisted(self):
        scored = score_candidates(
            ["https://en.wikipedia.org/wiki/Film"], "Film", include_denylisted=True
        )
        assert len(scored) == 1
        assert scored[0] == ScoredCandidate(
            url="https://en.wikipedia.org/wiki/Film", lang="en", score=100.0, denylisted=True
        )

    def test_a_non_denylisted_candidate_is_unaffected_either_way(self):
        for include_denylisted in (False, True):
            scored = score_candidates(
                ["https://en.wikipedia.org/wiki/Stereolab"],
                "Stereolab",
                include_denylisted=include_denylisted,
            )
            assert len(scored) == 1
            assert scored[0].denylisted is False


class TestCandidateSortKeyDenylistOrdering:
    def test_a_denylisted_candidate_sorts_after_a_non_denylisted_one_regardless_of_score(self):
        # LML#1192 review round 6, P2-2: "sorts last but stays reachable" --
        # even a LOWER-scoring non-denylisted candidate must outrank a
        # HIGHER-scoring denylisted one, so a fetch-capable caller always
        # tries every legitimate candidate before ever spending a
        # max_candidates slot on a denylisted guess.
        denylisted_high_score = ScoredCandidate(
            url="https://en.wikipedia.org/wiki/Film", lang="en", score=100.0, denylisted=True
        )
        non_denylisted_low_score = ScoredCandidate(
            url="https://en.wikipedia.org/wiki/Some_Other_Page",
            lang="en",
            score=81.0,
            denylisted=False,
        )
        ranked = sorted(
            [denylisted_high_score, non_denylisted_low_score], key=candidate_sort_key, reverse=True
        )
        assert ranked[0] is non_denylisted_low_score
        assert ranked[1] is denylisted_high_score

    def test_two_non_denylisted_candidates_are_unaffected(self):
        # A no-op for the synchronous path (never admits a denylisted
        # candidate in the first place) -- both candidates share
        # denylisted=False, so the leading tuple element never
        # distinguishes them and score still decides.
        higher = ScoredCandidate(url="https://en.wikipedia.org/wiki/A", lang="en", score=95.0)
        lower = ScoredCandidate(url="https://en.wikipedia.org/wiki/B", lang="en", score=85.0)
        ranked = sorted([lower, higher], key=candidate_sort_key, reverse=True)
        assert ranked == [higher, lower]
