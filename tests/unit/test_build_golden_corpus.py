"""Unit tests for `scripts/build_golden_corpus.py`'s pure helpers.

Covers `Fixture.route`'s conflict guard and the deduplicated library column
list (LML#1233 review). Everything else in that script needs a local
`library.db` plus a full Discogs dump and is exercised by hand at
regeneration time, not by this suite (see the script's own module docstring).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_golden_corpus import (
    LIBRARY_FIELDS,
    Fixture,
    _case,
    frozen_cases,
    merge_expectations,
)
from tests.e2e.golden import corpus


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


# ---------------------------------------------------------------------------
# The regeneration contract (LML#1233 re-land review)
# ---------------------------------------------------------------------------
#
# `cases.json` is authoritative for what a case *expects*; the builder is
# authoritative for what a case *is*. The seam between them is
# `merge_expectations`, and before these tests it carried exactly one key
# across -- `expect`. Every other field was re-stamped from the builder's
# hardcoded tables, so a human judgement recorded only in `cases.json`
# (promoting a case to `frozen`, citing its issue, declaring the rows it
# needs) was reverted by the next sanctioned regeneration, silently and with
# the expectation still carried forward to make the result look healthy.
#
# That is the same laundering path `frozen_cases()`'s docstring and the
# rebaseline tool's frozen-drift refusal exist to close, reached through the
# other door: `rebaseline_golden_corpus.py` refuses to rewrite a frozen case,
# but nothing stopped a regeneration from quietly making it non-frozen first.


@pytest.fixture
def tmp_cases(tmp_path: Path):
    """Write a cases.json-shaped file and hand back its path.

    `merge_expectations` reads the committed corpus off disk, so these tests
    drive it through a real file rather than a stubbed loader -- the JSON
    round trip is part of what they are pinning.
    """

    def _write(cases: list[dict]) -> Path:
        path = tmp_path / "cases.json"
        path.write_text(json.dumps(cases, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    return _write


def test_merge_expectations_carries_a_human_judgement_forward(tmp_cases):
    """A promotion recorded only in `cases.json` must survive regeneration.

    The concrete case: `track-miss-stereolab-zzyzx-marginal-fanfare` was
    recorded `suspect`, then promoted to `frozen` with an issue and a
    `requires_rows` declaration once LML#1239 landed the fix it was waiting
    for. Regenerating the skeleton must not undo that.
    """
    skeleton = [
        {
            "id": "case-a",
            "shape": "artist_song",
            "suspect": True,
            "note": "generated note",
            "query": {"artist": "Stereolab", "song": "Zzyzx Marginal Fanfare"},
            "expect": None,
        }
    ]
    committed = tmp_cases(
        [
            {
                "id": "case-a",
                "shape": "artist_song",
                "frozen": True,
                "suspect": False,
                "issue": "LML#1239",
                "note": "hand-authored note recording why this is frozen",
                "query": {"artist": "Stereolab", "song": "Zzyzx Marginal Fanfare"},
                "requires_rows": [["Stereolab", "Peng!"]],
                "expect": {
                    "miss_kind": "hit",
                    "song_not_found": True,
                    "found_on_compilation": False,
                    "results": ["#1 Stereolab — Peng!"],
                },
            }
        ]
    )

    merged = merge_expectations(skeleton, committed)[0]

    assert merged["frozen"] is True
    assert merged["suspect"] is False
    assert merged["issue"] == "LML#1239"
    assert merged["requires_rows"] == [["Stereolab", "Peng!"]]
    assert merged["note"] == "hand-authored note recording why this is frozen"
    assert merged["expect"]["song_not_found"] is True


def test_merge_expectations_still_regenerates_the_generated_facts(tmp_cases):
    """The complement, and the reason this is a roster rather than "keep everything".

    `id`, `shape` and `query` describe what the builder sampled. If a
    regeneration re-derives them, the new values win -- otherwise a stale
    query would sit in front of a carried-forward expectation, which is the
    failure mode in the opposite direction.
    """
    skeleton = [
        {"id": "case-a", "shape": "song_only", "query": {"song": "New Sample"}, "expect": None}
    ]
    committed = tmp_cases(
        [
            {
                "id": "case-a",
                "shape": "artist_song",
                "query": {"artist": "Old", "song": "Old Sample"},
                "expect": {
                    "miss_kind": "miss_clean",
                    "song_not_found": True,
                    "found_on_compilation": False,
                    "results": [],
                },
            }
        ]
    )

    merged = merge_expectations(skeleton, committed)[0]

    assert merged["shape"] == "song_only"
    assert merged["query"] == {"song": "New Sample"}


def test_a_genuinely_new_case_keeps_its_generated_judgement(tmp_cases):
    """Carry-forward must not blank a new case's own fields.

    A case with no prior entry has nothing to carry, so the builder's stamp
    stands -- including `suspect: True`, which is how a newly-observed
    doubtful verdict gets flagged in the first place.
    """
    skeleton = [
        {
            "id": "brand-new",
            "shape": "song_only",
            "suspect": True,
            "note": "generated",
            "query": {"song": "x"},
            "expect": None,
        }
    ]

    merged = merge_expectations(skeleton, tmp_cases([]))[0]

    assert merged["suspect"] is True
    assert merged["note"] == "generated"


def test_the_frozen_roster_agrees_with_the_committed_corpus():
    """The frozen roster is authored in two places; nothing tied them together.

    `cases.json` and `build_golden_corpus.frozen_cases()` must name the same
    frozen cases. They drifted the moment a sampled case was hand-promoted
    (7 in the corpus, 6 in the builder), and the carry-forward above now
    makes such a promotion *survive* -- which is exactly why this has to be
    checked rather than inferred. A case worth freezing is worth recording
    where the generator can see it, or the next regeneration re-derives a
    corpus that disagrees with the committed one about what is load-bearing.
    """
    committed = {
        case["id"]
        for case in json.loads(corpus.CASES_PATH.read_text(encoding="utf-8"))
        if case.get("frozen")
    }
    rostered = {case["id"] for case in frozen_cases()}

    assert committed == rostered, (
        f"frozen in cases.json but not in frozen_cases(): {sorted(committed - rostered)}; "
        f"in frozen_cases() but not frozen in cases.json: {sorted(rostered - committed)}. "
        "Add the case to scripts/build_golden_corpus.py::frozen_cases() so a regeneration "
        "reproduces it, or drop the frozen mark."
    )


# ---------------------------------------------------------------------------
# One serializer for cases.json (LML#1233 re-land review)
# ---------------------------------------------------------------------------


def test_case_emits_exactly_what_the_corpus_serializer_emits():
    """`_case` must not be a second encoding of the case shape.

    It was: `_case` hand-built the dict while `corpus.Case.to_json` -- the
    canonical serializer for the same shape -- sat with no callers at all. The
    two had already drifted on key order (`_case` put `settings` right after
    `frozen`, `to_json` puts it after `query`), and `cases.json` is
    byte-compared by `test_cases_file_is_canonically_formatted`, so the
    divergence would surface as key-reorder churn the first time a sampled case
    carried a `settings` block.
    """
    emitted = _case(
        "case-a",
        "artist_song",
        "a note",
        {"artist": "Stereolab", "song": "Peng!"},
        suspect=True,
        settings={"lml_resolve_nonlibrary_release": True},
        requires_discogs=True,
        requires_rows=[["Stereolab", "Peng!"]],
    )

    expected = corpus.Case(
        id="case-a",
        shape="artist_song",
        note="a note",
        query={"artist": "Stereolab", "song": "Peng!"},
        suspect=True,
        settings={"lml_resolve_nonlibrary_release": True},
        requires_discogs=True,
        requires_rows=(("Stereolab", "Peng!"),),
        expect=None,
    ).to_json()

    assert emitted == expected
    assert list(emitted) == list(expected), (
        "key ORDER must match too -- cases.json is byte-compared"
    )


def test_case_rejects_a_misspelled_guard_rather_than_dropping_it():
    """A typo used to be swallowed in silence.

    `_case` took `**extra: Any` and filtered it by string key, so
    `requires_row=[...]` -- singular -- was discarded without a word. The
    output is a checked-in fixture, so the loss surfaced much later as a case
    that passes vacuously, which is the exact failure `requires_rows` exists
    to prevent.
    """
    with pytest.raises(TypeError):
        _case(
            "case-a",
            "artist_song",
            "a note",
            {"artist": "Stereolab"},
            requires_row=[["Stereolab", "Peng!"]],  # type: ignore[call-arg]
        )
