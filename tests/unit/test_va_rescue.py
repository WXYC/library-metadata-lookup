"""Unit tests for the V/A-compilation rescue (LML#784 category 2).

The two evidence shapes, the precision-guard rationale, and the calibrated
score numbers live in ``lookup/strategies/va_rescue.py``'s module docstring —
these tests pin that behavior: the Perry (embedded-title-segment) and
Tasquier (tracklist-credit) shapes resolve, the structural gates hold, and
the #719 subset-inflation shape stays rejected.
"""

from __future__ import annotations

import pytest

from discogs.models import ReleaseMetadataResponse
from generated.api_models import DiscogsTrackItem
from lookup.strategies.va_rescue import _title_variants, find_va_comp_match
from tests.factories import make_discogs_result

PERRY_COMP_TITLE = "Lee Scratch Perry - Born In The Sky (Upsetter At The Controls 1969-1975)"
TASQUIER_COMP_TITLE = (
    "Prends Le Temps D'écouter - Musique D'expression Libre Des Enfants De 7 À 11 Ans"
)


def make_release_metadata(tracklist_artists: list[list[str]], titles: list[str] | None = None):
    """Minimal ReleaseMetadataResponse stand-in with a tracklist."""
    return ReleaseMetadataResponse(
        release_id=1,
        title="A Compilation",
        artist="Various",
        release_url="https://www.discogs.com/release/1",
        tracklist=[
            DiscogsTrackItem(
                position=str(i + 1),
                title=titles[i] if titles else f"Track {i + 1}",
                artists=artists,
            )
            for i, artists in enumerate(tracklist_artists)
        ],
    )


class TestTitleVariants:
    """Delimiter-bounded variant generation."""

    def test_segment_cap_keeps_longest_bounded_prefix(self):
        """A >4-segment title drops segments past ``_MAX_SEGMENTS`` but must
        still emit the capped 4-segment prefix — its longest bounded variant
        distinct from the full title."""
        variants = _title_variants("A - B - C - D - E")
        assert "A - B - C - D" in variants
        assert "E" not in variants  # capped out as a segment
        assert "A - B - C - D - E" in variants  # the full title always scores


class TestEmbeddedTitleSegmentShape:
    """The Perry shape: performer name embedded in the comp's title segments."""

    @pytest.mark.asyncio
    async def test_perry_comp_resolves_via_title_segments(self, mock_discogs_service):
        candidate = make_discogs_result(release_id=632150, artist="Various", album=PERRY_COMP_TITLE)
        best = await find_va_comp_match(
            [candidate],
            query_artist="lee scratch perry",
            query_album="Born in the Sky",
            discogs_service=mock_discogs_service,
        )
        assert best is candidate
        # Segment evidence sufficed — no tracklist fetch.
        mock_discogs_service.get_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_album_axis_uses_paren_stripped_segment(self, mock_discogs_service):
        """'Born in the Sky' clears only against the segment with its trailing
        parenthetical stripped — the full segment scores 44.8."""
        candidate = make_discogs_result(release_id=632150, artist="Various", album=PERRY_COMP_TITLE)
        best = await find_va_comp_match(
            [candidate],
            query_artist="Lee Scratch Perry",
            query_album="Born In The Sky",
            discogs_service=mock_discogs_service,
        )
        assert best is candidate


class TestTracklistCreditShape:
    """The Tasquier shape: performer credited per-track on the comp."""

    @pytest.mark.asyncio
    async def test_tasquier_comp_resolves_via_tracklist_credit(self, mock_discogs_service):
        candidate = make_discogs_result(
            release_id=27518829, artist="Various", album=TASQUIER_COMP_TITLE
        )
        mock_discogs_service.get_release.return_value = make_release_metadata(
            [["Lionel Tasquier"], ["Somebody Else"], []]
        )
        best = await find_va_comp_match(
            [candidate],
            query_artist="Lionel Tasquier",
            query_album="Prends Le Temps D'écouter",
            discogs_service=mock_discogs_service,
        )
        assert best is candidate
        mock_discogs_service.get_release.assert_awaited_once_with(27518829)

    @pytest.mark.asyncio
    async def test_credit_embedded_in_track_title_resolves(self, mock_discogs_service):
        """The real 27518829 shape (verified live): the structured per-track
        ``artists`` array is empty — the performer lives in ``extraartists``
        (which the API model drops) and embedded in the track title
        ("Lionel Tasquier - Hiroshima"). The title's " - " segments are the
        evidence."""
        candidate = make_discogs_result(
            release_id=27518829, artist="Various", album=TASQUIER_COMP_TITLE
        )
        mock_discogs_service.get_release.return_value = make_release_metadata(
            [[], [], []],
            titles=[
                "Frédéric Chanu - Prends Le Temps D'écouter",
                "Lionel Tasquier - Hiroshima",
                "Classe de perfectionnement - Voix + Tube à Musique",
            ],
        )
        best = await find_va_comp_match(
            [candidate],
            query_artist="Lionel Tasquier",
            query_album="Prends Le Temps D'écouter",
            discogs_service=mock_discogs_service,
        )
        assert best is candidate

    @pytest.mark.asyncio
    async def test_no_tracklist_credit_rejects(self, mock_discogs_service):
        candidate = make_discogs_result(
            release_id=27518829, artist="Various", album=TASQUIER_COMP_TITLE
        )
        mock_discogs_service.get_release.return_value = make_release_metadata(
            [["Somebody Else"], []]
        )
        best = await find_va_comp_match(
            [candidate],
            query_artist="Lionel Tasquier",
            query_album="Prends Le Temps D'écouter",
            discogs_service=mock_discogs_service,
        )
        assert best is None

    @pytest.mark.asyncio
    async def test_missing_release_or_tracklist_rejects(self, mock_discogs_service):
        candidate = make_discogs_result(
            release_id=27518829, artist="Various", album=TASQUIER_COMP_TITLE
        )
        mock_discogs_service.get_release.return_value = None
        best = await find_va_comp_match(
            [candidate],
            query_artist="Lionel Tasquier",
            query_album="Prends Le Temps D'écouter",
            discogs_service=mock_discogs_service,
        )
        assert best is None

    @pytest.mark.asyncio
    async def test_get_release_failure_skips_candidate(self, mock_discogs_service):
        """A fetch failure (outage, breaker shed) degrades to no-match, not a raise."""
        candidate = make_discogs_result(
            release_id=27518829, artist="Various", album=TASQUIER_COMP_TITLE
        )
        mock_discogs_service.get_release.side_effect = Exception("breaker open")
        best = await find_va_comp_match(
            [candidate],
            query_artist="Lionel Tasquier",
            query_album="Prends Le Temps D'écouter",
            discogs_service=mock_discogs_service,
        )
        assert best is None


class TestGates:
    @pytest.mark.asyncio
    async def test_non_va_candidates_are_ignored(self, mock_discogs_service):
        """Only compilation-credited candidates enter the rescue — a normal
        artist credit already had its fair shot at the standard floor."""
        candidate = make_discogs_result(
            release_id=316672, artist="Lee Perry", album=PERRY_COMP_TITLE
        )
        best = await find_va_comp_match(
            [candidate],
            query_artist="lee scratch perry",
            query_album="Born in the Sky",
            discogs_service=mock_discogs_service,
        )
        assert best is None
        mock_discogs_service.get_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_compilation_query_artist_skips_rescue(self, mock_discogs_service):
        """A 'Various' query has no performer signal to verify against — the
        #638/#592 shape where the title is the only axis. Skip entirely."""
        candidate = make_discogs_result(release_id=632150, artist="Various", album=PERRY_COMP_TITLE)
        best = await find_va_comp_match(
            [candidate],
            query_artist="Various Artists",
            query_album="Born in the Sky",
            discogs_service=mock_discogs_service,
        )
        assert best is None
        mock_discogs_service.get_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_results_returns_none(self, mock_discogs_service):
        best = await find_va_comp_match(
            [],
            query_artist="Lionel Tasquier",
            query_album="Prends Le Temps D'écouter",
            discogs_service=mock_discogs_service,
        )
        assert best is None

    @pytest.mark.asyncio
    async def test_album_axis_must_pass_before_tracklist_fetch(self, mock_discogs_service):
        """Artist evidence alone is not enough — an unrelated comp that happens
        to feature the artist stays out, and we never pay its tracklist fetch."""
        candidate = make_discogs_result(
            release_id=999,
            artist="Various",
            album="Upsetter Shop Volume 2 (Dub Plates & Rarities)",
        )
        best = await find_va_comp_match(
            [candidate],
            query_artist="lee scratch perry",
            query_album="Born in the Sky",
            discogs_service=mock_discogs_service,
        )
        assert best is None
        mock_discogs_service.get_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_degenerate_axes_skip_rescue(self, mock_discogs_service):
        """A (near-)identical artist and album — the typed-out self-titled
        shape, or the category-4 placeholder swap — makes the dual-axis
        requirement degenerate: one embedded title segment would satisfy
        both axes. A V/A compilation is never anyone's self-titled album,
        so the rescue must refuse the query outright."""
        candidate = make_discogs_result(
            release_id=999,
            artist="Various",
            album="Matmos - A Various Artists Tribute (Remixes)",
        )
        best = await find_va_comp_match(
            [candidate],
            query_artist="Matmos",
            query_album="Matmos",
            discogs_service=mock_discogs_service,
        )
        assert best is None
        mock_discogs_service.get_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_artist_segment_evidence_alone_is_not_enough(self, mock_discogs_service):
        """The vice-versa arm of the dual-axis requirement: the performer's
        name embedded in the title clears the artist axis, but a failing
        album axis still rejects — and never pays a tracklist fetch."""
        candidate = make_discogs_result(
            release_id=999,
            artist="Various",
            album="Lee Scratch Perry - Presents The Full Experience",
        )
        best = await find_va_comp_match(
            [candidate],
            query_artist="Lee Scratch Perry",
            query_album="Born in the Sky",
            discogs_service=mock_discogs_service,
        )
        assert best is None
        mock_discogs_service.get_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_passing_candidate_wins(self, mock_discogs_service):
        """Input order is confidence order — the first dual-axis pass is taken."""
        weaker = make_discogs_result(release_id=1, artist="Various", album=PERRY_COMP_TITLE)
        also_passing = make_discogs_result(release_id=2, artist="Various", album=PERRY_COMP_TITLE)
        best = await find_va_comp_match(
            [weaker, also_passing],
            query_artist="Lee Scratch Perry",
            query_album="Born in the Sky",
            discogs_service=mock_discogs_service,
        )
        assert best is weaker


class TestPrecisionGuards:
    """The #719 subset-inflation shape must stay rejected (LML#719/#721)."""

    @pytest.mark.asyncio
    async def test_719_parenthetical_shape_stays_rejected(self, mock_discogs_service):
        """The literal #719 candidate: a short generic query buried in a long
        unrelated parenthetical title. token_sort scores 42.9 full / 18.2
        paren-stripped — no segment variant rescues it."""
        candidate = make_discogs_result(
            release_id=1, artist="Various", album="Black Leather (The Hound Dog Mix)"
        )
        mock_discogs_service.get_release.return_value = make_release_metadata([["Elvis Vester"]])
        best = await find_va_comp_match(
            [candidate],
            query_artist="Elvis Vester",
            query_album="Hound Dog",
            discogs_service=mock_discogs_service,
        )
        assert best is None

    @pytest.mark.asyncio
    async def test_marginal_segment_pass_still_needs_artist_evidence(self, mock_discogs_service):
        """A dash-segmented variant of the same title lets the album axis pass
        marginally (token_sort 81.8 on 'The Hound Dog Mix' — the floor's
        normal marginal band, not inflation). The artist axis stays the
        load-bearing gate: no segment or tracklist evidence -> rejected."""
        candidate = make_discogs_result(
            release_id=1, artist="Various", album="Black Leather - The Hound Dog Mix"
        )
        mock_discogs_service.get_release.return_value = make_release_metadata(
            [["Somebody Unrelated"]]
        )
        best = await find_va_comp_match(
            [candidate],
            query_artist="Elvis Vester",
            query_album="Hound Dog",
            discogs_service=mock_discogs_service,
        )
        assert best is None
