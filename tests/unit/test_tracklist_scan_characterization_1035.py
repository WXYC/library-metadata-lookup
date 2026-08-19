"""Characterization tests for the tracklist-scan matching algorithm (LML#1035).

The track-validation matching algorithm exists twice: the API path
(``discogs.service._iter_title_matched_items`` + ``_scan_tracklist_for_match``,
reached through ``DiscogsService.validate_track_on_release``) and the cache
path (the inline loop inside ``DiscogsCacheService.validate_track_on_release``,
whose comments say "Mirrors the API path"). Both copies also duplicate
``_ARTIST_FUZZY_MATCH_THRESHOLD = 70`` and ``_TRACK_TITLE_FUZZY_MATCH_THRESHOLD
= 85`` verbatim.

This module pins the CURRENT behavior of BOTH copies against one shared table
of cases before either copy is touched. Each ``ScanCase`` is exercised against
both ``DiscogsService.validate_track_on_release`` (mocking ``get_release``) and
``DiscogsCacheService.validate_track_on_release`` (mocking the asyncpg pool),
so a divergence between the two copies shows up as a single parametrized
failure instead of two independently-drifting test files. Manual trace of both
implementations found them logically equivalent (title match: bidirectional
substring OR ``token_set_ratio`` fuzzy fallback; per-track artist: substring
then joined-credit fuzzy fallback; release-level artist: substring then fuzzy
fallback) -- these tests are the empirical confirmation of that trace, run
before the extraction in the same PR. No reconciliation was required; see the
PR body for the full diff writeup.

WXYC-representative fixtures throughout (Stereolab, Chuquimamani-Condori,
Juana Molina, Duke Ellington & John Coltrane, Sessa, Large Professor) per
CLAUDE.md's fixture guidance. Never Beatles/Radiohead/Queen.
"""

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from discogs.cache_service import DiscogsCacheService
from discogs.models import ReleaseMetadataResponse, TrackItem
from discogs.service import DiscogsService
from tests.unit.test_cache_service import make_fetch_router


@dataclass(frozen=True)
class ScanCase:
    """One tracklist-match scenario, expressed path-agnostically.

    ``release_track_title`` / ``release_track_artists`` describe the release's
    single tracklist row; ``release_artist`` is the release-level credit. Both
    the API-path ``ReleaseMetadataResponse``/``TrackItem`` shape and the
    cache-path asyncpg row shape are derived from these same fields, so the
    two paths are compared against identical input.
    """

    id: str
    release_artist: str
    release_track_title: str
    release_track_artists: list[str]
    query_track: str
    query_artist: str
    expected: bool
    note: str


CASES: list[ScanCase] = [
    ScanCase(
        id="title_exact_per_track_artist_substring",
        release_artist="Various",
        release_track_title="Back, Baby",
        release_track_artists=["Jessica Pratt"],
        query_track="Back, Baby",
        query_artist="Jessica Pratt",
        expected=True,
        note="Exact title, direct per-track artist substring match.",
    ),
    ScanCase(
        id="title_fuzzy_only_dropped_word",
        release_artist="Chuquimamani-Condori",
        release_track_title="Call Your Name",
        release_track_artists=[],
        query_track="call name",
        query_artist="Chuquimamani-Condori",
        expected=True,
        note=(
            "Query drops the interior word 'Your'; token_set_ratio scores "
            "100 (>= 85 floor). No per-track credit, so the release-level "
            "artist substring-matches once the title gate opens on the "
            "fuzzy fallback."
        ),
    ),
    ScanCase(
        id="title_miss_unrelated",
        release_artist="Juana Molina",
        release_track_title="Call Your Name",
        release_track_artists=[],
        query_track="Aluminum Tunes",
        query_artist="Juana Molina",
        expected=False,
        note=(
            "Query track title is unrelated to the release's only track "
            "(token_set_ratio ~43, well below the 85 floor; no substring "
            "relation either). The artist would match if the title did, so "
            "this isolates the title gate."
        ),
    ),
    ScanCase(
        id="per_track_credits_miss_release_level_substring_fallback",
        release_artist="Stereolab",
        release_track_title="Percolator",
        release_track_artists=["Laetitia Sadier", "Tim Gane"],
        query_track="Percolator",
        query_artist="Stereolab",
        expected=True,
        note=(
            "Per-track credits list the two band members, not the band "
            "name; neither the individual substring check nor the joined "
            "fuzzy fallback (score ~18) reaches 'Stereolab', so the "
            "release-level artist substring fallback must fire."
        ),
    ),
    ScanCase(
        id="joined_multiartist_fuzzy_duke_ellington_coltrane",
        release_artist="Various",
        release_track_title="In A Sentimental Mood",
        release_track_artists=["Duke Ellington", "John Coltrane"],
        query_track="In A Sentimental Mood",
        query_artist="Ellington Coltrane",
        expected=True,
        note=(
            "WXYC flowsheet-style compact credit for a joined performance. "
            "Neither 'Duke Ellington' nor 'John Coltrane' individually "
            "substring-matches 'Ellington Coltrane', but the joined credit "
            "'Duke Ellington John Coltrane' scores 100 on token_set_ratio "
            "(>= 70 floor) -- LML#210's canonical joined-credit case."
        ),
    ),
    ScanCase(
        id="fuzzy_reject_unrelated_artist",
        release_artist="Duke Ellington & John Coltrane",
        release_track_title="In A Sentimental Mood",
        release_track_artists=[],
        query_track="In A Sentimental Mood",
        query_artist="Thelonious Monk",
        expected=False,
        note=(
            "Title matches; the release-level fuzzy fallback must still "
            "reject an unrelated artist (token_set_ratio ~42, well below "
            "the 70 floor)."
        ),
    ),
    ScanCase(
        id="anv_suffix_per_track_artist_match",
        release_artist="Various",
        release_track_title="Pequena Vertigem de Amor",
        release_track_artists=["Sessa*"],
        query_track="Pequena Vertigem de Amor",
        query_artist="Sessa",
        expected=True,
        note=(
            "Discogs ANV disambiguation asterisk on the per-track credit "
            "('Sessa*'). Neither normalize function strips '*', but "
            "bidirectional substring still matches ('sessa' in 'sessa*')."
        ),
    ),
    ScanCase(
        id="anv_suffix_release_level_artist_match",
        release_artist="Large Professor*",
        release_track_title="1st Class",
        release_track_artists=[],
        query_track="1st Class",
        query_artist="Large Professor",
        expected=True,
        note="Same ANV-asterisk tolerance, exercised on the release-level credit.",
    ),
    # -----------------------------------------------------------------
    # LML#1225: query-token coverage gate. A title-matched entry (either
    # arm) must also have the QUERY substantially accounted for by the
    # matched title, not just the reverse. The three recall anchors below
    # are LML#334's original fuzzy-fallback motivating cases (added here
    # per LML#1225's acceptance criteria, not previously present in this
    # shared table); the two reject cases are LML#1225's production
    # repros, each entering through a different title-gate arm.
    # -----------------------------------------------------------------
    ScanCase(
        id="lml334_recall_singular_plural_typo",
        release_artist="Stereolab",
        release_track_title="Towers Of Dub",
        release_track_artists=[],
        query_track="tower of dub",
        query_artist="Stereolab",
        expected=True,
        note=(
            "LML#334 recall anchor. 'tower' vs 'towers' breaks the substring "
            "gate; token_set_ratio ~96 clears the fuzzy floor and query-token "
            "coverage is 100% (every query token fuzzy-matches a title token)."
        ),
    ),
    ScanCase(
        id="lml334_recall_dropped_interior_word",
        release_artist="Stereolab",
        release_track_title="Smells Like Teen Spirit",
        release_track_artists=[],
        query_track="smells teen spirit",
        query_artist="Stereolab",
        expected=True,
        note=(
            "LML#334 recall anchor. Dropping the interior word 'like' scores "
            "100 on token_set_ratio and 100% query-token coverage -- every "
            "remaining query token is a title token verbatim."
        ),
    ),
    ScanCase(
        id="lml334_recall_dash_vs_paren_suffix",
        release_artist="Stereolab",
        release_track_title="de la soul (radio edit)",
        release_track_artists=[],
        query_track="de la soul - radio edit",
        query_artist="Stereolab",
        expected=True,
        note=(
            "LML#334 recall anchor. Both sides normalize to the identical "
            "string 'de la soul radio edit' (punctuation-stripped), so this "
            "clears the substring arm outright at 100% query-token coverage."
        ),
    ),
    ScanCase(
        id="lml1225_reject_fuzzy_arm_noise_padded_query",
        release_artist="Population One",
        release_track_title="Battle For Space",
        release_track_artists=[],
        query_track="Space Lizzard Battle Star hell cat",
        query_artist="Population One",
        expected=False,
        note=(
            "LML#1225 production repro 1. token_set_ratio scores 85.71 (>= "
            "the 85 fuzzy floor) because it structurally ignores the four "
            "query tokens that match nothing ('lizzard', 'star', 'hell', "
            "'cat'). Query-token coverage is 2/6 (33%) -- below the "
            "TRACK_TITLE_QUERY_COVERAGE_THRESHOLD floor, so the fuzzy arm's "
            "accept is overridden and the entry is rejected. Same artist on "
            "both sides mirrors the real _validate_one call, which always "
            "passes release.artist regardless of strategy -- see "
            "lookup/strategies/track_release_matching.py."
        ),
    ),
    ScanCase(
        id="lml1225_reject_substring_arm_noise_padded_query",
        release_artist="Ludwig van Beethoven",
        release_track_title="Symphony No. 9",
        release_track_artists=[],
        query_track="Purple Refrigerator Symphony No 9",
        query_artist="Ludwig van Beethoven",
        expected=False,
        note=(
            "LML#1225 production repro 2. The normalized title 'symphony no "
            "9' is a literal substring of the normalized query, so this "
            "enters via the bidirectional-substring arm and never reaches "
            "the fuzzy fallback at all. Query-token coverage is 3/5 (60%) -- "
            "'purple' and 'refrigerator' match nothing in the title -- so "
            "the coverage gate rejects it even though the substring arm "
            "alone would have accepted it. Proves the gate covers BOTH arms, "
            "not just the fuzzy one."
        ),
    ),
]


def _api_release(case: ScanCase) -> ReleaseMetadataResponse:
    return ReleaseMetadataResponse(
        release_id=1,
        title="Test Release",
        artist=case.release_artist,
        release_url="https://discogs.com/release/1",
        tracklist=[
            TrackItem(
                position="1",
                title=case.release_track_title,
                artists=case.release_track_artists,
            )
        ],
    )


def _configure_cache_pool(case: ScanCase, mock_asyncpg_pool) -> None:
    mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
    track_artist_rows = (
        [{"track_sequence": 1, "artist_name": name} for name in case.release_track_artists]
        if case.release_track_artists
        else []
    )
    mock_asyncpg_pool.fetch = AsyncMock(
        side_effect=make_fetch_router(
            release_track_artist=track_artist_rows,
            release_track=[{"sequence": 1, "title": case.release_track_title}],
        )
    )
    mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": case.release_artist})


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
class TestTracklistScanCharacterization:
    """Same table, run against both duplicate implementations (LML#1035)."""

    @pytest.mark.asyncio
    async def test_api_path(self, case: ScanCase):
        service = DiscogsService(token="test-token")
        release = _api_release(case)
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, case.query_track, case.query_artist)
        assert result is case.expected, case.note

    @pytest.mark.asyncio
    async def test_cache_path(self, case: ScanCase, mock_asyncpg_pool):
        cache_service = DiscogsCacheService(mock_asyncpg_pool)
        _configure_cache_pool(case, mock_asyncpg_pool)
        result = await cache_service.validate_track_on_release(
            1, case.query_track, case.query_artist
        )
        assert result is case.expected, case.note
