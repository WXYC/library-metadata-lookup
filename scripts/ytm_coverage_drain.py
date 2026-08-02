#!/usr/bin/env python3
"""YouTube Music coverage drain (LML#1056, productionized in #1070).

Resolves verified ``music.youtube.com/browse/<browseId>`` album links for a set
of canonical ``(artist, title)`` candidates via :class:`YouTubeMusicClient`
(80/80 floor, shared production matcher), emits a coverage report, and -- under
the explicit ``--execute`` opt-in -- fill-only-persists every outcome (matches
*and* misses).

Write path (Option A of the #1056 / #1052 fork)
-----------------------------------------------
Persistence goes to ``streaming_availability.db`` -- LML's authoritative,
top-priority offline link store -- via :meth:`ResultsDB.update_youtube_music_url`
(fill-only) and :meth:`ResultsDB.mark_youtube_music_not_found` (also fill-only
guarded: it can never downgrade a row already resolved to ``found``). The rest
of the chain already exists: ``export_streaming_links.py`` copies
``albums.youtube_music_url`` into ``library.db.streaming_links`` and ``/lookup``
surfaces it, so this drain is the last missing piece, the producer. The sibling
``lml_cache.album_streaming_url_cache`` (Option B) is reserved for #1052's
runtime/rowless population, which cannot reach this library-keyed store.

Candidate source (DECIDED, #1070)
----------------------------------
The production candidate source is a **pending-scan over
``streaming_availability.db.albums``**, via ``--from-db`` ->
:func:`load_candidates_from_db` -> ``ResultsDB.get_pending("youtube_music",
limit)``. Because ``albums`` already holds the canonical ``(artist, title)``
that keys every row, the row's own ``id`` is the write target and its
``display_artist`` / ``display_title`` are the search terms -- no cross-store
names-join needed. The ``youtube_music_status = 'pending'`` filter excludes
both resolved (``found``) and previously-attempted (``not_found``) rows, so
the drain is **resumable for free**: each ``--execute`` run marks its page
(found + not_found), shrinking the next run's pending set. Wiring
``entity.release_identity`` -> discogs-cache as the candidate source was
considered and rejected (same-or-narrower universe, needs prod credentials
and a human go-ahead mid-run); file a follow-up only if the ``albums``
universe ever proves insufficient. ``--sample-csv`` -> :func:`load_candidates_from_csv`
remains the credential-free dry-run/sample harness; its misses have no
``album_id`` and are skipped (not markable) by :func:`execute_write`.

Usage
-----
``ytmusicapi`` lives in the ``drain`` extra (not ``dev``), and ``_run`` always
builds a real client, so the drain must run under ``--extra drain`` or the lazy
``from ytmusicapi import YTMusic`` raises ``ModuleNotFoundError``. Exactly one
of ``--from-db`` / ``--sample-csv`` is required.

    # production dry-run: pending-scan + report, write nothing
    uv run --extra drain python -m scripts.ytm_coverage_drain --from-db \
        --limit 200 --results-db streaming_availability.db

    # production persist (opt-in, fill-only, resumable pagination): --limit is
    # the get_pending page size; each --execute run shrinks the pending set,
    # so repeated invocations walk the whole backlog
    uv run --extra drain python -m scripts.ytm_coverage_drain --from-db \
        --limit 200 --execute --results-db streaming_availability.db

    # sample dry-run (credential-free, local CSV)
    uv run --extra drain python -m scripts.ytm_coverage_drain --sample-csv resolved_names.csv \
        --limit 200 --concurrency 4 --report-json /tmp/ytm_drain_report.json

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
    album_id: int | None = None


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


async def load_candidates_from_db(db: ResultsDB, limit: int) -> list[Candidate]:
    """Load the production candidate source: the ``albums`` pending-scan (#1070).

    ``get_pending("youtube_music", limit)`` already excludes both resolved
    (``found``) and previously-attempted (``not_found``) rows, so this loader
    is resumable for free -- a re-run only ever sees the untried backlog. Each
    row's own ``id`` is carried as ``Candidate.album_id`` so the write path can
    target it directly, with no ``(artist, title)`` names-join. ``db`` must
    already be connected; rows with a blank display artist or title are
    skipped, mirroring :func:`load_candidates_from_rows`.
    """
    rows = await db.get_pending("youtube_music", limit=limit)
    out: list[Candidate] = []
    for row in rows:
        artist = str(row["display_artist"] or "").strip()
        title = str(row["display_title"] or "").strip()
        if not artist or not title:
            continue
        out.append(Candidate(artist=artist, title=title, album_id=row["id"]))
    return out


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
    outcomes: Sequence[DrainOutcome],
    *,
    db_path: str = DEFAULT_RESULTS_DB_PATH,
) -> dict[str, int]:
    """Fill-only-persist every drain outcome -- matches and misses -- (#1070).

    Option A of the #1056 write-path fork: ``streaming_availability.db`` is the
    authoritative, top-priority offline link store. The rest of the chain
    already exists: ``export_streaming_links.py`` carries ``youtube_music_url``
    into ``library.db.streaming_links`` and ``/lookup`` surfaces it, so this is
    the producer.

    For a **match**: prefer ``outcome.candidate.album_id`` (the from-db pending-
    scan path -- write straight to the row, no names-join) and fall back to a
    normalized ``(artist, title)`` lookup only when it's unset (the CSV path).
    ``youtube_music_url`` is filled only when NULL, so a resolved (higher-
    priority) link is never clobbered (Data Safety; #669).

    For a **miss**: if the candidate carries an ``album_id``, durably mark
    ``not_found`` (fill-only guarded) so a re-run's pending-scan skips it --
    the resumability this drain exists for (#1070). A CSV-path miss has no
    ``album_id`` and no row to mark, so it's skipped silently (expected).

    Returns a tally ``{written, already_present, unmatched, not_found}``.
    Default is dry-run: this runs only under the explicit ``--execute``
    opt-in, off-peak, one bulk consumer at a time.
    """
    tally = {"written": 0, "already_present": 0, "unmatched": 0, "not_found": 0}
    db = ResultsDB(db_path)
    await db.connect()
    try:
        for outcome in outcomes:
            candidate = outcome.candidate
            if outcome.match is None:
                if candidate.album_id is not None:
                    await db.mark_youtube_music_not_found(candidate.album_id)
                    tally["not_found"] += 1
                continue
            album_id = candidate.album_id
            if album_id is None:
                album_id = await db.get_album_id_by_names(candidate.artist, candidate.title)
            if album_id is None:
                tally["unmatched"] += 1
                log.warning(
                    "no albums row for %r - %r; skipping YTM url write",
                    candidate.artist,
                    candidate.title,
                )
                continue
            wrote = await db.update_youtube_music_url(album_id, outcome.match.url)
            tally["written" if wrote else "already_present"] += 1
    finally:
        await db.close()
    return tally


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.from_db:
        db = ResultsDB(args.results_db)
        await db.connect()
        try:
            candidates = await load_candidates_from_db(db, args.limit)
        finally:
            await db.close()
    else:
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
        # Opt-in, fill-only persistence of every outcome -- found and missed
        # alike (#1070 resumability). Default path is dry-run and writes nothing.
        tally = await execute_write(outcomes, db_path=args.results_db)
        report["write"] = tally
        log.info(
            "persisted YTM outcomes: %d written, %d already present, %d unmatched, "
            "%d not_found (db=%s)",
            tally["written"],
            tally["already_present"],
            tally["unmatched"],
            tally["not_found"],
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
        description="YouTube Music coverage drain (LML#1056, productionized in #1070)."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--sample-csv",
        help="Credential-free dry-run/sample harness: a CSV with an 'artist,title' "
        "header holding resolved canonical names.",
    )
    source.add_argument(
        "--from-db",
        action="store_true",
        help="Production candidate source: a pending-scan over --results-db's albums "
        "table (get_pending('youtube_music', --limit)). Resumable for free -- a "
        "--execute run marks found + not_found, shrinking the next run's pending set.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="--sample-csv: cap candidates (0 = all). --from-db: required get_pending "
        "page size, must be > 0.",
    )
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
        help="streaming_availability.db path. --from-db reads its pending-scan from "
        "here; --execute fill-only-persists into it.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Opt-in: fill-only-persist outcomes into --results-db "
        "(default: dry-run, writes nothing).",
    )
    args = parser.parse_args(argv)
    if args.from_db and args.limit <= 0:
        parser.error("--from-db requires --limit > 0 (the get_pending page size)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    main()
