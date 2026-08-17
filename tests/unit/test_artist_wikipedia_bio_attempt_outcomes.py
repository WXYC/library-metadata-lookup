"""Ownership net for the attempt-table ``outcome`` vocabulary (LML#1192 cross-PR review, round 7).

``lml_cache.artist_wikipedia_bio_attempt.outcome`` is a free-text column
written from five call sites across two modules (the background miss-warm
and the offline drain), and through round 6 every writer spelled its
outcome as a bare string literal — so the vocabulary's only inventory was
prose, and it was already stale: ``entity/artist_wikipedia_bio_attempt.py``'s
docstring enumerated three outcomes while round 6 pass 3 A1 added a fourth
(``"truncated"``) in a different PR of the same stack. A typo'd or novel
outcome would be silently accepted by the upsert and silently
un-classified by the drain's ``_FAILURE_ISH_OUTCOMES`` streak-abort.

These tests make the owning entity module the vocabulary's single source:
named constants plus a ``KNOWN_ATTEMPT_OUTCOMES`` roster, a membership
check on the drain's failure-ish classifier, and a source-level check that
the two writer modules never pass a bare ``outcome="..."`` literal to
:func:`record_artist_wikipedia_bio_attempt`. The drain's per-candidate
REPORT labels (``"negative"``, ``"declined"``, ``"refreshed"``,
``"positive"``, ``"repick_kept_existing"``) are a different domain — they
tally session outcomes, most of which DO write content rows — and stay
script-local on purpose; only the durably-recorded attempt outcomes need a
cross-module owner.
"""

from __future__ import annotations

import re
from pathlib import Path

from entity.artist_wikipedia_bio_attempt import (
    KNOWN_ATTEMPT_OUTCOMES,
    OUTCOME_DECLINED,
    OUTCOME_FETCH_ERROR,
    OUTCOME_TRUNCATED,
    OUTCOME_UNEXPECTED_ERROR,
    OUTCOME_UNRESOLVABLE,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The two modules that call record_artist_wikipedia_bio_attempt. A new
# writer module should be added here (the source check below is
# deliberately writer-scoped, not repo-wide, so it can't be satisfied
# vacuously by files that never write attempts).
_WRITER_FILES = (
    _REPO_ROOT / "lookup" / "enrichment" / "wikipedia_warm.py",
    _REPO_ROOT / "scripts" / "warm_wikipedia_bios.py",
)


class TestOutcomeVocabularyOwnership:
    def test_constants_spellings_are_the_shipped_column_values(self):
        # Pinned to the exact strings rounds 4-6 already wrote: renaming a
        # constant's VALUE is a data migration (existing rows keep the old
        # spelling), not a refactor, and must fail here until decided.
        assert OUTCOME_FETCH_ERROR == "fetch_error"
        assert OUTCOME_UNRESOLVABLE == "unresolvable"
        assert OUTCOME_UNEXPECTED_ERROR == "unexpected_error"
        assert OUTCOME_TRUNCATED == "truncated"
        assert OUTCOME_DECLINED == "declined"

    def test_roster_is_total_over_the_constants(self):
        assert KNOWN_ATTEMPT_OUTCOMES == frozenset(
            {
                OUTCOME_FETCH_ERROR,
                OUTCOME_UNRESOLVABLE,
                OUTCOME_UNEXPECTED_ERROR,
                OUTCOME_TRUNCATED,
                OUTCOME_DECLINED,
            }
        )

    def test_drain_failure_classifier_spans_the_attempt_outcomes_it_names(self):
        # The streak-abort classifier mixes both domains: every
        # attempt-table outcome it classifies must be a KNOWN one (a typo
        # here silently exempts that outcome from the abort guard), and its
        # only non-attempt member is the script-local repick report label.
        from scripts.warm_wikipedia_bios import _FAILURE_ISH_OUTCOMES

        assert _FAILURE_ISH_OUTCOMES & KNOWN_ATTEMPT_OUTCOMES == frozenset(
            {OUTCOME_FETCH_ERROR, OUTCOME_UNEXPECTED_ERROR, OUTCOME_UNRESOLVABLE}
        )
        assert _FAILURE_ISH_OUTCOMES - KNOWN_ATTEMPT_OUTCOMES == frozenset({"repick_kept_existing"})

    def test_writers_never_pass_a_bare_outcome_literal(self):
        # Source-level: at every record_artist_wikipedia_bio_attempt call
        # site in the writer modules, the outcome kwarg must be a named
        # constant, never a string literal -- the literal form is exactly
        # how the vocabulary drifted across review rounds.
        offenders: list[str] = []
        for path in _WRITER_FILES:
            src = path.read_text(encoding="utf-8")
            for match in re.finditer(r"record_artist_wikipedia_bio_attempt\(", src):
                # Scan only the call's own argument list (up to its closing
                # paren), not trailing statements -- the drain assigns its
                # script-local REPORT labels right after some calls, and
                # those literals are legitimately not constants.
                close = src.index(")", match.end())
                window = src[match.start() : close]
                kwarg = re.search(r"outcome\s*=\s*(\"|')", window)
                if kwarg is not None:
                    line = src.count("\n", 0, match.start()) + 1
                    offenders.append(f"{path.name}:{line}")
        assert not offenders, (
            f'bare outcome="..." literal at {offenders} -- import the OUTCOME_* constant '
            "from entity.artist_wikipedia_bio_attempt instead"
        )
