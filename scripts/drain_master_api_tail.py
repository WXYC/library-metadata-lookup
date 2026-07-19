"""Resolve the LML#858 Discogs-API master tail and emit a seed CSV.

``scripts/resolve_master_overrides.py`` pins every master that has a cached
version or a local ``main_release_id``. Its ``--out-unresolved`` list is the
residual tail: masters with *no* cached release and *no* local ``main_release_id``
(they sit outside the discogs-cache's library-artist scope). This script
resolves that tail against the live Discogs API and produces a seed CSV the
existing ``scripts/seed_library_release_overrides.py`` consumes unchanged.

Per master (deduped across cards):

1. ``get_master(id)`` → ``.main_release_id`` — Discogs' canonical release.
2. ``get_release(main_release_id)`` — this both **confirms a non-blank
   tracklist** (so we never pin a trackless release, matching the Phase-2
   invariant) and **warms the PG cache** via the fallthrough write-back, so the
   first user lookup is hot instead of a cold-tail API hit (LML#706).

Only masters that clear both steps (``STATUS_RESOLVED``) are pinned — one row
per card that referenced the master — under the ``alex-l-2026-masters-api``
source tag.

**Safety.** ``get_master`` / ``get_release`` route through
``DiscogsService._request_with_retry``, so every call is bounded by the shared
50/min rate limiter and shielded by the LML#755 saturation breaker. Even so this
shares the prod Discogs token with live traffic: run **off-peak** and **never**
concurrently with a bulk backfill campaign (BS#1631-class). Progress is
checkpointed per master to a JSONL file, so an interrupted or re-run drain never
re-spends API budget on a master that already reached a terminal state.

Terminal states (checkpointed, never retried): ``resolved``, ``no_main_release``,
``trackless``. Retryable states (re-attempted on the next run): ``dead`` (a
``get_master`` ``None`` — ambiguous 404-vs-transient) and ``error`` (a
``get_release`` ``None`` — transient). A raised exception (e.g. a breaker shed)
is not checkpointed at all, so it is simply retried.

Usage (dry-run resolve, then inspect before seeding)::

    python -m scripts.drain_master_api_tail \\
        --unresolved unresolved.csv \\
        --checkpoint drain_checkpoint.jsonl \\
        --out-seed phase2_master_links_api.csv \\
        --concurrency 4

Then seed with the existing seeder::

    python -m scripts.seed_library_release_overrides.py \\
        --input phase2_master_links_api.csv \\
        --source alex-l-2026-masters-api --execute
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("drain_master_api_tail")

# Per-master outcome states.
STATUS_RESOLVED = "resolved"  # main_release found + non-blank tracklist -> pin
STATUS_NO_MAIN = "no_main_release"  # master has no main_release_id
STATUS_TRACKLESS = "trackless"  # main_release found but the release has no tracks
STATUS_DEAD = "dead"  # get_master returned None (404 or transient — ambiguous)
STATUS_ERROR = "error"  # get_release returned None (transient)

# Terminal states are stable conclusions and are skipped on re-run. ``dead`` and
# ``error`` are ambiguous/transient and are retried until they settle.
TERMINAL_STATUSES = frozenset({STATUS_RESOLVED, STATUS_NO_MAIN, STATUS_TRACKLESS})

# Seed CSV tagging. The source tag keeps the API-derived pins separable from the
# cache-derived Phase-2 pins (and a re-pin pass MUST reuse it or the seeder's
# source-guarded upsert no-ops). Tier/confidence mirror the low-confidence
# Tier-C convention: an API main_release pick is an edition approximation.
SOURCE_API = "alex-l-2026-masters-api"
TIER_API = "api"
CONF_LOW = "low"

_SEED_HEADER = [
    "card_catalog_id",
    "discogs_release_id",
    "master_id",
    "tier",
    "confidence",
    "reason",
]


@dataclass(frozen=True)
class MasterDrainOutcome:
    """The drain's terminal-or-retryable decision for one master id."""

    master_id: int
    main_release_id: int | None
    track_count: int | None
    status: str


# ---------------------------------------------------------------------------
# Pure core (no DB, no network)
# ---------------------------------------------------------------------------


def group_cards_by_master(rows: Iterable[tuple[int, int]]) -> dict[int, list[int]]:
    """Map ``(card_catalog_id, master_id)`` rows to ``{master_id: [card, ...]}``.

    Dedups masters so the API is called once per distinct master, while every
    card that referenced it still gets a pin at seed time.
    """
    out: dict[int, list[int]] = {}
    for card, master in rows:
        out.setdefault(master, []).append(card)
    return out


async def drain_master(service, master_id: int) -> MasterDrainOutcome:
    """Resolve one master to a pinnable release via the Discogs API.

    ``service`` needs async ``get_master(id)`` and ``get_release(id)`` (a
    ``DiscogsService``). Raises propagate to the caller (``run_drain`` treats a
    raise as retryable and does not checkpoint it).
    """
    master = await service.get_master(master_id)
    if master is None:
        return MasterDrainOutcome(master_id, None, None, STATUS_DEAD)
    main_release_id = master.main_release_id
    if not main_release_id or main_release_id <= 0:
        return MasterDrainOutcome(master_id, None, None, STATUS_NO_MAIN)
    release = await service.get_release(main_release_id)
    if release is None:
        return MasterDrainOutcome(master_id, main_release_id, None, STATUS_ERROR)
    track_count = len(release.tracklist or [])
    if track_count == 0:
        return MasterDrainOutcome(master_id, main_release_id, 0, STATUS_TRACKLESS)
    return MasterDrainOutcome(master_id, main_release_id, track_count, STATUS_RESOLVED)


def build_seed_rows(
    outcomes: dict[int, MasterDrainOutcome], cards_by_master: dict[int, list[int]]
) -> list[tuple[int, int, int]]:
    """Expand resolved masters to ``(card_catalog_id, release_id, master_id)`` rows.

    Only ``STATUS_RESOLVED`` masters (a confirmed, track-bearing ``main_release``)
    are seeded; every card that referenced the master gets one row.
    """
    rows: list[tuple[int, int, int]] = []
    for master_id, oc in outcomes.items():
        if oc.status != STATUS_RESOLVED or oc.main_release_id is None:
            continue
        for card in cards_by_master.get(master_id, []):
            rows.append((card, oc.main_release_id, master_id))
    return rows


# ---------------------------------------------------------------------------
# Checkpoint I/O (JSONL, append-only, last-write-wins)
# ---------------------------------------------------------------------------


def load_checkpoint(path: Path) -> dict[int, MasterDrainOutcome]:
    """Read the JSONL checkpoint into ``{master_id: outcome}`` (last line wins)."""
    out: dict[int, MasterDrainOutcome] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        mid = int(d["master_id"])
        out[mid] = MasterDrainOutcome(
            master_id=mid,
            main_release_id=d.get("main_release_id"),
            track_count=d.get("track_count"),
            status=d["status"],
        )
    return out


def append_checkpoint(path: Path, outcome: MasterDrainOutcome) -> None:
    """Append one outcome as a JSON line (creating parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(outcome)) + "\n")


def _pending_masters(
    cards_by_master: dict[int, list[int]], done: dict[int, MasterDrainOutcome]
) -> list[int]:
    """Masters not yet in a terminal state, in a stable (sorted) order."""
    return [
        m
        for m in sorted(cards_by_master)
        if (done.get(m) is None or done[m].status not in TERMINAL_STATUSES)
    ]


async def run_drain(
    service,
    cards_by_master: dict[int, list[int]],
    checkpoint_path: Path,
    *,
    concurrency: int = 4,
    limit: int | None = None,
) -> dict[int, MasterDrainOutcome]:
    """Drain all non-terminal masters, checkpointing each outcome as it lands.

    Bounded to ``concurrency`` in-flight ``drain_master`` calls (on top of the
    service's own 50/min limiter + breaker). ``limit`` caps how many masters
    this invocation processes — use it to smoke-test a small batch before the
    full run. Returns the merged ``{master_id: outcome}`` after this pass.
    """
    done = load_checkpoint(checkpoint_path)
    todo = _pending_masters(cards_by_master, done)
    if limit is not None:
        todo = todo[:limit]
    log.info(
        "drain: %d masters total, %d already terminal, %d to process this run",
        len(cards_by_master),
        len(cards_by_master) - len(_pending_masters(cards_by_master, done)),
        len(todo),
    )
    if not todo:
        return done

    sem = asyncio.Semaphore(concurrency)
    write_lock = asyncio.Lock()
    counts: dict[str, int] = {}

    async def worker(master_id: int) -> None:
        async with sem:
            try:
                outcome = await drain_master(service, master_id)
            except Exception as exc:  # noqa: BLE001 — retryable: do not checkpoint
                log.warning("master %d raised (%s) — left for retry", master_id, exc)
                async with write_lock:
                    counts["_raised"] = counts.get("_raised", 0) + 1
                return
        async with write_lock:
            append_checkpoint(checkpoint_path, outcome)
            done[master_id] = outcome
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            processed = sum(v for k, v in counts.items() if not k.startswith("_"))
            if processed % 250 == 0:
                log.info("progress: %d processed — %s", processed, dict(counts))

    await asyncio.gather(*(worker(m) for m in todo))
    log.info("drain pass complete: %s", dict(counts))
    return done


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------


def load_unresolved(path: Path) -> list[tuple[int, int]]:
    """Read the resolver's ``--out-unresolved`` CSV (``card_catalog_id,master_id``)."""
    rows: list[tuple[int, int]] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append((int(r["card_catalog_id"]), int(r["master_id"])))
    return rows


def write_seed_csv(path: Path, seed_rows: Sequence[tuple[int, int, int]]) -> int:
    """Write ``(card, release, master)`` rows in the seeder's column schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_SEED_HEADER)
        for card, release_id, master_id in seed_rows:
            w.writerow(
                [
                    card,
                    release_id,
                    master_id,
                    TIER_API,
                    CONF_LOW,
                    "api main_release for master tail",
                ]
            )
    return len(seed_rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> None:
    rows = load_unresolved(Path(args.unresolved))
    cards_by_master = group_cards_by_master(rows)
    log.info(
        "loaded %d unresolved card rows across %d distinct masters",
        len(rows),
        len(cards_by_master),
    )

    from config.settings import get_settings

    settings = get_settings()
    db_url = settings.database_url_discogs or os.environ.get("DATABASE_URL_DISCOGS")
    token = settings.discogs_token or os.environ.get("DISCOGS_TOKEN")
    if not db_url:
        raise SystemExit("DATABASE_URL_DISCOGS is not configured — cannot warm the cache")
    if not token:
        raise SystemExit("DISCOGS_TOKEN is not configured — cannot call the Discogs API")

    import asyncpg

    from discogs.cache_service import DiscogsCacheService
    from discogs.service import DiscogsService

    checkpoint_path = Path(args.checkpoint)
    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=max(2, args.concurrency))
    try:
        cache = DiscogsCacheService(pool)
        service = DiscogsService(token=token, cache_service=cache)
        try:
            outcomes = await run_drain(
                service,
                cards_by_master,
                checkpoint_path,
                concurrency=args.concurrency,
                limit=args.limit,
            )
        finally:
            await service.close()
    finally:
        await pool.close()

    seed_rows = build_seed_rows(outcomes, cards_by_master)
    n = write_seed_csv(Path(args.out_seed), seed_rows)
    resolved_masters = sum(1 for o in outcomes.values() if o.status == STATUS_RESOLVED)
    log.info(
        "wrote %d seed rows (%d resolved masters) -> %s (seed as --source %s)",
        n,
        resolved_masters,
        args.out_seed,
        SOURCE_API,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--unresolved",
        required=True,
        help="CSV of (card_catalog_id, master_id) from resolve_master_overrides --out-unresolved",
    )
    parser.add_argument(
        "--checkpoint",
        required=True,
        help="JSONL checkpoint path (resumable; never re-spends API budget)",
    )
    parser.add_argument(
        "--out-seed",
        required=True,
        help="Output seed CSV (card_catalog_id, discogs_release_id, ...) for the seeder",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Max in-flight masters (on top of the service's 50/min limiter; default 4)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap masters processed this run (smoke-test a batch before the full drain)",
    )
    return parser


if __name__ == "__main__":
    asyncio.run(_run(_build_arg_parser().parse_args()))
