"""Unit tests for `scripts/build_golden_corpus.py`'s pure helpers.

Covers `Fixture.route`'s conflict guard and the deduplicated library column
list (LML#1233 review). Everything else in that script needs a local
`library.db` plus a full Discogs dump and is exercised by hand at
regeneration time, not by this suite (see the script's own module docstring).
"""

from __future__ import annotations

import pytest

from scripts.build_golden_corpus import LIBRARY_FIELDS, Fixture


def _fixture_with(*release_ids: int) -> Fixture:
    fixture = Fixture()
    for rid in release_ids:
        fixture.releases[f"spec-{rid}"] = {"release_id": rid}
    return fixture


def test_route_records_a_fresh_key():
    fixture = _fixture_with(101)
    fixture.route(fixture.track_searches, "song|artist", ("spec-101",))
    assert fixture.track_searches == {"song|artist": [101]}


def test_route_allows_a_repeated_identical_route():
    """The same (key, resolved ids) pair recorded twice is not a conflict --
    only a DIFFERENT value for an existing key is."""
    fixture = _fixture_with(101)
    fixture.route(fixture.track_searches, "song|artist", ("spec-101",))
    fixture.route(fixture.track_searches, "song|artist", ("spec-101",))
    assert fixture.track_searches == {"song|artist": [101]}


def test_route_refuses_to_silently_overwrite_a_conflicting_key():
    """Two releases sharing a bare route key (e.g. an identical track title on
    two different pressings) must not let the second silently win.

    Already latent against the checked-in fixture (LML#1233 review): four
    track titles are shared across releases, and `route_key("untitled", None)`
    used to resolve to whichever of two releases (2807409, 3618272) routed it
    last.
    """
    fixture = _fixture_with(101, 202)
    fixture.route(fixture.track_searches, "untitled|", ("spec-101",))
    with pytest.raises(SystemExit):
        fixture.route(fixture.track_searches, "untitled|", ("spec-202",))
    # The loser's route must not have been silently applied.
    assert fixture.track_searches == {"untitled|": [101]}


def test_route_override_replaces_a_conflicting_key_deliberately():
    """FROZEN_*_ROUTES' use case: a frozen case's route is authoritative over
    whatever sampling independently derived for the same key."""
    fixture = _fixture_with(101, 202)
    fixture.route(fixture.track_searches, "untitled|", ("spec-101",))
    fixture.route(fixture.track_searches, "untitled|", ("spec-202",), override=True)
    assert fixture.track_searches == {"untitled|": [202]}


def test_library_fields_match_the_schema_under_test():
    from wxyc_etl.schema import library_columns

    assert LIBRARY_FIELDS == tuple(library_columns())
