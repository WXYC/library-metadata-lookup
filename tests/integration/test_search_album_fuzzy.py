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

import pytest

from lookup.orchestrator import search_album_fuzzy


@pytest.mark.asyncio
async def test_va_series_with_volume_surfaces_against_long_paren_subtitle(library_db):
    """Repro for WXYC#531: V/A library row ``Disco Not Disco, vol. 1`` must
    surface against the Discogs canonical release whose title carries a long
    parenthetical subtitle.

    The pre-fix behavior is empty results because FTS5 implicit-AND on the full
    title cannot match the terse library row, the LIKE fallback requires every
    significant word in title/artist, and the fuzzy fallback candidate-trawls
    on the longest word's 3-char prefix.
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
    """Regression guard for the existing prefix branch: a Discogs query with a
    paren-suffix already matched a library row whose title is the base. E.g.,
    ``OK Computer (OKNOTOK Anniversary Edition)`` should surface a library row
    titled ``OK Computer`` via the pre-existing ``query.startswith(result)``
    branch.

    Uses ``Aluminum Tunes`` from the seed because no parenthetical-suffixed
    seed row exists; the assertion is that the un-suffixed library row
    surfaces.
    """
    results = await search_album_fuzzy(library_db, "Aluminum Tunes (Remastered)")
    titles = {r.title for r in results}
    assert "Aluminum Tunes" in titles
