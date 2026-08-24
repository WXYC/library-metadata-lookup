"""Re-record the LML#1233 golden corpus expectations, deliberately (LML#1233).

A corpus whose expectations are re-recorded casually detects nothing. This
tool exists so that re-recording them is a *separate, visible act* rather than
something a test run does on its own, and it is built around three refusals:

1. **It refuses frozen cases.** A frozen case pins a failure that already
   reached production once. If one of those moved, the tool prints it and
   exits non-zero without writing. Accepting the new behavior means hand-editing
   that case and saying why -- which is exactly the friction that stops a
   regression from being laundered back into the baseline as an "improvement".
2. **It never runs implicitly.** No pytest plugin, no `--rebaseline` flag on
   the test suite, no environment variable the CI job could pick up. The only
   way to move a baseline is to run this on purpose.
3. **It prints every change before writing.** The diff is the review artifact;
   the commit that carries it should carry the reason too.

```bash
uv run python -m scripts.rebaseline_golden_corpus            # record + report
uv run python -m scripts.rebaseline_golden_corpus --dry-run  # report only
uv run python -m scripts.rebaseline_golden_corpus --format-only
```

`--format-only` rewrites `cases.json` through the canonical writer without
running anything, for when a hand-edit left the file's formatting off.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("rebaseline_golden_corpus")


async def _run_case(case: Any, library_path: Path, discogs_universe: Any) -> Any:
    """Drive one case through the real app, the same way the test tier does."""
    import os

    from config.settings import get_settings
    from library.db import LibraryDB, clear_library_caches
    from tests.e2e.golden import corpus

    # Same pins AND same wiring as `tests/e2e/golden/conftest.py`: the pin set
    # is `corpus.pinned_environment` and the app wiring is
    # `corpus.golden_app_client`, both called from here and from the fixtures.
    # If the two ever diverged the tool would record a verdict the test tier
    # cannot reproduce, and a comment is not a mechanism.
    previous_env = dict(os.environ)
    os.environ.update(corpus.pinned_environment(case.settings))
    get_settings.cache_clear()
    clear_library_caches()
    database = LibraryDB(library_path)
    await database.connect()
    fake = corpus.FakeDiscogsService(discogs_universe)
    try:
        async with corpus.golden_app_client(database, fake) as client:
            response = await client.post("/api/v1/lookup", json=case.request_body())
        response.raise_for_status()
        _assert_routes_served(case, fake)
        return corpus.verdict_from_payload(response.json())
    finally:
        await database.close()
        os.environ.clear()
        os.environ.update(previous_env)
        get_settings.cache_clear()
        clear_library_caches()


def _assert_routes_served(case: Any, fake: Any) -> None:
    """Refuse to record a verdict produced under a drifted fixture route.

    The test tier runs these two checks before it compares a verdict
    (`tests/e2e/golden/test_golden_corpus.py`); the recorder did not, so a case
    whose route key had drifted would be measuring an empty-upstream fallback,
    and this tool would write that down as the new baseline. The drift then
    surfaced on the *next* test run -- after the commit.
    """
    missing = sorted(set(case.requires_routes) - fake.served_keys)
    if missing:
        raise RuntimeError(
            f"{case.id} requires fixture route(s) {missing} to have served a candidate, "
            f"and they did not (served: {sorted(fake.served_keys)}). The route drifted; "
            "refusing to record a verdict for a case that is no longer exercising its path."
        )
    if case.requires_discogs and not fake.served_candidates:
        raise RuntimeError(
            f"{case.id} declares requires_discogs but no Discogs search returned a "
            "candidate. A route key drifted; refusing to record a verdict measured "
            "against an empty upstream."
        )


def classify_case_result(case: Any, previous: Any, actual: Any) -> str:
    """Decide what a freshly-observed verdict means for one case.

    A pure function of the three values a case's run produces, so it is unit-
    testable without spinning up the FastAPI app -- and so the frozen refusal
    is one rule instead of a condition inlined in the driver loop.

    Returns one of:

    - `"unchanged"` -- `previous == actual`; nothing to do.
    - `"frozen_drift"` -- `case.frozen` and the verdict moved, REGARDLESS of
      whether `previous` is `None`. A frozen case with `expect: null` is a
      `build_golden_corpus.py` skeleton not yet hand-authored -- not an
      ordinary new case -- and must refuse exactly like a frozen case whose
      recorded verdict actually moved. Gating on `previous is not None` here
      was the LML#1233 review's laundering path: null a frozen case's expect,
      re-run, and the new behavior gets written with no refusal at all.
    - `"changed"` -- everything else: a non-frozen case, moved or newly
      recorded.
    """
    if previous == actual:
        return "unchanged"
    if case.frozen:
        return "frozen_drift"
    return "changed"


def _describe(label: str, verdict: Any) -> list[str]:
    if verdict is None:
        return [f"  {label}: (never recorded)"]
    lines = [
        f"  {label}: {verdict.miss_kind}"
        f" song_not_found={verdict.song_not_found}"
        f" found_on_compilation={verdict.found_on_compilation}"
    ]
    lines.extend(f"      {identity}" for identity in verdict.results or ["(no results)"])
    return lines


async def _main_async(args: argparse.Namespace) -> int:
    from tests.e2e.golden import corpus

    raw_cases = json.loads(corpus.CASES_PATH.read_text(encoding="utf-8"))
    by_id = {raw["id"]: raw for raw in raw_cases}
    cases = corpus.load_cases()

    with tempfile.TemporaryDirectory() as tmp_dir:
        library_path = corpus.build_library_db(
            Path(tmp_dir) / "library.db", corpus.load_library_rows()
        )
        discogs_universe = corpus.load_discogs_universe()

        # Triples, not pairs: `previous` is derived once, where it is first
        # needed, and travels with the case. It used to be re-derived in each
        # of the two report loops below -- three copies of one two-line
        # derivation that had to agree, and the third had quietly dropped the
        # `is not None` guard the other two carried.
        changed: list[tuple[Any, Any, Any]] = []
        frozen_drift: list[tuple[Any, Any, Any]] = []
        for case in cases:
            actual = await _run_case(case, library_path, discogs_universe)
            recorded = by_id[case.id].get("expect")
            previous = corpus.Verdict.from_json(recorded) if recorded is not None else None
            outcome = classify_case_result(case, previous, actual)
            if outcome == "frozen_drift":
                frozen_drift.append((case, previous, actual))
            elif outcome == "changed":
                changed.append((case, previous, actual))

    for case, previous, actual in changed:
        logger.info("%s (%s)", case.id, case.shape)
        for line in _describe("was", previous) + _describe("now", actual):
            logger.info("%s", line)

    if frozen_drift:
        logger.error("")
        logger.error("%d FROZEN case(s) changed. Nothing was written.", len(frozen_drift))
        for case, previous, actual in frozen_drift:
            logger.error("%s -- pins %s", case.id, case.issue)
            logger.error("  %s", case.note)
            for line in _describe("recorded", previous) + _describe("actual", actual):
                logger.error("%s", line)
        logger.error(
            "A frozen case pins a failure that already reached production once. If the new "
            "behavior is genuinely correct, edit that case in tests/e2e/golden/cases.json by "
            "hand and say why in the commit message."
        )
        return 1

    if not changed:
        logger.info("no expectations moved")
        return 0

    if args.dry_run:
        logger.info("--dry-run: %d expectation(s) would be rewritten", len(changed))
        return 0

    for case, _previous, actual in changed:
        by_id[case.id]["expect"] = actual.to_json()
    corpus.CASES_PATH.write_text(corpus.dump_cases(raw_cases), encoding="utf-8")
    logger.info("rewrote %d expectation(s) in %s", len(changed), corpus.CASES_PATH)
    logger.info(
        "Commit tests/e2e/golden/cases.json on its own, with the reason each verdict moved."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change; write nothing."
    )
    parser.add_argument(
        "--format-only",
        action="store_true",
        help="Rewrite cases.json through the canonical writer without running the corpus.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # `--format-only` is handled entirely here and returns before `_main_async`
    # ever runs -- so `args.format_only` is always False for the rest of this
    # function and for `_main_async` (LML#1233 review flagged a dead check
    # there that read as though it were reachable; removed rather than kept
    # composable, since the two modes don't share any state worth threading
    # through one run: format-only never runs a case, and a real rebaseline
    # always writes through the same canonical `corpus.dump_cases` writer, so
    # there is nothing left for a combined flag to additionally fix).
    if args.format_only:
        from tests.e2e.golden import corpus

        raw = json.loads(corpus.CASES_PATH.read_text(encoding="utf-8"))
        corpus.CASES_PATH.write_text(corpus.dump_cases(raw), encoding="utf-8")
        logger.info("reformatted %s", corpus.CASES_PATH)
        return 0

    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        logger.warning("interrupted; nothing written")
        return 130


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
