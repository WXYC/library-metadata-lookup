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


async def _run_case(case: Any, library_path: Path, discogs_fixture: dict[str, Any]) -> Any:
    """Drive one case through the real app, the same way the test tier does."""
    import os

    from httpx import ASGITransport, AsyncClient

    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from library.db import LibraryDB, clear_library_caches
    from main import app
    from tests.e2e.golden import corpus

    # Same pins, same order, as `tests/e2e/golden/conftest.py`. If these two
    # ever diverge the tool records a verdict the test tier cannot reproduce,
    # so the pin set lives in `corpus.pinned_environment` and both call it.
    previous_env = dict(os.environ)
    os.environ.update(corpus.pinned_environment(case.settings))
    get_settings.cache_clear()
    clear_library_caches()
    database = LibraryDB(library_path)
    await database.connect()
    app.dependency_overrides[get_library_db] = lambda: database
    app.dependency_overrides[get_discogs_service] = lambda: corpus.FakeDiscogsService(
        discogs_fixture
    )
    app.dependency_overrides[get_posthog_client] = lambda: None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://golden"
        ) as client:
            response = await client.post("/api/v1/lookup", json=case.request_body())
        response.raise_for_status()
        return corpus.verdict_from_payload(response.json())
    finally:
        app.dependency_overrides.clear()
        await database.close()
        os.environ.clear()
        os.environ.update(previous_env)
        get_settings.cache_clear()
        clear_library_caches()


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
        discogs_fixture = corpus.load_discogs_fixture()

        changed: list[tuple[Any, Any]] = []
        frozen_drift: list[tuple[Any, Any]] = []
        for case in cases:
            actual = await _run_case(case, library_path, discogs_fixture)
            recorded = by_id[case.id].get("expect")
            previous = corpus.Verdict.from_json(recorded) if recorded is not None else None
            if previous == actual:
                continue
            if case.frozen and previous is not None:
                frozen_drift.append((case, actual))
            else:
                changed.append((case, actual))

    for case, actual in changed:
        recorded = by_id[case.id].get("expect")
        previous = corpus.Verdict.from_json(recorded) if recorded is not None else None
        logger.info("%s (%s)", case.id, case.shape)
        for line in _describe("was", previous) + _describe("now", actual):
            logger.info("%s", line)

    if frozen_drift:
        logger.error("")
        logger.error("%d FROZEN case(s) changed. Nothing was written.", len(frozen_drift))
        for case, actual in frozen_drift:
            logger.error("%s -- pins %s", case.id, case.issue)
            logger.error("  %s", case.note)
            recorded = by_id[case.id].get("expect")
            for line in _describe("recorded", corpus.Verdict.from_json(recorded)) + _describe(
                "actual", actual
            ):
                logger.error("%s", line)
        logger.error(
            "A frozen case pins a failure that already reached production once. If the new "
            "behavior is genuinely correct, edit that case in tests/e2e/golden/cases.json by "
            "hand and say why in the commit message."
        )
        return 1

    if not changed:
        logger.info("no expectations moved")
        if not args.format_only:
            return 0

    if args.dry_run:
        logger.info("--dry-run: %d expectation(s) would be rewritten", len(changed))
        return 0

    for case, actual in changed:
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
