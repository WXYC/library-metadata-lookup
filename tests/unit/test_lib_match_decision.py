"""Tests for the shared drain match decision (LML#1353).

The production defect these pin: ``scripts/search_unmatched_compilations.py``
accepted a candidate the guarded 80/80 matcher had *rejected* on a title-only
score, stored that title score in the ``confidence`` column, and wrote neither
``matched_artist`` nor ``matched_title`` — so album 52656 ("Married to the Mob",
a 1988 soundtrack) came to carry Speaker Knockerz's "Married to the Money" at
"89.47% confidence" with empty provenance.

The *write* half is pinned where its owner lives: ``tests/unit/test_results_db.py``
for ``ResultsDB.update_result``'s conditional write, and
``tests/unit/test_search_unmatched_compilations.py`` for the lane end to end.
"""

from __future__ import annotations

import pytest

from clients.streaming.matching import SCORE_MATCH_ACCEPTANCE_FLOOR
from scripts._lib.match_decision import (
    AXES_ARTIST_AND_TITLE,
    AXES_ARTIST_ONLY,
    AXES_TITLE_ONLY,
    STATUS_FOUND,
    STATUS_FOUND_TITLE_ONLY,
    ServiceMatch,
    best_title_only_candidate,
    decide_service_match,
)

# Spotify-shaped rows, as ``search_unmatched_compilations`` extracts them.
_SPOTIFY_ARTIST = lambda r: r.get("artists", [{}])[0].get("name", "")  # noqa: E731
_SPOTIFY_TITLE = lambda r: r.get("name", "")  # noqa: E731
_SPOTIFY_URL = lambda r: r.get("external_urls", {}).get("spotify", "")  # noqa: E731
_SPOTIFY_ID = lambda r: r.get("id", "")  # noqa: E731


def _spotify_row(artist: str, title: str, album_id: str = "abc123") -> dict:
    return {
        "id": album_id,
        "name": title,
        "artists": [{"name": artist}],
        "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
    }


def _spotify_kwargs() -> dict:
    return {
        "artist_fn": _SPOTIFY_ARTIST,
        "title_fn": _SPOTIFY_TITLE,
        "url_fn": _SPOTIFY_URL,
        "id_fn": _SPOTIFY_ID,
    }


class TestBestTitleOnlyCandidate:
    """The relaxation is scoped to the case where the artist axis says nothing."""

    def test_accepts_when_both_sides_are_va_credits(self):
        """A V/A shelf credit against a V/A-credited release: LML#1147's case."""
        rows = [_spotify_row("Various Artists", "Nuggets: Original Artyfacts")]
        winner = best_title_only_candidate(
            rows,
            query_artist="Various",
            query_title="Nuggets: Original Artyfacts",
            artist_fn=_SPOTIFY_ARTIST,
            title_fn=_SPOTIFY_TITLE,
            key_fn=_SPOTIFY_URL,
        )
        assert winner is not None
        assert winner[0] is rows[0]
        assert winner[1] == pytest.approx(100.0)

    def test_rejects_a_named_artist_candidate(self):
        """Album 52656 verbatim: the artist axis is informative and says NO."""
        rows = [_spotify_row("Speaker Knockerz", "Married to the Money")]
        assert (
            best_title_only_candidate(
                rows,
                query_artist="Soundtrack",
                query_title="Married to the Mob",
                artist_fn=_SPOTIFY_ARTIST,
                title_fn=_SPOTIFY_TITLE,
                key_fn=_SPOTIFY_URL,
            )
            is None
        )

    def test_picks_best_title_not_first_in_service_order(self):
        """Spotify orders by its own relevance, which is not the title-score order."""
        rows = [
            _spotify_row("Various Artists", "Aluminum Tune", "first"),
            _spotify_row("Various", "Aluminum Tunes", "second"),
        ]
        winner = best_title_only_candidate(
            rows,
            query_artist="Various Artists - Rock",
            query_title="Aluminum Tunes",
            artist_fn=_SPOTIFY_ARTIST,
            title_fn=_SPOTIFY_TITLE,
            key_fn=_SPOTIFY_URL,
        )
        assert winner is not None
        assert winner[0]["id"] == "second"

    def test_ties_resolve_by_ascending_key(self):
        """Deterministic across repeated identical queries (LML#1097's rule)."""
        rows = [
            _spotify_row("Various Artists", "DOGA", "zzz"),
            _spotify_row("Various Artists", "DOGA", "aaa"),
        ]
        winner = best_title_only_candidate(
            rows,
            query_artist="Various",
            query_title="DOGA",
            artist_fn=_SPOTIFY_ARTIST,
            title_fn=_SPOTIFY_TITLE,
            key_fn=_SPOTIFY_URL,
        )
        assert winner is not None
        assert winner[0]["id"] == "aaa"

    def test_rejects_below_the_floor(self):
        rows = [_spotify_row("Various Artists", "Completely Different Record")]
        assert (
            best_title_only_candidate(
                rows,
                query_artist="Various",
                query_title="Nuggets",
                artist_fn=_SPOTIFY_ARTIST,
                title_fn=_SPOTIFY_TITLE,
                key_fn=_SPOTIFY_URL,
            )
            is None
        )

    def test_honors_a_lane_specific_floor(self):
        """The Discogs cache lane keeps its historical 70 title floor."""
        rows = [{"id": 7, "title": "Songs of the Humpback", "artist_name": "Various"}]
        kwargs = {
            "query_artist": "Various",
            "query_title": "Songs of the Humpback Whale",
            "artist_fn": lambda r: r["artist_name"],
            "title_fn": lambda r: r["title"],
            "key_fn": lambda r: str(r["id"]),
        }
        assert best_title_only_candidate(rows, floor=95.0, **kwargs) is None
        assert best_title_only_candidate(rows, floor=70.0, **kwargs) is not None

    def test_skips_a_malformed_row_without_losing_the_response(self):
        """Mirrors find_best_match's LML#640 guard: one sparse row is not fatal."""
        rows = [{"name": "Aluminum Tunes"}, _spotify_row("Various Artists", "Aluminum Tunes")]
        winner = best_title_only_candidate(
            rows,
            query_artist="Various",
            query_title="Aluminum Tunes",
            artist_fn=_SPOTIFY_ARTIST,
            title_fn=_SPOTIFY_TITLE,
            key_fn=_SPOTIFY_URL,
        )
        assert winner is not None
        assert winner[0]["id"] == "abc123"

    def test_every_row_failing_extraction_re_raises(self):
        """A systemic extractor break must not read as "nothing matched".

        ``search_discogs_by_title`` calls this helper with no guarded pass in
        front of it, so swallowing a wholly-failed response would report a
        Discogs-cache column rename as a clean zero-match run and push the entire
        compilation population at the rate-limited streaming APIs. Same rule, and
        the same LML#376 reason, as ``find_best_match``'s own re-raise.
        """
        rows = [{"wrong": "shape"}, {"also": "wrong"}]
        with pytest.raises(KeyError):
            best_title_only_candidate(
                rows,
                query_artist="Various",
                query_title="Nuggets",
                artist_fn=lambda r: r["artist_name"],
                title_fn=lambda r: r["title"],
                key_fn=lambda r: str(r["id"]),
            )

    def test_empty_results(self):
        assert (
            best_title_only_candidate(
                [],
                query_artist="Various",
                query_title="Nuggets",
                artist_fn=_SPOTIFY_ARTIST,
                title_fn=_SPOTIFY_TITLE,
                key_fn=_SPOTIFY_URL,
            )
            is None
        )


class TestServiceMatchStatus:
    def test_an_axes_value_with_no_status_is_refused(self):
        """Derivation, not an ``else``: the pair must not be able to drift.

        ``AXES_ARTIST_ONLY`` is in this module's vocabulary (``discogs_rematch``
        reports it), and an ``else`` branch would persist it as
        ``found_title_only`` — a status asserting a title comparison that never
        happened.
        """
        match = ServiceMatch(
            url="https://open.spotify.com/album/x",
            confidence=90.0,
            matched_artist="Stereolab",
            matched_title="Aluminum Tunes",
            axes=AXES_ARTIST_ONLY,
        )
        with pytest.raises(KeyError):
            _ = match.status


class TestDecideServiceMatch:
    def test_guarded_match_reports_both_axes(self):
        rows = [_spotify_row("Jessica Pratt", "On Your Own Love Again")]
        decision = decide_service_match(
            rows,
            query_artist="Jessica Pratt",
            query_title="On Your Own Love Again",
            **_spotify_kwargs(),
        )
        assert decision is not None
        assert decision.axes == AXES_ARTIST_AND_TITLE
        assert decision.status == STATUS_FOUND
        assert decision.confidence == pytest.approx(100.0)
        assert decision.matched_artist == "Jessica Pratt"
        assert decision.matched_title == "On Your Own Love Again"
        assert decision.service_item_id == "abc123"

    def test_title_only_match_is_never_status_found(self):
        rows = [_spotify_row("Various Artists", "Nuggets")]
        decision = decide_service_match(
            rows,
            query_artist="Various",
            query_title="Nuggets",
            **_spotify_kwargs(),
        )
        assert decision is not None
        assert decision.axes == AXES_TITLE_ONLY
        assert decision.status == STATUS_FOUND_TITLE_ONLY
        assert decision.matched_artist == "Various Artists"
        assert decision.matched_title == "Nuggets"

    def test_rejected_candidate_yields_no_decision(self):
        """The 52656 shape end to end: the drain records nothing at all."""
        rows = [_spotify_row("Speaker Knockerz", "Married to the Money")]
        assert (
            decide_service_match(
                rows,
                query_artist="Soundtrack",
                query_title="Married to the Mob",
                **_spotify_kwargs(),
            )
            is None
        )

    def test_confidence_is_the_score_of_the_axes_it_names(self):
        """One meaning per row: the axes field says what confidence measured."""
        rows = [_spotify_row("Various Artists", "Aluminum Tunez")]
        decision = decide_service_match(
            rows,
            query_artist="Various",
            query_title="Aluminum Tunes",
            **_spotify_kwargs(),
        )
        assert decision is not None
        assert decision.axes == AXES_TITLE_ONLY
        assert SCORE_MATCH_ACCEPTANCE_FLOOR <= decision.confidence < 100.0

    def test_no_results(self):
        assert (
            decide_service_match(
                [],
                query_artist="Various",
                query_title="Nuggets",
                **_spotify_kwargs(),
            )
            is None
        )

    def test_a_url_less_top_candidate_does_not_sink_the_lane(self):
        """Filtered before scoring, not after the winner is picked.

        ``find_best_match`` returns one winner. If a URL-less candidate can win,
        a region-restricted Spotify album with no ``external_urls.spotify`` takes
        the runner-up that also cleared 80/80 — and the relaxation, which only
        runs when the guarded pass found nothing — down with it.
        """
        best_but_url_less = {
            "id": "restricted",
            "name": "On Your Own Love Again",
            "artists": [{"name": "Jessica Pratt"}],
        }
        runner_up = _spotify_row("Jessica Pratt", "On Your Own Love", "playable")
        decision = decide_service_match(
            [best_but_url_less, runner_up],
            query_artist="Jessica Pratt",
            query_title="On Your Own Love Again",
            **_spotify_kwargs(),
        )
        assert decision is not None
        assert decision.url == "https://open.spotify.com/album/playable"
        assert decision.matched_title == "On Your Own Love"

    def test_a_malformed_id_does_not_abort_the_relaxed_path(self):
        """``find_best_match`` extracts the id inside its own guard; so must this.

        The reachable shape needs a second row that extracts cleanly, because a
        response where *every* row is id-sparse is a systemic break the guarded
        pass re-raises on before the relaxation is reached. Here the guarded pass
        skips the id-sparse row, rejects the other on the artist axis and returns
        None — and the relaxation then picks the very row it skipped. Without the
        guard that raises into the caller's blanket ``except Exception``, which
        discards the whole lane's response over one missing field.
        """
        id_sparse = {
            "name": "Nuggets",
            "artists": [{"name": "Various Artists"}],
            "external_urls": {"spotify": "https://open.spotify.com/album/nuggets"},
        }
        well_formed = _spotify_row("Various Artists", "Something Else Entirely", "other")
        decision = decide_service_match(
            [well_formed, id_sparse],
            query_artist="Various",
            query_title="Nuggets",
            artist_fn=_SPOTIFY_ARTIST,
            title_fn=_SPOTIFY_TITLE,
            url_fn=_SPOTIFY_URL,
            id_fn=lambda r: r["id"],
        )
        assert decision is not None
        assert decision.axes == AXES_TITLE_ONLY
        assert decision.matched_title == "Nuggets"
        assert decision.service_item_id is None

    @pytest.mark.parametrize(
        ("candidate_artist", "query_artist"),
        [("Various Artists", "Various"), ("Jessica Pratt", "Jessica Pratt")],
        ids=["title-only", "guarded"],
    )
    def test_a_candidate_with_no_url_is_not_a_decision(self, candidate_artist, query_artist):
        """Neither path may record a URL-less candidate, on either axis.

        ``_SPOTIFY_URL``/``_DEEZER_URL`` default to ``""`` on a missing key, and
        ``''`` is not NULL: it clears ``export_streaming_links.py``'s
        ``spotify_url IS NOT NULL`` export gate and then violates the PG mirror's
        ``url <> ''`` CHECK when the artifact is seeded.
        """
        rows = [{"id": "abc123", "name": "Nuggets", "artists": [{"name": candidate_artist}]}]
        assert (
            decide_service_match(
                rows,
                query_artist=query_artist,
                query_title="Nuggets",
                **_spotify_kwargs(),
            )
            is None
        )
