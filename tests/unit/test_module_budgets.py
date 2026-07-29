"""Module-budget guardrail for the ``lookup/`` package (LML#731).

The orchestrator decomposition (LML#722) took ``lookup/orchestrator.py`` from a
4,485-line god module to a documented spine plus one module per concern. This
test is the regrowth guardrail: every ``lookup/**/*.py`` file carries an
explicit per-file line ceiling, and any new file under ``lookup/`` must be
given one deliberately before it can ship.

Calibration (2026-07-06, post-LML#731 respine): each ceiling is the smallest
multiple of 50 at or above 1.3x the file's measured size (minimum 50) — about
30% headroom for ordinary maintenance, not for a new concern. When a file hits
its ceiling, the answer is to extract a module, not to append; raising a
ceiling is a deliberate, reviewed decision, never a drive-by edit.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOOKUP_ROOT = REPO_ROOT / "lookup"

#: Repo-relative POSIX path -> maximum physical line count (``wc -l``).
MODULE_BUDGETS: dict[str, int] = {
    "lookup/__init__.py": 50,
    # LML#930 PR2: shed policy only (predicate + env resolvers + telemetry
    # projection + orchestration). 1.3x its ~152-line measured size -> 200.
    "lookup/admission.py": 200,
    "lookup/artist_resolution.py": 550,
    "lookup/artwork.py": 500,
    "lookup/caller_reason.py": 100,
    "lookup/candidate_memo.py": 150,
    "lookup/concurrency.py": 200,
    "lookup/endpoint_family.py": 100,
    "lookup/enrichment/__init__.py": 350,
    "lookup/enrichment/background.py": 100,
    "lookup/enrichment/context.py": 150,
    "lookup/enrichment/item.py": 850,
    "lookup/enrichment/top1.py": 100,
    "lookup/external_search.py": 350,
    "lookup/matching.py": 550,
    "lookup/models.py": 150,
    # Recalibrated 2026-07-29 (LML#750): extracting step 3b's validation cascade
    # and step 7's external-cache dispatch policy out of the spine dropped it
    # from 1291 to ~1225 lines. The spine is the known regrowth attractor (its
    # own history — #944, #750 — is repeated near-miss growth), so this ceiling
    # is deliberately NOT re-derived from the 1.3x formula (which would reopen
    # ~370 lines of headroom, i.e. room for another buried concern to
    # accumulate unnoticed). 1250 keeps just enough slack for an ordinary small
    # edit while forcing the next real concern straight to extraction.
    "lookup/orchestrator.py": 1250,
    "lookup/release_resolution.py": 550,
    # Recalibrated 2026-07-27 (LML#944): unrelated changes since the 2026-07-06
    # calibration had already carried this file to exactly its old 950-line
    # ceiling (zero headroom) before this ticket's two mandated Sentry-tag call
    # sites landed. The new endpoint-family concern itself was extracted to
    # `lookup/endpoint_family.py` per this module's own policy; the residual
    # growth here (an import plus the two irreducible call sites) is
    # recalibrated with the same formula as every other entry: smallest
    # multiple of 50 at or above 1.3x the post-change 959-line size.
    "lookup/router.py": 1250,
    "lookup/rowless.py": 450,
    "lookup/server_timing_legs.py": 100,
    "lookup/spine_deadline.py": 250,
    "lookup/strategies/__init__.py": 150,
    "lookup/strategies/artist_plus_album.py": 300,
    "lookup/strategies/library_miss.py": 200,
    "lookup/strategies/song_as_artist.py": 250,
    "lookup/strategies/song_as_track.py": 150,
    "lookup/strategies/swapped_interpretation.py": 300,
    "lookup/strategies/track_on_compilation.py": 850,
    "lookup/strategies/track_release_matching.py": 550,
    "lookup/strategies/va_rescue.py": 300,
    "lookup/streaming_url_postprocess.py": 750,
    "lookup/tail_deadline.py": 150,
    "lookup/timeouts.py": 100,
    # Recalibrated 2026-07-29 (LML#750): the step-3b validation cascade
    # (``apply_track_validation_cascade``) and the LML#717 song-as-album-title
    # promotion moved in from the spine, carrying this file from 243 to ~395
    # lines. Smallest multiple of 50 at or above 1.3x the post-change size.
    "lookup/validation.py": 550,
}


def _lookup_files() -> list[str]:
    """Every ``.py`` file under ``lookup/``, as a repo-relative POSIX path."""
    # Dot-prefixed components (Emacs lock files and the like) can never be valid
    # modules, so they are skipped; untracked non-dot scratch files deliberately
    # keep failing (pre-push signal).
    return sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in LOOKUP_ROOT.rglob("*.py")
        if not any(part.startswith(".") for part in p.relative_to(LOOKUP_ROOT).parts)
    )


@pytest.mark.parametrize("rel_path", _lookup_files())
def test_lookup_module_within_budget(rel_path: str) -> None:
    """Each lookup/ module stays within its declared line ceiling."""
    ceiling = MODULE_BUDGETS.get(rel_path)
    if ceiling is None:
        pytest.fail(
            f"{rel_path} has no entry in MODULE_BUDGETS ({__file__}). "
            "Add a ceiling sized per the calibration convention in this module's "
            "docstring — the guardrail cannot be dodged by putting growth in a "
            "new, unbudgeted file."
        )
    actual = (REPO_ROOT / rel_path).read_bytes().count(b"\n")
    assert actual <= ceiling, (
        f"{rel_path} is {actual} lines, over its {ceiling}-line budget. "
        "Extract the growing concern into its own lookup/ module — see this "
        "module's docstring for the ceiling policy and the key-files map in "
        "docs/architecture.md for where each concern lives."
    )


def test_no_stale_budget_entries() -> None:
    """MODULE_BUDGETS lists only files that exist (keeps the table honest)."""
    stale = sorted(set(MODULE_BUDGETS) - set(_lookup_files()))
    assert not stale, (
        f"MODULE_BUDGETS has entries for files that no longer exist: {stale}. "
        "Remove (or rename) them so the budget table stays an accurate map of "
        "the lookup/ package."
    )
