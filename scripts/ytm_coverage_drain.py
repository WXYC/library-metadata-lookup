#!/usr/bin/env python3
"""YouTube Music coverage drain — dry-run harness (LML#1056).

Resolves verified ``music.youtube.com/browse/<browseId>`` album links for a set
of canonical ``(artist, title)`` candidates via :class:`YouTubeMusicClient`
(80/80 floor, shared production matcher) and emits a coverage report. It is the
first acceptance criterion of #1056 ("dry-run report: candidate count, projected
hit rate, sample matches") and is deliberately **read-only**: the persistence
step is gated behind :func:`execute_write`, which raises until the write-path
design fork (#1056 / #1052) is settled.

Candidate source
----------------
The real drain drives off ``entity.release_identity``. That table carries only
per-source external IDs (``discogs_release_id``, ``discogs_master_id``, …) — it
has **no** ``artist``/``title`` columns — so the canonical ``(artist, title)`` is
obtained by resolving ``discogs_release_id`` against the discogs-cache release
table, exactly as the #525 cache warmer's ``_refresh_discogs_release`` does. That
join runs in the discogs-cache PostgreSQL and needs prod credentials plus a human
go-ahead, so it is out of scope for this dry-run harness. The operator supplies
already-resolved rows and this module stays schema-agnostic via
:func:`load_candidates_from_rows`; :func:`load_candidates_from_csv` is the
credential-free path for a local sample.

Usage (dry-run only)
--------------------
    uv run python -m scripts.ytm_coverage_drain --sample-csv resolved_names.csv \
        --limit 200 --concurrency 4 --report-json /tmp/ytm_drain_report.json

``--execute`` exists but is a tripwire: it raises :class:`WritePathNotResolvedError`
so no prod write can happen before the fork is resolved. Persisting is the user's
action, off-peak, fill-only, one bulk LML consumer at a time.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from clients.streaming.youtube_music import YouTubeMusicClient
from streaming.models import SourceMatch

log = logging.getLogger("ytm_coverage_drain")

DEFAULT_CONCURRENCY = 4
DEFAULT_SAMPLE_SIZE = 15
DEFAULT_RATE = 2.0
DEFAULT_SEARCH_LIMIT = 5


class WritePathNotResolvedError(RuntimeError):
    """Raised when ``--execute`` / :func:`execute_write` is used before the
    #1056 / #1052 write-path design fork (warmer leg vs. direct cache upsert)
    is resolved. The dry-run path never persists, so this is a hard tripwire."""


@dataclass(frozen=True)
class Candidate:
    """A canonical album to look up on YouTube Music."""

    artist: str
    title: str
    discogs_release_id: int | None = None
    identity_id: int | None = None


@dataclass(frozen=True)
class DrainOutcome:
    """A candidate paired with its resolved match (``None`` = no 80/80 clear)."""

    candidate: Candidate
    match: SourceMatch | None


def load_candidates_from_rows(
    rows: Iterable[dict[str, Any]],
    *,
    artist_key: str,
    title_key: str,
    id_key: str | None = None,
    identity_key: str | None = None,
) -> list[Candidate]:
    """Adapt already-resolved rows into :class:`Candidate` objects.

    Schema-agnostic on purpose: the operator's discogs-cache join can project
    the canonical artist/title under any column names. Rows with a blank or
    null artist or title are skipped (they cannot be searched meaningfully).
    """
    out: list[Candidate] = []
    for r in rows:
        artist = str(r.get(artist_key) or "").strip()
        title = str(r.get(title_key) or "").strip()
        if not artist or not title:
            continue
        out.append(
            Candidate(
                artist=artist,
                title=title,
                discogs_release_id=r.get(id_key) if id_key else None,
                identity_id=r.get(identity_key) if identity_key else None,
            )
        )
    return out


def load_candidates_from_csv(path: str) -> list[Candidate]:
    """Load candidates from a CSV with an ``artist,title`` header (dry-run path)."""
    with open(path, newline="", encoding="utf-8") as f:
        return load_candidates_from_rows(csv.DictReader(f), artist_key="artist", title_key="title")


async def resolve_candidates(
    client: YouTubeMusicClient,
    candidates: Sequence[Candidate],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> list[DrainOutcome]:
    """Resolve every candidate concurrently (bounded), preserving input order."""
    sem = asyncio.Semaphore(concurrency)

    async def _one(c: Candidate) -> DrainOutcome:
        async with sem:
            match = await client.find_album_match(c.artist, c.title)
        return DrainOutcome(candidate=c, match=match)

    return list(await asyncio.gather(*(_one(c) for c in candidates)))


def summarize(
    outcomes: Sequence[DrainOutcome], *, sample_size: int = DEFAULT_SAMPLE_SIZE
) -> dict[str, Any]:
    """Aggregate outcomes into a JSON-serializable coverage report."""
    resolved = [o for o in outcomes if o.match is not None]
    misses = [o for o in outcomes if o.match is None]
    n = len(outcomes)
    return {
        "candidates": n,
        "resolved": len(resolved),
        "misses": len(misses),
        "hit_rate": (len(resolved) / n) if n else 0.0,
        "sample_matches": [
            {
                "artist": o.candidate.artist,
                "title": o.candidate.title,
                "url": o.match.url,  # type: ignore[union-attr]  # resolved => match is not None
                "confidence": o.match.confidence,  # type: ignore[union-attr]
            }
            for o in resolved[:sample_size]
        ],
        "sample_misses": [
            {"artist": o.candidate.artist, "title": o.candidate.title} for o in misses[:sample_size]
        ],
    }


def execute_write(matched_outcomes: Sequence[DrainOutcome]) -> None:
    """Persist verified links (fill-only) — GATED, not yet implemented.

    The write path is intentionally unbuilt: #1056 and #1052 share an open
    design fork over *how* to persist (wire a ``youtube_music`` leg into the
    ``refresh-for-identities`` dispatcher, the approach rejected for the sibling
    legs in #548/#549 — versus a direct fill-only upsert into
    ``lml_cache.album_streaming_url_cache``). Until that is resolved with the
    user, invoking the write path raises rather than guessing.
    """
    raise WritePathNotResolvedError(
        "YTM drain write path is gated on the #1056/#1052 design fork "
        "(refresh-for-identities leg vs. direct streaming_url_cache upsert). "
        f"Refusing to persist {len(matched_outcomes)} matches. Dry-run writes nothing."
    )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    candidates = load_candidates_from_csv(args.sample_csv)
    if args.limit:
        candidates = candidates[: args.limit]
    log.info(
        "resolving %d candidates against YouTube Music (concurrency=%d, rate=%.1f/s)",
        len(candidates),
        args.concurrency,
        args.rate,
    )
    client = YouTubeMusicClient(rate_limit=(args.rate, 1), limit=args.search_limit)
    outcomes = await resolve_candidates(client, candidates, concurrency=args.concurrency)
    report = summarize(outcomes, sample_size=args.sample_size)
    log.info(
        "resolved %d/%d (%.1f%%)",
        report["resolved"],
        report["candidates"],
        100 * report["hit_rate"],
    )
    if args.execute:
        # Tripwire: raises until the write-path fork is resolved.
        execute_write([o for o in outcomes if o.match is not None])
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        log.info("wrote report to %s", args.report_json)
    return report


def main(argv: Sequence[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description="YouTube Music coverage drain — dry-run harness (LML#1056)."
    )
    parser.add_argument(
        "--sample-csv",
        required=True,
        help="CSV with an 'artist,title' header holding resolved canonical names.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Cap candidates (0 = all).")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--rate", type=float, default=DEFAULT_RATE, help="Max YouTube Music searches per second."
    )
    parser.add_argument(
        "--search-limit", type=int, default=DEFAULT_SEARCH_LIMIT, help="Candidates per YTM search."
    )
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--report-json", default=None, help="Optional path to write the report.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="(GATED) persist matches — raises until the #1056/#1052 write-path fork is resolved.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    main()
