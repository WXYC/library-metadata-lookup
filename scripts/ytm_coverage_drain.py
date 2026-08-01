#!/usr/bin/env python3
"""YouTube Music coverage drain (LML#1056).

Resolves verified ``music.youtube.com/browse/<browseId>`` album links for a set
of canonical ``(artist, title)`` candidates via :class:`YouTubeMusicClient`
(80/80 floor, shared production matcher), emits a coverage report, and -- under
the explicit ``--execute`` opt-in -- fill-only-persists the matches.

Write path (Option A of the #1056 / #1052 fork)
-----------------------------------------------
Persistence goes to ``streaming_availability.db`` -- LML's authoritative,
top-priority offline link store -- via :meth:`ResultsDB.update_youtube_music_url`
(fill-only). The rest of the chain already exists: ``export_streaming_links.py``
copies ``albums.youtube_music_url`` into ``library.db.streaming_links`` and
``/lookup`` surfaces it, so this drain is the last missing piece, the producer.
The sibling ``lml_cache.album_streaming_url_cache`` (Option B) is reserved for
#1052's runtime/rowless population, which cannot reach this library-keyed store.

Candidate source
----------------
The real drain drives off ``entity.release_identity``. That table carries only
per-source external IDs (``discogs_release_id``, ``discogs_master_id``, …) — it
has **no** ``artist``/``title`` columns — so the canonical ``(artist, title)`` is
obtained by resolving ``discogs_release_id`` against the discogs-cache release
table, exactly as the #525 cache warmer's ``_refresh_discogs_release`` does. That
join runs in the discogs-cache PostgreSQL and needs prod credentials plus a human
go-ahead, so it is out of scope for this harness. The operator supplies
already-resolved rows and this module stays schema-agnostic via
:func:`load_candidates_from_rows`; :func:`load_candidates_from_csv` is the
credential-free path for a local sample. :func:`execute_write` then maps each
match back to its ``albums`` row by normalized ``(artist, title)`` -- the same
key the dedup pipeline wrote -- so there is no normalization drift.

Usage
-----
``ytmusicapi`` lives in the ``drain`` extra (not ``dev``), and ``_run`` always
builds a real client, so the drain must run under ``--extra drain`` or the lazy
``from ytmusicapi import YTMusic`` raises ``ModuleNotFoundError``.

    # dry-run (default): resolve + report, write nothing
    uv run --extra drain python -m scripts.ytm_coverage_drain --sample-csv resolved_names.csv \
        --limit 200 --concurrency 4 --report-json /tmp/ytm_drain_report.json

    # persist (opt-in, fill-only): add --execute + the target DB
    uv run --extra drain python -m scripts.ytm_coverage_drain --sample-csv resolved_names.csv \
        --execute --results-db streaming_availability.db

Persisting is the user's action -- off-peak, fill-only, one bulk LML consumer at
a time -- and republishing to prod is a separate ``POST /admin/upload-streaming-db``
step.
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
from scripts.streaming_availability.results_db import ResultsDB
from streaming.models import SourceMatch

log = logging.getLogger("ytm_coverage_drain")

DEFAULT_CONCURRENCY = 4
DEFAULT_SAMPLE_SIZE = 15
DEFAULT_RATE = 2.0
DEFAULT_SEARCH_LIMIT = 5
DEFAULT_RESULTS_DB_PATH = "streaming_availability.db"


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
            try:
                match = await client.find_album_match(c.artist, c.title)
            except Exception:
                # One malformed search must not abort the whole run. The shared
                # matcher re-raises when every result row fails extraction
                # (clients/streaming/matching.py, LML#640/#376) -- e.g. a YTM
                # result set whose rows all lack `artists`. Treat as a miss,
                # mirroring the /streaming-check orchestrator's per-task isolation.
                log.exception("resolve failed for %r - %r; recording a miss", c.artist, c.title)
                return DrainOutcome(candidate=c, match=None)
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


async def execute_write(
    matched_outcomes: Sequence[DrainOutcome],
    *,
    db_path: str = DEFAULT_RESULTS_DB_PATH,
) -> dict[str, int]:
    """Fill-only-persist verified YTM links into the streaming_availability store.

    Option A of the #1056 write-path fork: ``streaming_availability.db`` is the
    authoritative, top-priority offline link store. Each match maps to its
    ``albums`` row by normalized ``(artist, title)`` -- the exact key the dedup
    pipeline wrote -- and ``youtube_music_url`` is filled only when NULL, so a
    resolved (higher-priority) link is never clobbered (Data Safety; #669). The
    rest of the chain already exists: ``export_streaming_links.py`` carries the
    column into ``library.db.streaming_links`` and ``/lookup`` surfaces it, so
    this is the last missing piece -- the producer.

    Returns a tally ``{written, already_present, unmatched}``. A candidate whose
    normalized ``(artist, title)`` has no ``albums`` row is counted ``unmatched``
    and skipped (never a wrong write). Default is dry-run: this runs only under
    the explicit ``--execute`` opt-in, off-peak, one bulk consumer at a time.
    """
    tally = {"written": 0, "already_present": 0, "unmatched": 0}
    db = ResultsDB(db_path)
    await db.connect()
    try:
        for outcome in matched_outcomes:
            if outcome.match is None:  # defensive: caller passes matches only
                continue
            album_id = await db.get_album_id_by_names(
                outcome.candidate.artist, outcome.candidate.title
            )
            if album_id is None:
                tally["unmatched"] += 1
                log.warning(
                    "no albums row for %r - %r; skipping YTM url write",
                    outcome.candidate.artist,
                    outcome.candidate.title,
                )
                continue
            wrote = await db.update_youtube_music_url(album_id, outcome.match.url)
            tally["written" if wrote else "already_present"] += 1
    finally:
        await db.close()
    return tally


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
        # Opt-in, fill-only persistence into the streaming_availability store
        # (Option A). Default path is dry-run and writes nothing.
        tally = await execute_write(
            [o for o in outcomes if o.match is not None], db_path=args.results_db
        )
        report["write"] = tally
        log.info(
            "persisted YTM links: %d written, %d already present, %d unmatched (db=%s)",
            tally["written"],
            tally["already_present"],
            tally["unmatched"],
            args.results_db,
        )
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
        "--results-db",
        default=DEFAULT_RESULTS_DB_PATH,
        help="streaming_availability.db path to fill-only-persist into under --execute.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Opt-in: fill-only-persist matches into --results-db (default: dry-run, writes nothing).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    main()
