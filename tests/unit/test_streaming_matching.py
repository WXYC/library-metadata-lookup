"""Unit tests for clients/streaming/matching.py."""

from unittest.mock import MagicMock, patch

import pytest

from clients.streaming.matching import (
    is_acceptable_match,
    normalize_album_title,
    normalize_artist_name,
    score_match,
    score_match_track,
    strip_discogs_suffix,
    strip_format_suffix,
    strip_the_prefix,
    strip_track_suffix,
)


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
