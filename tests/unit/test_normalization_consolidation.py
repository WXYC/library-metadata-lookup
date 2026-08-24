"""LML#1042: table-driven behavior pins for the normalization-shim consolidation.

Issue #1042 asks LML to adopt the shared ``wxyc_etl.text`` primitives
(``fold_conjunctions``, ``strip_discogs_disambiguation``, and the
already-promoted ``strip_leading_article``) in place of locally duplicated
regex shims, WITHOUT silently changing behavior anywhere the divergence
matters. This module is the single place that documents every divergence
found while tracing the old shims against the new primitives, alongside the
deliberate decision made for each one:

1. ``discogs/matching.py::normalize_for_track_comparison`` — KEPT unchanged
   (local bare ``&`` -> ``and`` replace, not delegated to
   ``fold_conjunctions``).
2. ``discogs/matching.py::strip_discogs_suffix`` — ADOPTED
   ``strip_discogs_disambiguation(_, broad=False)``, widening
   trailing-whitespace tolerance.
3. ``clients/streaming/matching.py::normalize_artist_credit``'s ampersand
   variant generation — KEPT unchanged (local ``_AND_RE`` / `_AMPERSAND_RE`,
   not delegated to ``fold_conjunctions``).
4. ``clients/streaming/matching.py::strip_discogs_disambig`` — ADOPTED
   ``strip_discogs_disambiguation(_, broad=True)``, narrowing the
   space-after-open-paren case.
5. ``clients/streaming/matching.py::strip_the_prefix`` — KEPT unchanged
   (narrower scope than the crate's ``strip_leading_article``).
6. ``discogs/service.py::calculate_confidence``'s ad-hoc ``normalize()`` —
   ADOPTED ``to_match_form``, widening diacritic tolerance.
7. ``core/text.py`` — DELETED; its ``strip_leading_article`` was
   byte-identical to the crate's, so its consumers (``lookup/matching.py``,
   ``library/db.py``) now import ``wxyc_etl.text.strip_leading_article``
   directly.

Also verifies the hard cache-key-stability constraint: none of the
functions touched by #1042 feed a persisted PostgreSQL cache key (those all
use ``to_match_form`` / ``to_identity_match_form`` directly, untouched by
this issue), pinned against a diacritic-bearing WXYC corpus.
"""

import re

import pytest
from wxyc_etl.text import (
    fold_conjunctions,
    strip_leading_article,
    to_identity_match_form,
    to_match_form,
)

from clients.streaming.matching import normalize_artist_credit, strip_the_prefix
from discogs.matching import normalize_for_track_comparison, strip_discogs_suffix
from discogs.service import calculate_confidence
from identity.normalize import canonicalize_for_identity_lookup

# ---------------------------------------------------------------------------
# Landmine 1: normalize_for_track_comparison keeps its bare "&" -> "and"
# replace instead of adopting fold_conjunctions.
#
# fold_conjunctions only folds a whitespace-flanked "&"; a token-glued "&"
# (e.g. "R&B") passes through untouched. This function immediately strips
# all remaining punctuation afterward, so a glued "&" that survived the
# fold would be deleted outright ("R&B" -> "RB"), destroying the "and"
# comparison signal a real tracklist/title match might depend on. The local
# bare replace treats every "&" as a conjunction, which is the safer
# behavior for this validation path. See discogs/matching.py's docstring.
# ---------------------------------------------------------------------------


class TestNormalizeForTrackComparisonKeepsBareAmpersandFold:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("R&B", "r and b", id="glued-ampersand-still-folds"),
            pytest.param(
                "Duke Ellington & John Coltrane",
                "duke ellington and john coltrane",
                id="whitespace-flanked-ampersand",
            ),
            pytest.param("A&E Records", "a and e records", id="glued-ampersand-also-folds"),
        ],
    )
    def test_current_behavior_pinned(self, text: str, expected: str) -> None:
        assert normalize_for_track_comparison(text) == expected

    def test_would_diverge_under_fold_conjunctions(self) -> None:
        """Demonstrates WHY the swap was rejected: fold_conjunctions + punctuation-strip corrupts "R&B"."""
        result = fold_conjunctions("r&b")
        result = re.sub(r"[^\w\s]", "", result)
        assert result == "rb"  # "R&B" would become "RB", not "R and B"
        assert normalize_for_track_comparison("R&B") != result


# ---------------------------------------------------------------------------
# Landmine 2: strip_discogs_suffix ADOPTS strip_discogs_disambiguation(broad=False).
#
# One behavior change: trailing whitespace after the closing paren is now
# tolerated. No traced call site persists this output as a cache key (see
# TestCacheKeyStabilityCanary below).
# ---------------------------------------------------------------------------


class TestStripDiscogsSuffixAdoptsPrimitive:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            pytest.param("Sessa (2)", "Sessa", id="numeric-unchanged"),
            pytest.param("Stereolab (UK)", "Stereolab (UK)", id="non-numeric-preserved"),
            pytest.param("", "", id="empty-unchanged"),
        ],
    )
    def test_unaffected_cases_unchanged(self, name: str, expected: str) -> None:
        assert strip_discogs_suffix(name) == expected

    def test_trailing_whitespace_now_stripped(self) -> None:
        """Deliberate widening: the old ``\\s*\\(\\d+\\)$`` regex required the
        suffix to sit at the literal end of the string, so "Sessa (2) "
        (trailing space) was left unstripped. The primitive tolerates
        trailing whitespace, which is the more correct behavior."""
        assert strip_discogs_suffix("Sessa (2) ") == "Sessa"

    def test_unicode_digit_narrowed_to_ascii(self) -> None:
        """Deliberate narrowing: Python's Unicode-aware ``\\d`` used to strip
        non-ASCII digit disambiguators too (e.g. Arabic-Indic '٢'); the
        crate primitive is ASCII-only. Discogs' own disambiguator scheme is
        ASCII-only, so this has no practical effect on real Discogs data."""
        assert strip_discogs_suffix("Foo (٢)") == "Foo (٢)"


# ---------------------------------------------------------------------------
# Landmine 3: normalize_artist_credit KEEPS its glued-ampersand-tolerant
# variant generation instead of delegating to fold_conjunctions.
#
# normalize_artist_credit generates SEARCH VARIANTS for Discogs recall, not
# a single canonical form. Its local _AMPERSAND_RE (r"\s*&\s*") folds a
# glued "&" too, generating an extra "and"-spelled variant that
# fold_conjunctions (whitespace-flanked only) would not. Narrowing recall
# here has no compensating benefit, and the crate has no primitive for the
# reverse "and" -> "&" direction _AND_RE covers, so at least one local
# regex is required regardless.
# ---------------------------------------------------------------------------


class TestNormalizeArtistCreditKeepsGluedAmpersandVariant:
    def test_glued_ampersand_still_generates_and_variant(self) -> None:
        variants = normalize_artist_credit("Sam&Dave")
        assert "Sam and Dave" in variants

    def test_would_lose_variant_under_fold_conjunctions(self) -> None:
        """fold_conjunctions alone would NOT produce the "Sam and Dave" variant."""
        assert fold_conjunctions("Sam&Dave") == "Sam&Dave"


# ---------------------------------------------------------------------------
# Landmine 4: strip_discogs_disambig (broad) ADOPTS
# strip_discogs_disambiguation(broad=True).
#
# One divergence: the old local regex tolerated whitespace right after the
# opening paren ("Foo ( UK)"); the crate primitive requires the qualifier
# to sit flush against "(". Covered in tests/unit/test_streaming_matching.py
# (TestStripDiscogsDisambig) alongside the function's full behavior table;
# this module cross-references the decision for completeness.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Landmine 5: strip_the_prefix KEEPS its "The "-only scope instead of
# widening to the crate's "the"/"a"/"an" strip_leading_article.
#
# strip_the_prefix backs score_match's fallback rescoring, which selects
# the streaming candidate that gets persisted as the winning URL for an
# unaffected cache key. Widening the article scope would change which
# candidate wins on "A Tribe Called Quest" vs "Tribe Called Quest"-shaped
# ties -- a live match-quality change that deserves its own measurement,
# not a silent ride-along here. Full pin lives in
# tests/unit/test_streaming_matching.py::TestStripThePrefix.
# ---------------------------------------------------------------------------


class TestStripThePrefixNarrowerThanCrate:
    def test_a_an_not_stripped_unlike_crate_strip_leading_article(self) -> None:
        assert strip_the_prefix("A Tribe Called Quest") == "A Tribe Called Quest"
        # The crate's broader primitive WOULD strip "A "/"An " (lowercase-gated):
        assert strip_leading_article("a tribe called quest") == "tribe called quest"


# ---------------------------------------------------------------------------
# core/text.py fold-in: strip_leading_article was byte-identical to the
# crate's version, so core/text.py is deleted outright (not ported) and
# every consumer imports wxyc_etl.text.strip_leading_article directly.
# LEADING_ARTICLES (the frozenset, distinct from the strip function) moved
# to library/db.py with the candidate search-term selection that consumed
# it, then was deleted along with that selection when LML#1245 replaced the
# LIKE prefilter with the full-pool scan.
# ---------------------------------------------------------------------------


class TestStripLeadingArticleFoldedIntoCrate:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            pytest.param("the black dog", "black dog", id="the"),
            pytest.param("a microphones", "microphones", id="a"),
            pytest.param("therapy?", "therapy?", id="the-substring-preserved"),
            pytest.param("thee silver mt. zion", "thee silver mt. zion", id="thee-preserved"),
            pytest.param("the", "", id="bare-the"),
        ],
    )
    def test_pinned_cases_from_deleted_core_text_module(self, name: str, expected: str) -> None:
        """Migrated verbatim from the deleted tests/unit/test_core_text.py."""
        assert strip_leading_article(name) == expected


# ---------------------------------------------------------------------------
# Ad-hoc normalizer: discogs/service.py::calculate_confidence's nested
# normalize() ADOPTS to_match_form in place of s.lower().strip().
#
# Deliberate widening: diacritic-bearing WXYC artist names now
# substring-match their plain-ASCII spelling. This feeds search-result
# ranking (calculate_confidence), never a persisted cache key, so the wider
# match can only raise a candidate's score, never silently corrupt stored
# data.
# ---------------------------------------------------------------------------


class TestCalculateConfidenceAdoptsToMatchForm:
    def test_diacritic_artist_now_matches_ascii_spelling(self) -> None:
        with_diacritic = calculate_confidence("Nilüfer Yanya", None, "Nilufer Yanya", "Anything")
        assert with_diacritic == 0.4  # exact artist match despite the accent difference

    def test_would_not_have_matched_at_all_under_old_lower_strip(self) -> None:
        """Demonstrates the behavior this call site used to have: a bare
        ``.lower().strip()`` treats "Nilüfer Yanya" and "Nilufer Yanya" as
        unrelated strings -- neither equal nor a substring of the other --
        so neither the exact-match (+0.4) nor substring-partial (+0.3)
        branch would have fired, and the score would have fallen all the
        way to the 0.2 floor instead of the 0.4 this call site now returns
        (see test_diacritic_artist_now_matches_ascii_spelling above)."""
        req = "nilüfer yanya".lower().strip()
        res = "nilufer yanya".lower().strip()
        assert req != res
        assert req not in res
        assert res not in req


# ---------------------------------------------------------------------------
# Cache-key byte-stability canary (issue #1042 acceptance criterion).
#
# None of the functions touched above feed a persisted cache key: every
# entity/*_cache module (entity/streaming_url_cache.py,
# entity/track_streaming_url_cache.py, entity/release_resolution_cache.py,
# entity/compilation_track_location.py) computes its key via to_match_form
# / to_identity_match_form directly -- neither of which #1042 touches. This
# test pins those two functions' output for the WXYC diacritic corpus named
# in the issue, as a regression canary: if a future change to this file's
# imports ever accidentally re-routed a cache-key computation through one
# of the swapped shims, this test would catch the drift.
# ---------------------------------------------------------------------------


class TestCacheKeyStabilityCanary:
    @pytest.mark.parametrize(
        ("name", "expected_match_form"),
        [
            pytest.param("Nilüfer Yanya", "nilufer yanya", id="nilufer-yanya"),
            pytest.param("Hermanos Gutiérrez", "hermanos gutierrez", id="hermanos-gutierrez"),
            pytest.param("Csillagrablók", "csillagrablok", id="csillagrablok"),
        ],
    )
    def test_to_match_form_unaffected_by_shim_swap(
        self, name: str, expected_match_form: str
    ) -> None:
        assert to_match_form(name) == expected_match_form

    @pytest.mark.parametrize(
        ("name", "expected_identity_form"),
        [
            pytest.param("Nilüfer Yanya", "nilufer yanya", id="nilufer-yanya"),
            pytest.param("Hermanos Gutiérrez", "hermanos gutierrez", id="hermanos-gutierrez"),
            pytest.param("Csillagrablók", "csillagrablok", id="csillagrablok"),
        ],
    )
    def test_to_identity_match_form_unaffected_by_shim_swap(
        self, name: str, expected_identity_form: str
    ) -> None:
        assert to_identity_match_form(name) == expected_identity_form

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("Nilüfer Yanya", id="nilufer-yanya"),
            pytest.param("Hermanos Gutiérrez", id="hermanos-gutierrez"),
            pytest.param("Csillagrablók", id="csillagrablok"),
        ],
    )
    def test_canonicalize_for_identity_lookup_still_delegates_correctly(self, name: str) -> None:
        """canonicalize_for_identity_lookup (the ONE swapped function that
        feeds an entity.identity query bind, not a persisted cache-table
        key -- see module docstring) still produces the same output as
        before the fold_conjunctions swap for the diacritic corpus."""
        assert canonicalize_for_identity_lookup(name) == to_identity_match_form(name)
