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

from clients.streaming.matching import _EXTRACTION_ERRORS, SCORE_MATCH_ACCEPTANCE_FLOOR
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

    @pytest.mark.parametrize(
        ("query_title", "candidate_title"),
        [("", ""), ("   ", ""), ("Nuggets", ""), ("", "Nuggets")],
        ids=["both-empty", "whitespace-query", "empty-candidate", "empty-query"],
    )
    def test_an_empty_title_is_never_a_match(self, query_title, candidate_title):
        """``score_match("", "")`` is 100 by rapidfuzz convention — both siblings
        guard this and this one must too.

        Otherwise a blank title on either side is *accepted* at confidence 100 and
        written as ``found_title_only`` with an empty ``matched_title``: a row
        recording maximum certainty about nothing, which is the shape LML#1353
        exists to remove rather than to mint.
        """
        rows = [_spotify_row("Various Artists", candidate_title)]
        assert (
            best_title_only_candidate(
                rows,
                query_artist="Various",
                query_title=query_title,
                artist_fn=_SPOTIFY_ARTIST,
                title_fn=_SPOTIFY_TITLE,
                key_fn=_SPOTIFY_URL,
            )
            is None
        )


_VA_CREDIT = "Various"
_VA_CANDIDATE = "Various Artists"
_VA_TITLE = "Nuggets"
_NAMED_CREDIT = "Jessica Pratt"
_NAMED_TITLE = "On Your Own Love Again"


def _clean(credit: str, title: str, album_id: str) -> dict:
    return _spotify_row(credit, title, album_id)


def _url_less(credit: str, title: str, album_id: str) -> dict:
    """Well formed on every axis, but the URL extractor yields ``""``."""
    return {"id": album_id, "name": title, "artists": [{"name": credit}]}


def _malformed(album_id: str) -> dict:
    """``artists`` is None, so ``artist_fn`` raises — a sparse row, with a URL."""
    return {
        "id": album_id,
        "name": "whatever",
        "artists": None,
        "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
    }


class TestDecideServiceMatchSeam:
    """The pre-pass verdict x the V/A partition, as one table (LML#1353).

    This is the seam three successive fixes each broke a different corner of: the
    pre-pass owns "is this response usable" on behalf of *both* matchers, and the
    query credit decides *which* matcher runs. The cross-product of candidate-list
    shape and query-credit kind is small enough to enumerate, and every regression
    in this area landed in one of these cells:

    * a URL-less top candidate sinking a lane that had a playable runner-up,
    * a URL filter shrinking the denominator until one sparse row read as "every
      row failed" and aborted the lane,
    * a wholly-URL-less response passing as a clean no-match,
    * a V/A query credit reaching the guarded matcher and scoring a tautology.
    """

    @pytest.mark.parametrize(
        ("credit", "title", "candidate_credit"),
        [
            pytest.param(_VA_CREDIT, _VA_TITLE, _VA_CANDIDATE, id="va-credit"),
            pytest.param(_NAMED_CREDIT, _NAMED_TITLE, _NAMED_CREDIT, id="named-credit"),
        ],
    )
    @pytest.mark.parametrize(
        ("shape", "expect"),
        [
            pytest.param("empty", "none", id="no-candidates"),
            pytest.param("all-url-less", "none", id="all-url-less"),
            pytest.param("some-url-less", "decision", id="some-url-less"),
            pytest.param("all-clean", "decision", id="all-clean"),
            pytest.param("all-malformed", "raise", id="all-malformed"),
            pytest.param("one-malformed-rest-url-less", "none", id="sparse-row-among-url-less"),
            pytest.param("one-malformed-rest-clean", "decision", id="sparse-row-among-clean"),
        ],
    )
    def test_the_cross_product(self, credit, title, candidate_credit, shape, expect):
        rows = {
            "empty": [],
            "all-url-less": [
                _url_less(candidate_credit, title, "a"),
                _url_less(candidate_credit, title, "b"),
            ],
            "some-url-less": [
                _url_less(candidate_credit, title, "a"),
                _clean(candidate_credit, title, "playable"),
            ],
            "all-clean": [
                _clean(candidate_credit, title, "playable"),
                _clean(candidate_credit, title, "other"),
            ],
            "all-malformed": [_malformed("a"), _malformed("b")],
            "one-malformed-rest-url-less": [
                _url_less(candidate_credit, title, "a"),
                _malformed("b"),
            ],
            "one-malformed-rest-clean": [
                _clean(candidate_credit, title, "playable"),
                _malformed("b"),
            ],
        }[shape]

        if expect == "raise":
            with pytest.raises(_EXTRACTION_ERRORS):
                decide_service_match(
                    rows, query_artist=credit, query_title=title, **_spotify_kwargs()
                )
            return

        decision = decide_service_match(
            rows, query_artist=credit, query_title=title, **_spotify_kwargs()
        )
        if expect == "none":
            assert decision is None
            return

        assert decision is not None
        assert decision.url, "a recorded decision always carries a URL"
        # The partition, not the candidate: a V/A query credit never reports two
        # axes, a named one always does when the guarded matcher accepted.
        expected_axes = AXES_TITLE_ONLY if credit == _VA_CREDIT else AXES_ARTIST_AND_TITLE
        assert decision.axes == expected_axes


class TestBlankQueryTautology:
    """``score_match("", "")`` is 100, and that applies to the artist axis too.

    The relaxed path was guarded against it on the title axis; the guarded path
    was guarded on neither. A blank credit does not take the V/A branch either --
    ``is_compilation_artist("")`` is False -- so it falls to the guarded matcher,
    which scores 100/100 against an equally blank candidate and would record
    ``artist+title``/``found`` at maximum confidence over nothing at all.
    """

    @pytest.mark.parametrize(
        ("query_artist", "query_title"),
        [("", "Nuggets"), ("   ", "Nuggets"), ("Various", ""), ("Stereolab", "   ")],
        ids=["blank-artist", "whitespace-artist", "blank-title", "whitespace-title"],
    )
    def test_a_blank_query_axis_is_never_a_decision(self, query_artist, query_title):
        rows = [_spotify_row(query_artist, query_title)]
        assert (
            decide_service_match(
                rows, query_artist=query_artist, query_title=query_title, **_spotify_kwargs()
            )
            is None
        )

    @pytest.mark.parametrize(
        ("candidate_credit", "candidate_title"),
        [("", ""), ("", _NAMED_TITLE), (_NAMED_CREDIT, "")],
        ids=["both-blank", "blank-artist", "blank-title"],
    )
    def test_a_blank_candidate_axis_is_never_a_decision(self, candidate_credit, candidate_title):
        """The candidate side of the same hole, on the axis the guarded pass claims.

        The row carries a real URL deliberately: without one the pre-pass's URL
        filter would drop it and this would pass for the wrong reason, telling us
        nothing about the blank-axis rule.
        """
        rows = [
            {
                "id": "x",
                "name": candidate_title,
                "artists": [{"name": candidate_credit}],
                "external_urls": {"spotify": "https://open.spotify.com/album/x"},
            }
        ]
        assert (
            decide_service_match(
                rows,
                query_artist=_NAMED_CREDIT,
                query_title=_NAMED_TITLE,
                **_spotify_kwargs(),
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

    @pytest.mark.parametrize("candidate_credit", ["Various", "Soundtrack"])
    def test_a_va_query_credit_never_reports_two_axes(self, candidate_credit):
        """The query credit is our sentinel, not catalog data, so scoring it lies.

        ``VA_QUERY_CREDIT`` is what the drain substitutes when
        ``build_compilation_query`` recovered no artist at all. A candidate
        credited *exactly* ``Various`` then scores 100 on the artist axis against
        it for free — not a marginal LML#1139 prefix clear but a tautology on a
        string we supplied — and would be recorded as ``artist+title``/``found``.
        That is the population most likely to be a wrong V/A link, so it is
        exactly the one that must carry the one-axis marker. The Phase 1 lane
        branches around this; the streaming lanes must too.
        """
        rows = [_spotify_row(candidate_credit, "Blues Masters, Vol. 1")]
        decision = decide_service_match(
            rows,
            query_artist=candidate_credit,
            query_title="Blues Masters, Vol. 1",
            **_spotify_kwargs(),
        )
        assert decision is not None
        assert decision.axes == AXES_TITLE_ONLY
        assert decision.status == STATUS_FOUND_TITLE_ONLY

    def test_a_real_query_credit_still_reports_two_axes(self):
        """The other half of the partition: a named credit gates on both axes."""
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

    def test_one_sparse_row_does_not_abort_a_lane_of_url_less_rows(self):
        """The URL filter must not shrink the systemic-break denominator.

        ``find_best_match`` re-raises when *every* row it sees fails extraction.
        Handing it a URL-filtered subset lets a single sparse row become "every
        row", so a response that is mostly well-formed-but-URL-less aborts the
        whole lane instead of skipping one candidate. The verdict has to be
        computed over the response as received.
        """
        url_less_a = {"id": "a", "name": "Blues Masters, Vol. 1", "artists": [{"name": "Various"}]}
        url_less_b = {"id": "b", "name": "Blues Masters, Vol. 2", "artists": [{"name": "Various"}]}
        sparse_with_url = {
            "id": "c",
            "name": "Blues Masters, Vol. 1",
            "artists": None,
            "external_urls": {"spotify": "https://open.spotify.com/album/c"},
        }
        assert (
            decide_service_match(
                [url_less_a, url_less_b, sparse_with_url],
                query_artist="Various",
                query_title="Blues Masters, Vol. 1",
                **_spotify_kwargs(),
            )
            is None
        )

    def test_every_row_failing_extraction_still_re_raises(self):
        """The signal itself survives: a wholly-malformed response is systemic."""
        with pytest.raises(_EXTRACTION_ERRORS):
            decide_service_match(
                [{"artists": None}, {"artists": None}],
                query_artist="Various",
                query_title="Nuggets",
                **_spotify_kwargs(),
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
