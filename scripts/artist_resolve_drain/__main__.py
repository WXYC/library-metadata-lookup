"""CLI entrypoint for the bulk artist-resolve drain (LML#759 PR D).

Runbook (LML#759 design; BS#1614 gate):

    # 1. dry drain against prod off-peak (outside the 06:00 UTC backfill window)
    LML_API_KEY=... LML_BASE_URL=https://<prod-lml> \
        uv run python -m scripts.artist_resolve_drain names.txt \
        --out drain.jsonl --report report.md

    # 2. a human eyeballs report.md's spot-check table for wrong mints, THEN
    # 3. authorize the live drain (mints entity.identity — durable, COALESCE
    #    never-clobber, so a wrong mint is un-self-correcting)
    ... --live --out drain.jsonl --report report-live.md

The drain always runs against the **prod** endpoint: it is the only place all
Discogs traffic coordinates through a single 50/min limiter + LML#755 breaker, so
a drain there can't 429 live lookups the way a staging drain (shared token,
uncoordinated limiter) would.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from argparse import ArgumentParser
from pathlib import Path

import httpx

from scripts._lib.runtime import set_up_script_runtime
from scripts._lib.signals import ShutdownFlag
from scripts.artist_resolve_drain.drain import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_MAX_RETRIES,
    PAGE_SIZE,
    make_post_batch,
    parse_names_file,
    run_drain,
)
from scripts.artist_resolve_drain.report import (
    build_report,
    format_report_markdown,
    sample_spot_check,
)

logger = logging.getLogger("artist_resolve_drain")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = ArgumentParser(
        prog="python -m scripts.artist_resolve_drain",
        description="Drain a bare-name set through prod POST /api/v1/artists/resolve/bulk.",
    )
    parser.add_argument("names_file", help="Names handoff file (JSON array or newline-delimited).")
    parser.add_argument(
        "--out",
        default="artist_resolve_drain.jsonl",
        help="JSONL verdict log (append + resume). Default: artist_resolve_drain.jsonl",
    )
    parser.add_argument("--report", help="Write the markdown report to this path.")
    parser.add_argument(
        "--base-url",
        default=None,
        help="LML base URL. Defaults to $LML_BASE_URL, then $PRODUCTION_URL.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="LML API key (bearer). Defaults to $LML_API_KEY.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Mint entity.identity rows. Default is a dry run (no write-back).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=PAGE_SIZE,
        help=f"Names per request (endpoint cap {PAGE_SIZE}). Default: {PAGE_SIZE}",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"escalation retries. Default: {DEFAULT_MAX_RETRIES}",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=float(DEFAULT_COOLDOWN_SECONDS),
        help=f"Seconds between retry rounds. Default: {DEFAULT_COOLDOWN_SECONDS}",
    )
    parser.add_argument("--spot-check", type=int, default=20, help="Spot-check sample size.")
    parser.add_argument("--seed", type=int, default=0, help="Spot-check RNG seed (reproducible).")
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-request timeout (s). A full escalating page can take ~30s. Default: 120",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


async def _run(
    args: argparse.Namespace,
    base_url: str,
    api_key: str,
    dry_run: bool,
    shutdown: ShutdownFlag,
) -> None:
    names = parse_names_file(Path(args.names_file).read_text(encoding="utf-8"))
    logger.info("loaded %d unique name(s) from %s", len(names), args.names_file)
    if not names:
        logger.warning("no names to drain; nothing to do")
        return

    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout)) as client:
        post_batch = make_post_batch(client, base_url, api_key)
        records = await run_drain(
            all_names=names,
            dry_run=dry_run,
            out_path=args.out,
            post_batch=post_batch,
            page_size=args.page_size,
            max_retries=args.max_retries,
            cooldown=args.cooldown,
            shutdown=shutdown,
        )

    report = build_report(records, names, max_attempts=args.max_retries + 1)
    rows = sample_spot_check(records, seed=args.seed, k=args.spot_check)
    markdown = format_report_markdown(report, rows, dry_run=dry_run)
    if args.report:
        Path(args.report).write_text(markdown + "\n", encoding="utf-8")
        logger.info("wrote report to %s", args.report)
    print("\n" + markdown)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    shutdown = set_up_script_runtime(
        logger=logger,
        verbose=args.verbose,
        shutdown_unit="batch",
        include_logger_name=False,
    )

    base_url = args.base_url or os.environ.get("LML_BASE_URL") or os.environ.get("PRODUCTION_URL")
    if not base_url:
        logger.error("no base URL: pass --base-url or set $LML_BASE_URL / $PRODUCTION_URL")
        sys.exit(2)
    api_key = args.api_key or os.environ.get("LML_API_KEY")
    if not api_key:
        logger.error("no API key: pass --api-key or set $LML_API_KEY")
        sys.exit(2)
    if args.page_size < 1 or args.page_size > PAGE_SIZE:
        logger.error("--page-size must be 1..%d (endpoint cap)", PAGE_SIZE)
        sys.exit(2)

    dry_run = not args.live
    if dry_run:
        logger.info("DRY RUN — no entity.identity write-back (pass --live to mint)")
    else:
        logger.warning(
            "LIVE DRAIN against %s — mints DURABLE entity.identity rows (COALESCE "
            "never-clobber). Ensure the dry-run spot-check was human-reviewed.",
            base_url,
        )

    asyncio.run(_run(args, base_url, api_key, dry_run, shutdown))


if __name__ == "__main__":
    main()
