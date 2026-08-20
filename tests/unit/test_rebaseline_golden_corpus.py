"""Unit tests for `scripts/rebaseline_golden_corpus.py`'s pure classification logic.

Deliberately narrow: `_main_async` drives the real FastAPI app end-to-end, but
the decision of what to DO with a freshly-observed verdict --
leave it, refuse it, or write it -- is a pure function of three values and
belongs under a fast, dependency-free unit test rather than only being
exercised through the full e2e path.
"""

from __future__ import annotations

from scripts.rebaseline_golden_corpus import classify_case_result
from tests.e2e.golden.corpus import Case, Verdict

_HIT = Verdict(
    miss_kind="hit", song_not_found=False, found_on_compilation=False, results=("#1 A — B",)
)
_OTHER_HIT = Verdict(
    miss_kind="hit", song_not_found=False, found_on_compilation=False, results=("#2 C — D",)
)


def _case(*, frozen: bool) -> Case:
    return Case(
        id="x",
        shape="artist_album",
        note="note",
        query={"artist": "A", "album": "B"},
        expect=None,
        frozen=frozen,
    )


def test_unchanged_verdict_is_left_alone():
    assert classify_case_result(_case(frozen=False), _HIT, _HIT) == "unchanged"
    assert classify_case_result(_case(frozen=True), _HIT, _HIT) == "unchanged"


def test_non_frozen_case_with_a_moved_verdict_is_changed():
    assert classify_case_result(_case(frozen=False), _HIT, _OTHER_HIT) == "changed"


def test_frozen_case_with_a_moved_verdict_is_refused():
    assert classify_case_result(_case(frozen=True), _HIT, _OTHER_HIT) == "frozen_drift"


def test_frozen_case_with_a_null_expectation_is_refused_not_laundered():
    """A frozen case with `expect: null` (a fresh `build_golden_corpus.py`
    skeleton not yet hand-authored) must not be treated as an ordinary new
    case. Previously `previous is not None` gated the frozen refusal, so a
    null expectation on a frozen case slipped into `changed` and got written
    with whatever behavior happened to run today -- exactly the laundering
    path `frozen_cases()`'s docstring says is closed (LML#1233 review): null a
    frozen case's expect, re-run, get new behavior recorded with no refusal.
    """
    assert classify_case_result(_case(frozen=True), None, _HIT) == "frozen_drift"


def test_non_frozen_case_with_a_null_expectation_is_changed():
    """The ordinary new-case path must still work: build_golden_corpus.py
    emits `expect: null` for every genuinely new (non-frozen) case, and
    rebaseline is exactly what is supposed to fill that in."""
    assert classify_case_result(_case(frozen=False), None, _HIT) == "changed"
