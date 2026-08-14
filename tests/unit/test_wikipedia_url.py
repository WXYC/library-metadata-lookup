"""Unit tests for lookup/wikipedia_url.py — the LML#513 slug-scored Wikipedia
URL extractor.

Table-driven per the repo's TDD protocol: scoring/tie-break/floor-fallback
cases first (red before ``lookup/wikipedia_url.py`` exists), the hard-reject
denylist (must fire BEFORE disambig stripping, per the module design — a
stripped ``Sessa (album)`` would score 100 against artist ``Sessa`` and slip
past scoring), the ``LML_WIKIPEDIA_SLUG_MATCH`` flag gate (default OFF,
``_TRUE_FLAG_VALUES`` spellings), and the shadow telemetry pair that fires
regardless of the flag.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lookup.wikipedia_url import (
    WIKIPEDIA_SLUG_MATCH_ENV_VAR,
    ExtractorComparison,
    PickedWikiUrl,
    _wikipedia_slug_match_enabled,
    compare_wikipedia_extractors,
    pick_artist_wikipedia_url,
)

# ---------------------------------------------------------------------------
# No wikipedia.org URLs at all
# ---------------------------------------------------------------------------


class TestNoWikipediaUrls:
    def test_none_urls_returns_none(self):
        assert pick_artist_wikipedia_url(None, "Stereolab") is None

    def test_empty_list_returns_none(self):
        assert pick_artist_wikipedia_url([], "Stereolab") is None

    def test_no_wikipedia_urls_returns_none(self):
        urls = ["https://www.discogs.com/artist/1", "https://example.com"]
        assert pick_artist_wikipedia_url(urls, "Stereolab") is None


# ---------------------------------------------------------------------------
# Flag OFF (default): legacy first-match behavior, below_floor always True
# ---------------------------------------------------------------------------


class TestFlagOffByteIdentical:
    """Default posture: the served URL is always the legacy first-match pick,
    and ``below_floor`` is always True — Phase B's gate keys on this so no
    bio text is ever fetched while the flag is off, independent of score."""

    def test_flag_off_serves_first_match_even_when_it_scores_high(self, monkeypatch):
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        urls = ["https://en.wikipedia.org/wiki/Stereolab", "https://www.discogs.com/artist/1"]
        picked = pick_artist_wikipedia_url(urls, "Stereolab")
        assert picked == PickedWikiUrl(
            url="https://en.wikipedia.org/wiki/Stereolab",
            lang="en",
            slug_score=pytest.approx(100.0),
            below_floor=True,
        )

    def test_flag_off_serves_first_match_even_when_a_later_url_scores_better(self, monkeypatch):
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        urls = [
            "https://en.wikipedia.org/wiki/Stereolab_(band_member)",
            "https://en.wikipedia.org/wiki/Stereolab",
        ]
        picked = pick_artist_wikipedia_url(urls, "Stereolab")
        assert picked is not None
        assert picked.url == "https://en.wikipedia.org/wiki/Stereolab_(band_member)"
        assert picked.below_floor is True


# ---------------------------------------------------------------------------
# Flag ON: slug-scored pick wins when it clears the floor
# ---------------------------------------------------------------------------


class TestFlagOnScoring:
    def test_exact_match_wins(self, monkeypatch):
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = ["https://en.wikipedia.org/wiki/Jessica_Pratt"]
        picked = pick_artist_wikipedia_url(urls, "Jessica Pratt")
        assert picked == PickedWikiUrl(
            url="https://en.wikipedia.org/wiki/Jessica_Pratt",
            lang="en",
            slug_score=pytest.approx(100.0),
            below_floor=False,
        )

    def test_url_decodes_and_underscores_become_spaces(self, monkeypatch):
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = ["https://en.wikipedia.org/wiki/Duke_Ellington_%26_John_Coltrane"]
        picked = pick_artist_wikipedia_url(urls, "Duke Ellington & John Coltrane")
        assert picked is not None
        assert picked.below_floor is False
        assert picked.slug_score >= 80.0

    def test_highest_scoring_candidate_wins_over_a_band_member_page(self, monkeypatch):
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = [
            "https://en.wikipedia.org/wiki/Tim_Gane",
            "https://en.wikipedia.org/wiki/Stereolab",
        ]
        picked = pick_artist_wikipedia_url(urls, "Stereolab")
        assert picked is not None
        assert picked.url == "https://en.wikipedia.org/wiki/Stereolab"
        assert picked.below_floor is False

    def test_disambig_suffix_is_stripped_before_scoring(self, monkeypatch):
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = ["https://en.wikipedia.org/wiki/Sessa_(2)"]
        picked = pick_artist_wikipedia_url(urls, "Sessa")
        assert picked is not None
        assert picked.url == "https://en.wikipedia.org/wiki/Sessa_(2)"
        assert picked.below_floor is False
        assert picked.slug_score == pytest.approx(100.0)

    def test_the_prefix_mismatch_still_scores_via_score_match(self, monkeypatch):
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = ["https://en.wikipedia.org/wiki/Mountain_Goats"]
        picked = pick_artist_wikipedia_url(urls, "The Mountain Goats")
        assert picked is not None
        assert picked.below_floor is False


# ---------------------------------------------------------------------------
# Hard-reject denylist — fires BEFORE disambig stripping
# ---------------------------------------------------------------------------


class TestHardRejectDenylist:
    @pytest.mark.parametrize(
        "slug",
        [
            "Sessa_(album)",
            "Some_Song_(song)",
            "A_Track_(single)",
            "An_EP_(EP)",
            "A_Score_(soundtrack)",
            "A_Movie_(film)",
            "A_Show_(TV_series)",
            "An_Artist_discography",
        ],
    )
    def test_qualifier_rejects_the_candidate_outright(self, monkeypatch, slug):
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = [f"https://en.wikipedia.org/wiki/{slug}"]
        picked = pick_artist_wikipedia_url(urls, "Sessa")
        # No candidate clears the floor (the only wikipedia.org URL was
        # hard-rejected before scoring), so the fallback posture applies:
        # legacy first-match link, below_floor=True.
        assert picked is not None
        assert picked.below_floor is True
        assert picked.url == urls[0]

    def test_eponymous_album_page_does_not_win_against_the_artist_page(self, monkeypatch):
        # Sessa_(album) would score 100 against "Sessa" if stripping ran
        # first — the hard-reject must fire before strip_discogs_disambig
        # so the eponymous album page can never masquerade as the artist.
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = [
            "https://en.wikipedia.org/wiki/Sessa_(album)",
            "https://en.wikipedia.org/wiki/Sessa_(musician)",
        ]
        picked = pick_artist_wikipedia_url(urls, "Sessa")
        assert picked is not None
        assert picked.url == "https://en.wikipedia.org/wiki/Sessa_(musician)"
        assert picked.below_floor is False


# ---------------------------------------------------------------------------
# Language tie-break — en wins ties, foreign wikis are never rejected outright
# ---------------------------------------------------------------------------


class TestLanguageTieBreak:
    def test_en_wins_a_score_tie(self, monkeypatch):
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = [
            "https://fr.wikipedia.org/wiki/Stereolab",
            "https://en.wikipedia.org/wiki/Stereolab",
        ]
        picked = pick_artist_wikipedia_url(urls, "Stereolab")
        assert picked is not None
        assert picked.lang == "en"
        assert picked.url == "https://en.wikipedia.org/wiki/Stereolab"

    def test_foreign_only_wiki_is_accepted_above_floor(self, monkeypatch):
        # Noura Mint Seymali-class artists have only non-en coverage.
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = ["https://fr.wikipedia.org/wiki/Noura_Mint_Seymali"]
        picked = pick_artist_wikipedia_url(urls, "Noura Mint Seymali")
        assert picked is not None
        assert picked.lang == "fr"
        assert picked.below_floor is False


# ---------------------------------------------------------------------------
# Below-floor fallback — never regress populated -> None
# ---------------------------------------------------------------------------


class TestBelowFloorFallback:
    def test_low_scoring_candidate_falls_back_to_first_match_not_none(self, monkeypatch):
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = ["https://en.wikipedia.org/wiki/Completely_Unrelated_Page"]
        picked = pick_artist_wikipedia_url(urls, "Stereolab")
        assert picked is not None
        assert picked.below_floor is True
        assert picked.url == urls[0]


# ---------------------------------------------------------------------------
# compare_wikipedia_extractors — flag- and floor-independent comparison
# (the shape scripts/wikipedia_url_validation.py's empirical gate needs)
# ---------------------------------------------------------------------------


class TestCompareWikipediaExtractors:
    def test_agreeing_picks(self):
        urls = ["https://en.wikipedia.org/wiki/Jessica_Pratt"]
        comparison = compare_wikipedia_extractors(urls, "Jessica Pratt")
        assert comparison == ExtractorComparison(
            heuristic_pick="https://en.wikipedia.org/wiki/Jessica_Pratt",
            slug_pick="https://en.wikipedia.org/wiki/Jessica_Pratt",
            slug_score=pytest.approx(100.0),
            slug_lang="en",
        )

    def test_diverging_picks_independent_of_the_flag(self, monkeypatch):
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        urls = [
            "https://en.wikipedia.org/wiki/Tim_Gane",
            "https://en.wikipedia.org/wiki/Stereolab",
        ]
        comparison = compare_wikipedia_extractors(urls, "Stereolab")
        assert comparison.heuristic_pick == "https://en.wikipedia.org/wiki/Tim_Gane"
        assert comparison.slug_pick == "https://en.wikipedia.org/wiki/Stereolab"

    def test_no_wikipedia_urls_returns_all_none(self):
        comparison = compare_wikipedia_extractors([], "Stereolab")
        assert comparison == ExtractorComparison(
            heuristic_pick=None, slug_pick=None, slug_score=0.0, slug_lang=None
        )

    def test_hard_rejected_candidate_never_becomes_the_slug_pick(self):
        urls = ["https://en.wikipedia.org/wiki/Sessa_(album)"]
        comparison = compare_wikipedia_extractors(urls, "Sessa")
        assert comparison.heuristic_pick == urls[0]
        assert comparison.slug_pick is None


# ---------------------------------------------------------------------------
# Flag resolver
# ---------------------------------------------------------------------------


class TestWikipediaSlugMatchEnabled:
    def test_default_off_when_unset(self, monkeypatch):
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        assert _wikipedia_slug_match_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "True", "yes", "YES", "on", " on "])
    def test_true_flag_values_enable(self, monkeypatch, value):
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, value)
        assert _wikipedia_slug_match_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "disabled", "garbage", ""])
    def test_everything_else_stays_off(self, monkeypatch, value):
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, value)
        assert _wikipedia_slug_match_enabled() is False


# ---------------------------------------------------------------------------
# Shadow telemetry — fires regardless of the flag
# ---------------------------------------------------------------------------


class TestShadowTelemetry:
    def test_fires_set_data_even_when_flag_is_off(self, monkeypatch):
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        urls = ["https://en.wikipedia.org/wiki/Stereolab"]
        mock_transaction = MagicMock()
        with patch("lookup.wikipedia_url.sentry_sdk") as mock_sentry:
            mock_sentry.get_current_scope.return_value.transaction = mock_transaction
            pick_artist_wikipedia_url(urls, "Stereolab")
        mock_transaction.set_data.assert_called_once()
        key, payload = mock_transaction.set_data.call_args.args
        assert key == "wikipedia_slug_pick"
        assert payload["heuristic_pick"] == "https://en.wikipedia.org/wiki/Stereolab"
        assert payload["slug_pick"] == "https://en.wikipedia.org/wiki/Stereolab"
        assert payload["agreement"] is True

    def test_records_disagreement_when_picks_diverge(self, monkeypatch):
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        urls = [
            "https://en.wikipedia.org/wiki/Tim_Gane",
            "https://en.wikipedia.org/wiki/Stereolab",
        ]
        mock_transaction = MagicMock()
        with patch("lookup.wikipedia_url.sentry_sdk") as mock_sentry:
            mock_sentry.get_current_scope.return_value.transaction = mock_transaction
            pick_artist_wikipedia_url(urls, "Stereolab")
        _key, payload = mock_transaction.set_data.call_args.args
        assert payload["heuristic_pick"] == "https://en.wikipedia.org/wiki/Tim_Gane"
        assert payload["slug_pick"] == "https://en.wikipedia.org/wiki/Stereolab"
        assert payload["agreement"] is False

    def test_no_active_transaction_is_a_no_op(self, monkeypatch):
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        urls = ["https://en.wikipedia.org/wiki/Stereolab"]
        with patch("lookup.wikipedia_url.sentry_sdk") as mock_sentry:
            mock_sentry.get_current_scope.return_value.transaction = None
            # Must not raise.
            pick_artist_wikipedia_url(urls, "Stereolab")

    def test_sentry_error_is_swallowed(self, monkeypatch):
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        urls = ["https://en.wikipedia.org/wiki/Stereolab"]
        with patch("lookup.wikipedia_url.sentry_sdk") as mock_sentry:
            mock_sentry.get_current_scope.side_effect = RuntimeError("boom")
            # Must not raise — telemetry is best-effort.
            pick_artist_wikipedia_url(urls, "Stereolab")
