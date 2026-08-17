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

    def test_disambiguated_artist_name_is_stripped_before_scoring(self, monkeypatch):
        # LML#1192 review (A2): a Discogs artist name like "Sessa (2)" must
        # be stripped symmetrically with the candidate slug, mirroring
        # lookup/artist_resolution.py's _artist_pair_verified (lines
        # 119/124) -- without the strip, "Sessa" vs "Sessa (2)" scores
        # 71.43 and fails the 80 floor, making every disambiguated Discogs
        # artist name unable to ever match its own correct Wikipedia page.
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = ["https://en.wikipedia.org/wiki/Sessa"]
        picked = pick_artist_wikipedia_url(urls, "Sessa (2)")
        assert picked is not None
        assert picked.url == "https://en.wikipedia.org/wiki/Sessa"
        assert picked.below_floor is False
        assert picked.slug_score == pytest.approx(100.0)

    def test_disambiguated_artist_name_matches_a_country_suffixed_page(self, monkeypatch):
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = ["https://en.wikipedia.org/wiki/Stereolab"]
        picked = pick_artist_wikipedia_url(urls, "Stereolab (UK)")
        assert picked is not None
        assert picked.below_floor is False
        assert picked.slug_score == pytest.approx(100.0)


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

    @pytest.mark.parametrize(
        "slug,artist",
        [
            # Compound qualifiers: a year/name/adjective PRECEDES the
            # denylist word inside the parenthetical, so the whole
            # parenthetical never equals a bare denylist entry -- these
            # must still hard-reject on the trailing qualifier TOKEN.
            ("Sessa_(2015_album)", "Sessa"),
            ("Grace_(Jeff_Buckley_album)", "Grace"),
            ("Edits_(mixtape)", "Edits"),
            ("DOGA_(compilation_album)", "DOGA"),
            ("Aluminum_Tunes_(Stereolab_album)", "Aluminum Tunes"),
        ],
    )
    def test_compound_qualifier_still_hard_rejects(self, monkeypatch, slug, artist):
        # Confirmed pre-fix leak (LML#1192 review): the slug's own prefix
        # equals the query artist, and without the fix the compound
        # parenthetical sails past the exact-match-only denylist check,
        # strips down to the bare prefix via strip_discogs_disambig, and
        # scores 100 -- an album/mixtape/compilation page masquerading as
        # the artist page.
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = [f"https://en.wikipedia.org/wiki/{slug}"]
        picked = pick_artist_wikipedia_url(urls, artist)
        assert picked is not None
        assert picked.below_floor is True
        assert picked.url == urls[0]  # falls back to the (only) heuristic pick

    def test_compound_qualifier_leak_does_not_win_against_the_real_artist_page(self, monkeypatch):
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = [
            "https://en.wikipedia.org/wiki/Sessa_(2015_album)",
            "https://en.wikipedia.org/wiki/Sessa_(musician)",
        ]
        picked = pick_artist_wikipedia_url(urls, "Sessa")
        assert picked is not None
        assert picked.url == "https://en.wikipedia.org/wiki/Sessa_(musician)"
        assert picked.below_floor is False

    def test_synchronous_path_still_hard_rejects_a_bare_denylisted_slug(self, monkeypatch):
        # LML#1192 review round 6, P2-2: the synchronous request path
        # (pick_artist_wikipedia_url) has no live fetch to validate a
        # denylisted guess against and never will (the plan's Non-goals) --
        # unlike lookup.wikipedia_pick_validation.resolve_and_validate_pick
        # (a fetch-capable caller, which now admits a denylisted candidate
        # like this one -- see tests/unit/test_wikipedia_pick_validation.py),
        # this path must keep the denylist fully decisive. The Polish/
        # Croatian band Film's real article lives at exactly this URL, but
        # this path has no way to know that without a fetch it can't afford.
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = ["https://en.wikipedia.org/wiki/Film"]
        picked = pick_artist_wikipedia_url(urls, "Film")
        assert picked is not None
        assert picked.below_floor is True
        assert picked.url == urls[0]  # legacy heuristic fallback, never the slug pick


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

    def test_www_subdomain_normalizes_to_en_not_a_literal_www_lang(self, monkeypatch):
        # LML#1192 review round 4, P0-7: "www.wikipedia.org/wiki/..." is a
        # realistic input (artist_url is unvalidated free text, and a
        # browser 301s a bare wikipedia.org root to the www host) -- but
        # "www" is a hostname prefix, not a language code. Verified live:
        # the REST API 500s on https://www.wikipedia.org/api/rest_v1/...
        # (https://en.wikipedia.org/... 200s for the same title), so an
        # un-normalized "www" lang persists into the NOT NULL lang column
        # and generates permanent fetch_error residue on every retry.
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = ["https://www.wikipedia.org/wiki/Stereolab"]
        picked = pick_artist_wikipedia_url(urls, "Stereolab")
        assert picked is not None
        assert picked.lang == "en"
        assert picked.below_floor is False

    def test_mixed_case_wikipedia_domain_still_falls_back_to_the_heuristic_pick(self, monkeypatch):
        # LML#1192 review round 2, C3: scripts/warm_wikipedia_bios.py's seed
        # SQL selects candidates via a case-INSENSITIVE `au.url ILIKE
        # '%wikipedia.org%'`, and lookup.wikipedia_candidates.score_candidates's
        # regex matches case-insensitively too (re.IGNORECASE) -- but the legacy heuristic
        # fallback used a case-SENSITIVE `"wikipedia.org" in url` substring
        # check. A mixed-case domain ("en.Wikipedia.org") that scores below
        # the floor would make compare_wikipedia_extractors return a real
        # slug_pick/slug_score but a None heuristic_pick, so
        # pick_artist_wikipedia_url's below-floor fallback branch (which
        # serves heuristic_pick) silently returned a URL-less pick even
        # though the artist DOES have a wikipedia.org URL -- and the SAME
        # gap crashed the offline drain's below-floor write path outright
        # (a since-removed `assert url is not None`; see
        # test_warm_wikipedia_bios.py).
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        urls = ["https://en.Wikipedia.org/wiki/Totally_Unrelated_Page"]
        picked = pick_artist_wikipedia_url(urls, "Some Artist Whose Name Does Not Match At All")
        assert picked is not None
        assert picked.below_floor is True
        assert picked.url == urls[0]

    @pytest.mark.parametrize(
        "artist_name,bare_slug,qualified_slug",
        [
            ("Sun Ra", "Sun_Ra", "Sun_Ra_(disambiguation)"),
            ("Stereolab", "Stereolab", "Stereolab_(band)"),
            ("Cat Power", "Cat_Power", "Cat_Power_(musician)"),
            ("Sade", "Sade", "Sade_(band)"),
            ("Low", "Low", "Low_(band)"),
            ("Sessa", "Sessa", "Sessa_(2)"),
        ],
    )
    def test_genuine_tie_picks_the_shorter_unqualified_page_regardless_of_input_order(
        self, monkeypatch, artist_name, bare_slug, qualified_slug
    ):
        # LML#1192 review round 3: round 2's C1 fix made the tiebreak TOTAL
        # (good -- that property must survive) but broke WHICH url wins --
        # it sorted on the url string descending, and a bare slug is always
        # a strict string prefix of its qualified sibling, so the qualified
        # (wrong) page won 5/5 on real examples, deterministically, every
        # time. Once LML_WIKIPEDIA_SLUG_MATCH is on this is a live /lookup
        # regression: a disambiguation page fails `type != "standard"` and a
        # `(band)`/`(musician)` page 404s, so both negative-cache for 7 days
        # (BS#1747 can then freeze that permanently). The correct tiebreak
        # prefers the SHORTER (unqualified) URL -- still a total order
        # (independent of input list order), but now pointed at the
        # canonical page instead of away from it.
        monkeypatch.setenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, "true")
        bare_url = f"https://en.wikipedia.org/wiki/{bare_slug}"
        qualified_url = f"https://en.wikipedia.org/wiki/{qualified_slug}"

        picked_bare_first = pick_artist_wikipedia_url([bare_url, qualified_url], artist_name)
        picked_qualified_first = pick_artist_wikipedia_url([qualified_url, bare_url], artist_name)

        assert picked_bare_first is not None
        assert picked_qualified_first is not None
        assert picked_bare_first.url == bare_url, (
            f"expected the bare page to win, got {picked_bare_first.url!r}"
        )
        assert picked_qualified_first.url == bare_url, (
            "a genuine tie must resolve identically regardless of input order -- "
            f"got {picked_qualified_first.url!r}"
        )


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


class TestExtractorComparisonClearsFloorAndAgreement:
    """LML#1192 review round 3, finding 9: ``clears_floor``/``agreement`` were
    hand-written identically in three modules; now owned here."""

    def test_clears_floor_true_above_the_acceptance_floor(self):
        comparison = compare_wikipedia_extractors(
            ["https://en.wikipedia.org/wiki/Stereolab"], "Stereolab"
        )
        assert comparison.clears_floor is True

    def test_clears_floor_false_when_no_slug_pick(self):
        comparison = compare_wikipedia_extractors([], "Stereolab")
        assert comparison.clears_floor is False

    def test_clears_floor_false_below_the_acceptance_floor(self):
        comparison = compare_wikipedia_extractors(
            ["https://en.wikipedia.org/wiki/Completely_Unrelated_Page"], "Stereolab"
        )
        assert comparison.clears_floor is False

    def test_agreement_true_when_picks_match(self):
        comparison = compare_wikipedia_extractors(
            ["https://en.wikipedia.org/wiki/Jessica_Pratt"], "Jessica Pratt"
        )
        assert comparison.agreement is True

    def test_agreement_false_when_picks_diverge(self):
        urls = [
            "https://en.wikipedia.org/wiki/Tim_Gane",
            "https://en.wikipedia.org/wiki/Stereolab",
        ]
        comparison = compare_wikipedia_extractors(urls, "Stereolab")
        assert comparison.agreement is False

    def test_agreement_false_when_no_slug_pick_even_if_heuristic_exists(self):
        comparison = compare_wikipedia_extractors(
            ["https://en.wikipedia.org/wiki/Sessa_(album)"], "Sessa"
        )
        assert comparison.slug_pick is None
        assert comparison.heuristic_pick is not None
        assert comparison.agreement is False


class TestExtractorComparisonResolve:
    """LML#1192 review round 3, finding 9: the decision body for
    ``pick_artist_wikipedia_url`` (``slug_enabled=_wikipedia_slug_match_enabled()``),
    the live read path's unvalidated best-guess pick. LML#1192 review round
    4, P0-2: the offline drain and background miss-warm no longer call this
    -- they go through ``lookup.wikipedia_pick_validation.resolve_and_validate_pick``
    instead, which validates each ranked candidate against a live fetch."""

    def test_no_url_at_all_returns_none(self):
        comparison = compare_wikipedia_extractors([], "Stereolab")
        assert comparison.resolve(slug_enabled=True) is None
        assert comparison.resolve(slug_enabled=False) is None

    def test_slug_enabled_and_clears_floor_serves_the_slug_pick(self):
        comparison = compare_wikipedia_extractors(
            ["https://en.wikipedia.org/wiki/Stereolab"], "Stereolab"
        )
        picked = comparison.resolve(slug_enabled=True)
        assert picked == PickedWikiUrl(
            url="https://en.wikipedia.org/wiki/Stereolab",
            lang="en",
            slug_score=pytest.approx(100.0),
            below_floor=False,
        )

    def test_slug_disabled_falls_back_to_heuristic_even_above_floor(self):
        comparison = compare_wikipedia_extractors(
            ["https://en.wikipedia.org/wiki/Stereolab"], "Stereolab"
        )
        picked = comparison.resolve(slug_enabled=False)
        assert picked is not None
        assert picked.below_floor is True
        assert picked.url == "https://en.wikipedia.org/wiki/Stereolab"

    def test_below_floor_fallback_derives_lang_from_the_served_heuristic_url_not_slug_lang(self):
        # LML#1192 review round 3, finding 8: the drain's own hand-rolled
        # copy of this decision returned slug_lang (the winning SLUG
        # candidate's language) on the below-floor branch, which can name a
        # completely different page's language than the URL actually being
        # served. resolve() must always derive lang from heuristic_pick.
        urls = [
            "https://de.wikipedia.org/wiki/Voellig_Unrelated_Seite",  # heuristic (first)
            "https://fr.wikipedia.org/wiki/Le_Meilleur_Match_Possible",  # scores highest
        ]
        comparison = compare_wikipedia_extractors(urls, "Le Meilleur Match Possible")
        assert comparison.slug_lang == "fr"
        assert comparison.heuristic_pick != comparison.slug_pick
        picked = comparison.resolve(slug_enabled=False)
        assert picked is not None
        assert picked.url == urls[0]
        assert picked.lang == "de"


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

    def test_no_wikipedia_url_at_all_does_not_fire_telemetry(self, monkeypatch):
        # LML#1192 review (A4): a non-event (nothing to compare -- no
        # wikipedia.org URL present) must not be recorded as a
        # disagreement; it would flood the divergence counter with
        # meaningless entries.
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        mock_transaction = MagicMock()
        with patch("lookup.wikipedia_url.sentry_sdk") as mock_sentry:
            mock_sentry.get_current_scope.return_value.transaction = mock_transaction
            result = pick_artist_wikipedia_url(["https://example.com/not-wikipedia"], "Stereolab")
        assert result is None
        mock_transaction.set_data.assert_not_called()
        mock_sentry.add_breadcrumb.assert_not_called()

    def test_payload_carries_slug_score_and_clears_floor(self, monkeypatch):
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        urls = ["https://en.wikipedia.org/wiki/Stereolab"]
        mock_transaction = MagicMock()
        with patch("lookup.wikipedia_url.sentry_sdk") as mock_sentry:
            mock_sentry.get_current_scope.return_value.transaction = mock_transaction
            pick_artist_wikipedia_url(urls, "Stereolab")
        _key, payload = mock_transaction.set_data.call_args.args
        assert payload["slug_score"] == pytest.approx(100.0)
        assert payload["clears_floor"] is True

    def test_below_floor_payload_reports_clears_floor_false(self, monkeypatch):
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        urls = ["https://en.wikipedia.org/wiki/Completely_Unrelated_Page"]
        mock_transaction = MagicMock()
        with patch("lookup.wikipedia_url.sentry_sdk") as mock_sentry:
            mock_sentry.get_current_scope.return_value.transaction = mock_transaction
            pick_artist_wikipedia_url(urls, "Stereolab")
        _key, payload = mock_transaction.set_data.call_args.args
        assert payload["clears_floor"] is False

    def test_fires_a_breadcrumb_so_bulk_items_do_not_clobber_each_other(self, monkeypatch):
        # LML#1192 review (A4): set_data with a fixed key is last-writer-wins
        # across multiple items sharing one /lookup/bulk transaction --
        # add_breadcrumb accumulates instead, mirroring
        # lookup.artist_resolution._log_artist_identity_split_gate.
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        urls = ["https://en.wikipedia.org/wiki/Stereolab"]
        with patch("lookup.wikipedia_url.sentry_sdk") as mock_sentry:
            mock_sentry.get_current_scope.return_value.transaction = MagicMock()
            pick_artist_wikipedia_url(urls, "Stereolab")
        mock_sentry.add_breadcrumb.assert_called_once()
        assert mock_sentry.add_breadcrumb.call_args.kwargs["category"] == "wikipedia_slug_pick"

    def test_breadcrumb_error_is_independently_swallowed(self, monkeypatch):
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        urls = ["https://en.wikipedia.org/wiki/Stereolab"]
        with patch("lookup.wikipedia_url.sentry_sdk") as mock_sentry:
            mock_sentry.get_current_scope.return_value.transaction = MagicMock()
            mock_sentry.add_breadcrumb.side_effect = RuntimeError("boom")
            # Must not raise, and must not prevent set_data from still firing.
            pick_artist_wikipedia_url(urls, "Stereolab")
        mock_sentry.get_current_scope.return_value.transaction.set_data.assert_called_once()


# ---------------------------------------------------------------------------
# Fallback URL/lang consistency (LML#1192 review, A5)
# ---------------------------------------------------------------------------


class TestFallbackUrlLangConsistency:
    def test_fallback_lang_matches_the_served_url_not_a_different_candidate(self, monkeypatch):
        # The heuristic pick (served in the below-floor fallback) is the
        # FIRST wikipedia.org URL, in German; the highest-scoring (but
        # below-floor) slug candidate is a DIFFERENT, English URL. The
        # served pair must describe the SAME page -- lang must come from
        # the served url, never from an unrelated candidate.
        monkeypatch.delenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR, raising=False)
        urls = [
            "https://de.wikipedia.org/wiki/Erste_Seite",
            "https://en.wikipedia.org/wiki/Stereolab",
        ]
        picked = pick_artist_wikipedia_url(urls, "A Totally Different Band")
        assert picked is not None
        assert picked.url == "https://de.wikipedia.org/wiki/Erste_Seite"
        assert picked.lang == "de"
