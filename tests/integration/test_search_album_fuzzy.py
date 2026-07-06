"""Integration tests for ``search_album_fuzzy`` against a real SQLite+FTS5.

The unit tests in ``tests/unit/test_orchestrator_gaps.py::TestSearchAlbumFuzzy``
stub ``db.search``, which hides FTS5 / LIKE-fallback / fuzzy-fallback semantics.
WXYC#531 shipped a fix (PR #552) whose unit tests passed but whose end-to-end
behavior against a real library was a no-op — the helper sat in a post-filter
block that never received candidates because the un-stripped Discogs subtitle
query produced empty results. These tests exercise the real search path so
that class of regression can't recur silently.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import ReleaseInfo, TrackReleasesResponse
from lookup.strategies.track_on_compilation import search_compilations_for_track
from lookup.strategies.track_release_matching import search_album_fuzzy
from services.parser import ParsedRequest


@pytest.mark.asyncio
async def test_va_series_with_volume_surfaces_against_long_paren_subtitle(library_db):
    """Repro for WXYC#531: V/A library row ``Disco Not Disco, vol. 1`` must
    surface against the Discogs canonical release whose title carries a long
    parenthetical subtitle.

    Pre-fix behavior is empty results: FTS5 throws on ``&`` in the subtitle,
    ``db.search`` swallows the syntax error and falls through to the LIKE /
    fuzzy ladder, the LIKE fallback requires every significant word to appear
    in title or artist (the subtitle's words don't), and the fuzzy fallback
    candidate-trawls on the longest word's 3-char prefix and surfaces
    tangentially related rows that the post-filter then drops.
    """
    results = await search_album_fuzzy(
        library_db,
        "Disco Not Disco (Post Punk, Electro & Leftfield Disco Classics 1974-1986)",
    )
    titles = {r.title for r in results}
    assert "Disco Not Disco, vol. 1" in titles, (
        f"V/A vol.1 must surface against the long-subtitle Discogs query, got: {titles}"
    )
    assert "Disco Not Disco, vol. 2" in titles, (
        f"V/A vol.2 must also surface — both volumes share the base, got: {titles}"
    )


@pytest.mark.asyncio
async def test_va_series_surfaces_against_various_prefix_retry(library_db):
    """The compilation retry path in ``process_release`` prepends ``Various ``
    to the album title when the base search returns no matches. That retry must
    also surface the V/A row when the Discogs title carries a paren subtitle.
    """
    results = await search_album_fuzzy(
        library_db,
        "Various Disco Not Disco (Post Punk, Electro & Leftfield Disco Classics 1974-1986)",
    )
    titles = {r.title for r in results}
    assert "Disco Not Disco, vol. 1" in titles, (
        f"Various-prefix retry must surface V/A vol.1, got: {titles}"
    )


@pytest.mark.asyncio
async def test_plain_base_query_unchanged(library_db):
    """Regression guard: a query that already matches by FTS5 (no paren,
    exact prefix of the library row) must continue to surface. This locks in
    the existing prefix-acceptance branch in ``album_title_acceptable``.
    """
    results = await search_album_fuzzy(library_db, "Disco Not Disco")
    titles = {r.title for r in results}
    assert "Disco Not Disco, vol. 1" in titles
    assert "Disco Not Disco, vol. 2" in titles


@pytest.mark.asyncio
async def test_paren_on_discogs_side_with_matching_library_base(library_db):
    """Exercises the new paren-strip retry against a non-V/A library row.

    For ``Aluminum Tunes (Remastered)``, the un-stripped query trips FTS5's
    special-char handling on ``(`` — ``db.search`` swallows the syntax error
    and falls through the LIKE / fuzzy ladder. The LIKE fallback eliminates
    the Stereolab row because ``"Remastered"`` is absent from title and
    artist; the fuzzy fallback's longest-word 3-char prefix (``rem``) only
    coincidentally surfaces the row via ``Stereolab``. The paren-strip
    retry rebuilds the query as plain ``Aluminum Tunes``, which FTS5
    answers cleanly and the existing prefix branch in
    ``album_title_acceptable`` accepts via ``query.startswith(result)``.
    """
    results = await search_album_fuzzy(library_db, "Aluminum Tunes (Remastered)")
    titles = {r.title for r in results}
    assert "Aluminum Tunes" in titles


@pytest.mark.asyncio
async def test_non_va_paren_match_filtered_by_downstream_artist_gate(library_db):
    """The widened matching layer surfaces non-V/A rows for any Discogs query
    that shares a prefix with a library title; the downstream artist gate
    inside ``process_release`` must drop them when artists don't match.

    Seed row 60001 catalogues ``Live Sessions, vol. 2`` under ``Some Band``.
    A Discogs release titled ``Live Sessions (Acoustic Recordings From The
    Greek Theatre 1998-2002)`` credited to ``Some Other Band`` will be
    surfaced by ``search_album_fuzzy`` (the paren-strip retry produces
    ``Live Sessions``, which prefix-matches row 60001's title). Without the
    artist gate the row would slip through to the user, despite the artist
    mismatch.

    Pins three things in one test:

    1. ``search_album_fuzzy`` does widen for non-V/A rows (the strip-retry
       is unconditional on V/A-ness — by design, since V/A-ness is a
       library-row property unknown at query construction time).
    2. ``process_release``'s ``artist_matches_item`` filter drops the
       non-V/A row when ``parsed.artist`` doesn't prefix-match the
       library-row artist, so the false positive never surfaces.
    3. The ``Live Sessions, vol. 2`` seed row earns its keep as a
       regression fixture — adding it to the SQLite seed without a test
       asserting on it was the gap this test closes.
    """
    parsed = ParsedRequest(
        artist="Some Other Band",
        song="Recording 3",
        raw_message="Recording 3 by Some Other Band",
    )

    service = AsyncMock()
    service.cache_service = None

    async def _track_releases(track, artist=None, artist_as_keyword=False, **_):
        # Wave A returns the non-V/A release shape that the seed row's title
        # prefix-matches after the paren-strip. Wave B returns empty (no
        # genuine V/A surfaces; the row is a false-positive candidate).
        releases = (
            []
            if artist_as_keyword
            else [
                ReleaseInfo(
                    album="Live Sessions (Acoustic Recordings From The Greek Theatre 1998-2002)",
                    artist="Some Other Band",
                    release_id=70001,
                    release_url="https://www.discogs.com/release/70001",
                    is_compilation=False,
                ),
            ]
        )
        return TrackReleasesResponse(
            track=track,
            artist=artist,
            releases=list(releases),
            total=len(releases),
        )

    service.search_releases_by_track = AsyncMock(side_effect=_track_releases)
    # Track validation would say True (track is on the release) — the artist
    # gate must filter before we ever get here. If it didn't, the row would
    # surface in results and this test would fail.
    service.validate_track_on_release = AsyncMock(return_value=True)

    # First pin the matching layer: search_album_fuzzy must surface the row
    # via the paren-strip retry. Without this assertion the end-to-end check
    # below could silently pass a regression in the strip-retry (nothing to
    # filter = nothing in titles, indistinguishable from "filtered correctly").
    matching_layer = await search_album_fuzzy(
        library_db,
        "Live Sessions (Acoustic Recordings From The Greek Theatre 1998-2002)",
    )
    matching_layer_titles = {item.title for item in matching_layer}
    assert "Live Sessions, vol. 2" in matching_layer_titles, (
        f"Matching layer must widen via the paren-strip retry — without this "
        f"the artist-gate assertion below is vacuous, got: {matching_layer_titles}"
    )

    with patch(
        "lookup.strategies.track_on_compilation.lookup_releases_by_track",
        new_callable=AsyncMock,
        return_value=[],
    ):
        items, _titles = await search_compilations_for_track(
            library_db, parsed, discogs_service=service
        )

    titles = {item.title for item in items}
    assert "Live Sessions, vol. 2" not in titles, (
        f"Non-V/A row paired with a non-matching Discogs artist must be filtered "
        f"by process_release's artist gate, got: {titles}"
    )
