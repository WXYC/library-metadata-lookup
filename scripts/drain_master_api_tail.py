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
``DiscogsService._request_with_retry``, so every call is bounded by the
service's own (per-process) 50/min rate limiter and shielded by the LML#755
saturation breaker. That limiter is not coordinated with the live service's, so
this drain shares the prod Discogs token with live traffic uncoordinated: run
**off-peak** and **never** concurrently with a bulk backfill campaign
(BS#1631-class). Progress is
checkpointed per master to a JSONL file, so an interrupted or re-run drain never
re-spends API budget on a master that already reached a terminal state.

**Pause on breaker shed.** The drain runs as its own process, so it owns a
*separate* breaker from the live service — its own requests drive it. When that
breaker sheds a live probe (a transient shared-token blip, or a competing prod
burst pushing ``X-Discogs-Ratelimit-Remaining`` to the floor), the drain
**pauses one cool-down and retries the shed master** instead of marking it dead
and marching on. Its pre-fix behavior was fatal: a single proactive trip made
every remaining master fast-fail as a swallowed ``None``, so the whole backlog
shed to ``dead`` and the run exited — ~13k masters lost to one blip. After
``--max-breaker-pauses`` consecutive pauses with no master settling in between (a
*sustained* flood, not a blip), the run stops cleanly and leaves the remainder
non-terminal for a later run.

Terminal states (checkpointed, never retried): ``resolved``, ``no_main_release``,
``trackless``. Retryable states (re-attempted on the next run): ``dead`` (a
``get_master`` ``None`` — ambiguous 404-vs-transient) and ``error`` (a
``get_release`` ``None`` — transient). A breaker shed no longer *lands* in either
bucket: ``get_release``'s raised ``DiscogsBreakerOpenError`` and ``get_master``'s
swallowed ``None`` (recognized by the open-breaker check) both route to the pause
path above, so a shed is ridden out rather than checkpointed. A non-shed raised
exception is not checkpointed at all. Either way a non-settled master is retried,
so budget is never re-spent on one already terminal.

Usage (dry-run resolve, then inspect before seeding)::

    python -m scripts.drain_master_api_tail \\
        --unresolved unresolved.csv \\
        --checkpoint drain_checkpoint.jsonl \\
        --out-seed phase2_master_links_api.csv \\
        --concurrency 4

Then seed with the existing seeder::

    python -m scripts.seed_library_release_overrides \\
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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from discogs.breaker import DiscogsBreakerOpenError
from scripts._lib.csv_ids import parse_positive_int

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

# Breaker-pause defaults. On a saturation shed the drain pauses one cool-down and
# retries the same master rather than shedding the whole backlog (its pre-fix
# behavior: a single proactive trip marked every remaining master ``dead`` and
# exited). ``DEFAULT_MAX_BREAKER_PAUSES`` consecutive pauses without a settled
# master in between end the run cleanly (a transient blip clears in one pause; a
# sustained flood — a competing backfill — won't clear within this window, so the
# drain bails and leaves the backlog non-terminal for a later run). The pause
# default sits a touch over the 20s breaker cool-down so the resumed probe lands
# after the OPEN→HALF_OPEN promotion instead of re-shedding on the boundary.
DEFAULT_MAX_BREAKER_PAUSES = 8
_DEFAULT_PAUSE_SECONDS = 22.0


def _live_breaker_is_open() -> bool:
    """True when this process's Discogs saturation breaker is not CLOSED.

    The drain runs as its own process, so ``get_discogs_breaker()`` returns the
    very breaker its ``DiscogsService`` requests drive — reading its state here
    is race-free from the live service (a different process → a different
    breaker). Imported lazily so the pure decision core and CLI arg-parsing don't
    pull in the rate-limiter's optional dependency chain.
    """
    from discogs.breaker import BreakerState
    from discogs.ratelimit import get_discogs_breaker

    return get_discogs_breaker().state is not BreakerState.CLOSED


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
    # Count only real (non-blank-title) tracks. A tracklist can carry blank-title
    # heading/index rows; the resolver's Phase-2 invariant drops empty-normalized
    # candidates, so counting raw ``len(tracklist)`` would pin a heading-only
    # release the resolver would reject as trackless.
    track_count = sum(
        1 for t in (release.tracklist or []) if (getattr(t, "title", "") or "").strip()
    )
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
    """Read the JSONL checkpoint into ``{master_id: outcome}`` (last line wins).

    A malformed line — a truncated final record from a hard kill mid-append, or
    a JSON object missing a required field — is skipped with a warning rather
    than aborting the resume, so a partial write never strands the whole drain.
    """
    out: dict[int, MasterDrainOutcome] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            outcome = MasterDrainOutcome(
                master_id=int(d["master_id"]),
                main_release_id=d.get("main_release_id"),
                track_count=d.get("track_count"),
                status=d["status"],
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            log.warning("skipping malformed checkpoint line (%s): %.80s", exc, line)
            continue
        out[outcome.master_id] = outcome
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
    breaker_is_open: Callable[[], bool] | None = None,
    pause_seconds: float = _DEFAULT_PAUSE_SECONDS,
    max_breaker_pauses: int = DEFAULT_MAX_BREAKER_PAUSES,
) -> dict[int, MasterDrainOutcome]:
    """Drain all non-terminal masters, checkpointing each outcome as it lands.

    Bounded to ``concurrency`` in-flight ``drain_master`` calls (on top of the
    service's own 50/min limiter + breaker). ``limit`` caps how many masters
    this invocation processes — use it to smoke-test a small batch before the
    full run. Returns the merged ``{master_id: outcome}`` after this pass.

    **Pause on breaker shed.** When the process's saturation breaker sheds a live
    probe — ``get_release`` raises ``DiscogsBreakerOpenError``, or ``get_master``
    swallows the shed into a ``None`` (``STATUS_DEAD``) while ``breaker_is_open``
    reads open — the drain does **not** mark the master dead and march on (its
    pre-fix behavior shed the whole backlog to ``STATUS_DEAD`` on one proactive
    trip). Instead every worker pauses one ``pause_seconds`` cool-down and the
    shed master is retried, so a transient shared-token blip is ridden out rather
    than converted into a run-ending stampede. After ``max_breaker_pauses``
    consecutive pauses with no master settling in between (a sustained flood —
    e.g. a competing backfill), the run stops cleanly, leaving the remainder
    non-terminal for a later run; no API budget is re-spent on a settled master.
    ``breaker_is_open`` defaults to this process's live breaker; tests inject a
    controllable predicate.

    Raises ``ValueError`` for out-of-range knobs, so a typo fails fast instead of
    hanging or silently misbehaving: ``concurrency < 1`` would build an
    unacquirable ``asyncio.Semaphore(0)`` and hang holding the DB pool, and a
    negative ``limit`` would slice ``todo[:-1]`` and drain all-but-one of the
    tail against the prod token instead of a small smoke batch.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be >= 1 when set, got {limit}")
    if max_breaker_pauses < 1:
        raise ValueError(f"max_breaker_pauses must be >= 1, got {max_breaker_pauses}")
    if pause_seconds < 0:
        raise ValueError(f"pause_seconds must be >= 0, got {pause_seconds}")
    if breaker_is_open is None:
        breaker_is_open = _live_breaker_is_open
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
    pause_lock = asyncio.Lock()
    resume = asyncio.Event()
    resume.set()  # workers proceed while set; a pause clears it
    counts: dict[str, int] = {}
    # Mutated only between awaits under asyncio's cooperative scheduling, so a
    # plain dict needs no lock beyond the two guarding their own critical section.
    ctrl = {"consecutive_pauses": 0, "aborted": False}

    async def pause_for_breaker() -> bool:
        """Pause every worker one cool-down, then resume — or abort the run once
        the breaker has stayed saturated ``max_breaker_pauses`` rounds.

        Returns ``True`` if the caller should retry its master, ``False`` if the
        run is aborting (the caller leaves its master non-terminal — never
        checkpointed — for a later run). Exactly one worker per round drives the
        pause; the rest wait on ``resume``.
        """
        if ctrl["aborted"]:
            return False
        drive = False
        async with pause_lock:
            if ctrl["aborted"]:
                return False
            if resume.is_set():
                # First worker into this round drives the pause; the count resets
                # to 0 whenever a master settles, so it measures a *sustained*
                # saturation, not cumulative blips across a long clean run.
                ctrl["consecutive_pauses"] += 1
                if ctrl["consecutive_pauses"] > max_breaker_pauses:
                    ctrl["aborted"] = True
                    resume.set()  # release any waiters so they can exit
                    log.warning(
                        "Discogs breaker still saturated after %d consecutive pauses "
                        "(~%.0fs) — stopping this drain run; the remaining masters stay "
                        "non-terminal for a later run (no API budget re-spent). Re-run "
                        "off-peak once the shared token has recovered.",
                        max_breaker_pauses,
                        max_breaker_pauses * pause_seconds,
                    )
                    return False
                resume.clear()
                drive = True
                log.warning(
                    "Discogs breaker OPEN — pausing the drain %.0fs (pause %d/%d) to let "
                    "the shared token recover instead of shedding the backlog",
                    pause_seconds,
                    ctrl["consecutive_pauses"],
                    max_breaker_pauses,
                )
        if drive:
            try:
                await asyncio.sleep(pause_seconds)
            finally:
                resume.set()
        else:
            await resume.wait()
        return not ctrl["aborted"]

    async def worker(master_id: int) -> None:
        while True:
            await resume.wait()  # hold outside the semaphore while the drain is paused
            if ctrl["aborted"]:
                return
            shed = False
            async with sem:
                try:
                    outcome = await drain_master(service, master_id)
                except DiscogsBreakerOpenError:
                    # get_release shed the live probe — unambiguous saturation.
                    shed = True
                except Exception as exc:  # noqa: BLE001 — retryable: do not checkpoint
                    log.warning("master %d raised (%s) — left for retry", master_id, exc)
                    async with write_lock:
                        counts["_raised"] = counts.get("_raised", 0) + 1
                    return
            if not shed and outcome.status not in TERMINAL_STATUSES and breaker_is_open():
                # get_master swallows a shed into a None (STATUS_DEAD),
                # indistinguishable from a real 404 at the return value — so a
                # non-terminal outcome while our own breaker is open is a shed,
                # not a settled conclusion. Pause rather than march the backlog.
                shed = True
            if shed:
                if await pause_for_breaker():
                    continue  # retry the same master
                return  # aborting — leave it non-terminal for a later run
            async with write_lock:
                append_checkpoint(checkpoint_path, outcome)
                done[master_id] = outcome
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
                ctrl["consecutive_pauses"] = 0  # a settled master ends any pause streak
                processed = sum(v for k, v in counts.items() if not k.startswith("_"))
                if processed % 250 == 0:
                    log.info("progress: %d processed — %s", processed, dict(counts))
            return

    await asyncio.gather(*(worker(m) for m in todo))
    log.info("drain pass complete: %s", dict(counts))
    return done


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------


def load_unresolved(path: Path) -> list[tuple[int, int]]:
    """Read the resolver's ``--out-unresolved`` CSV (``card_catalog_id,master_id``).

    Ids go through the shared :func:`parse_positive_int` (as the resolver and
    seeder do, to keep the seeding scripts from drifting): a blank, non-integer,
    or non-positive cell skips the row rather than aborting the whole
    rate-limited drain with a bare ``ValueError``. Opens ``utf-8-sig`` so a
    BOM-prefixed export still matches the header.
    """
    rows: list[tuple[int, int]] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            card = parse_positive_int(r.get("card_catalog_id"))
            master = parse_positive_int(r.get("master_id"))
            if card is None or master is None:
                continue
            rows.append((card, master))
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
    # Default the pause a touch over the breaker cool-down so the resumed probe
    # lands after the OPEN->HALF_OPEN promotion rather than re-shedding on the
    # boundary; an explicit --pause-seconds overrides.
    pause_seconds = (
        args.pause_seconds
        if args.pause_seconds is not None
        else settings.discogs_breaker_cooldown_seconds + 2.0
    )
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
                pause_seconds=pause_seconds,
                max_breaker_pauses=args.max_breaker_pauses,
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
    parser.add_argument(
        "--max-breaker-pauses",
        type=int,
        default=DEFAULT_MAX_BREAKER_PAUSES,
        help=(
            "Consecutive breaker-cool-down pauses (no master settling in between) before "
            f"the run stops and leaves the rest for later (default {DEFAULT_MAX_BREAKER_PAUSES})"
        ),
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=None,
        help=(
            "Seconds to pause on a breaker shed before retrying "
            "(default: the breaker cool-down + 2s)"
        ),
    )
    return parser


if __name__ == "__main__":
    asyncio.run(_run(_build_arg_parser().parse_args()))
