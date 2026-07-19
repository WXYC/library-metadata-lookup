"""Off-prod Apple-Music-URL resolver for the BS#1631 album-phase tail.

BS#1631 drives LML ``/api/v1/lookup`` over ~27K still-null ``album_metadata``
rows to fill ``apple_music_url``. Almost none of a full lookup's work is needed
for that column: the persistent album-level ``apple_music_url`` the backfill
reads comes *solely* from the async warm calling
``AppleMusicClient.find_album_match(artist, album)``
(``entity/streaming_url_cache.py:350``), which uses the shared fuzz-floor
``find_best_source_match``. So a standalone resolver that calls
``find_album_match`` directly has **exact match parity** with the album-phase
capture path, at a fraction of the cost — and, run off-prod, it sidesteps both
the LML health watchdog and the synchronous-enrichment latency wall (LML#706)
that make the in-service backfill trip and under-capture.

This is the **resolve** half of a two-phase, clean-ownership design: this script
(LML repo) reads the still-null candidate set ``(album_id, artist, album)`` and
emits ``(album_id, apple_music_url)`` for accepted matches to a TSV; the **apply**
half (Backend-Service ``jobs/apple-music-url-backfill/resolve.ts::applyUpdate``,
fill-only ``WHERE apple_music_url IS NULL``) writes it back. LML never writes BS
RDS.

Per candidate:

1. ``find_album_match(artist, album)`` — the exact album-level warm path.
2. A ``SourceMatch`` -> ``matched`` (emit ``album_id\turl``); ``None`` ->
   ``no_match``; a raised Apple API error -> ``api_error`` (transient).

Progress is checkpointed per album to a JSONL file, so an interrupted or re-run
resolve never re-spends Apple API budget on a candidate already in a terminal
state (``matched`` / ``no_match``). ``api_error`` is transient and retried.

**Transient failures settle as ``no_match``, not ``api_error``.** The
authenticated ``AppleMusicClient`` (migrated off the iTunes Search API in
LML#443) *swallows* transient HTTP failures — an exhausted 429/5xx retry, a
transport error, a bad-JSON body — to an empty result, so ``find_album_match``
returns ``None`` for them, indistinguishable from a genuine "no Apple album".
Those land as ``no_match`` (terminal). The ``api_error`` bucket therefore only
catches a rare hard *raise*; it is **not** the 429 back-pressure path. So run
off-peak: transient contention silently freezes candidates as ``no_match`` for
this checkpoint, and re-probing them needs a *fresh* checkpoint (the BS
fill-only predicate keeps them eligible, so a future full re-run is safe but
low-yield).

**Safety.** The Apple JWT creds are prod's, and ``AppleMusicClient`` already
enforces its own internal ceiling — an ``asyncio.Semaphore(5)`` plus an
``AsyncLimiter`` of ~60 calls/min — that is *not* coordinated with the live
service's instance. ``--concurrency`` and ``--rate-per-min`` can only make this
resolver **more** conservative than that built-in 5-concurrent / ~60-per-minute
cap (they bind only when set below it); they cannot make it faster. To leave
headroom for prod's own Apple enrichment, run **off-peak** and set
``--concurrency`` / ``--rate-per-min`` *below* the built-in ceiling. Read-only
against BS (candidates are an exported TSV, not a live BS read); the only write
is the separate fill-only apply.

Usage (dry-run tally first, then the real run)::

    python -m scripts.resolve_apple_urls \\
        --candidates candidates.tsv \\
        --checkpoint apple_resolve.jsonl \\
        --out apple_urls.tsv \\
        --concurrency 3 --rate-per-min 30 --dry-run

    python -m scripts.resolve_apple_urls \\
        --candidates candidates.tsv \\
        --checkpoint apple_resolve.jsonl \\
        --out apple_urls.tsv \\
        --concurrency 3 --rate-per-min 30
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv

from scripts._lib.csv_ids import parse_positive_int

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("resolve_apple_urls")

# Per-candidate outcome states.
STATUS_MATCHED = "matched"  # find_album_match returned a SourceMatch -> emit url
STATUS_NO_MATCH = "no_match"  # find_album_match returned None (no Apple album)
STATUS_API_ERROR = "api_error"  # find_album_match raised (transient) -> retry

# Terminal states are stable conclusions and are skipped on re-run. ``api_error``
# is transient and retried until it settles.
TERMINAL_STATUSES = frozenset({STATUS_MATCHED, STATUS_NO_MATCH})

_TSV_HEADER = ["album_id", "apple_music_url"]


@dataclass(frozen=True)
class AlbumResolveOutcome:
    """The resolver's terminal-or-retryable decision for one album id."""

    album_id: int
    apple_music_url: str | None
    status: str


# ---------------------------------------------------------------------------
# Pure core (no network)
# ---------------------------------------------------------------------------


async def resolve_one(client, album_id: int, artist: str, album: str) -> AlbumResolveOutcome:
    """Resolve one ``(artist, album)`` to an Apple URL via ``find_album_match``.

    ``client`` needs async ``find_album_match(artist, title) -> SourceMatch|None``
    (an ``AppleMusicClient``). A raised Apple API error is caught and reported as
    ``api_error`` so one transient failure never aborts the run — the caller
    leaves it un-terminal for retry on the next pass. Note the real client
    swallows transient HTTP errors to ``None`` (recorded ``no_match``); this
    ``api_error`` path only catches a rare hard raise (see the module docstring).
    """
    try:
        match = await client.find_album_match(artist, album)
    except Exception as exc:  # noqa: BLE001 — transient Apple error: retryable
        log.warning("album %d (%s — %s) raised: %s", album_id, artist, album, exc)
        return AlbumResolveOutcome(album_id, None, STATUS_API_ERROR)
    url = match.url if match is not None else None
    if url:
        return AlbumResolveOutcome(album_id, url, STATUS_MATCHED)
    return AlbumResolveOutcome(album_id, None, STATUS_NO_MATCH)


def min_interval_seconds(rate_per_min: int | None) -> float:
    """Minimum seconds between call starts for ``rate_per_min``, or 0 (unbounded).

    ``None`` or a non-positive rate disables spacing.
    """
    if not rate_per_min or rate_per_min <= 0:
        return 0.0
    return 60.0 / rate_per_min


class _RateGate:
    """Serializes call *starts* to at least ``min_interval`` apart (monotonic).

    On top of the concurrency semaphore: the semaphore bounds in-flight calls;
    this bounds the start rate so a burst of fast Apple responses can't exceed
    the per-minute budget. ``min_interval == 0`` is a no-op.
    """

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait_for = self._next_allowed - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


# ---------------------------------------------------------------------------
# Checkpoint I/O (JSONL, append-only, last-write-wins)
# ---------------------------------------------------------------------------


def load_checkpoint(path: Path) -> dict[int, AlbumResolveOutcome]:
    """Read the JSONL checkpoint into ``{album_id: outcome}`` (last line wins).

    A malformed line — a truncated final record from a hard kill mid-append, or a
    JSON object missing a required field — is skipped with a warning rather than
    aborting the resume, so a partial write never strands the whole run.
    """
    out: dict[int, AlbumResolveOutcome] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            outcome = AlbumResolveOutcome(
                album_id=int(d["album_id"]),
                apple_music_url=d.get("apple_music_url"),
                status=d["status"],
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            log.warning("skipping malformed checkpoint line (%s): %.80s", exc, line)
            continue
        out[outcome.album_id] = outcome
    return out


def append_checkpoint(path: Path, outcome: AlbumResolveOutcome) -> None:
    """Append one outcome as a JSON line (creating parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(outcome)) + "\n")


def _pending_candidates(
    candidates: Sequence[tuple[int, str, str]], done: dict[int, AlbumResolveOutcome]
) -> list[tuple[int, str, str]]:
    """Candidates not yet in a terminal state, preserving input order."""
    return [
        c
        for c in candidates
        if (done.get(c[0]) is None or done[c[0]].status not in TERMINAL_STATUSES)
    ]


async def run_resolve(
    client,
    candidates: Sequence[tuple[int, str, str]],
    checkpoint_path: Path,
    *,
    concurrency: int = 10,
    limit: int | None = None,
    rate_per_min: int | None = None,
    dry_run: bool = False,
    out_path: Path | None = None,
) -> dict[int, AlbumResolveOutcome]:
    """Resolve all non-terminal candidates, checkpointing each outcome as it lands.

    Bounded to ``concurrency`` in-flight ``find_album_match`` calls and, if
    ``rate_per_min`` is set, at most that many call-starts per minute. ``limit``
    caps how many candidates this invocation processes — use it to smoke-test a
    small batch. Returns the merged ``{album_id: outcome}`` after this pass. When
    ``dry_run`` and ``out_path`` are given, the run tallies but writes no output
    TSV (the checkpoint is still updated so a dry-run resume is cheap).

    Raises ``ValueError`` for out-of-range knobs so a typo fails fast: a
    ``concurrency < 1`` builds an unacquirable ``asyncio.Semaphore(0)`` and hangs;
    a negative ``limit`` slices ``todo[:-1]`` and would resolve all-but-one of the
    tail against the prod Apple token instead of a small batch.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be >= 1 when set, got {limit}")
    done = load_checkpoint(checkpoint_path)
    pending = _pending_candidates(candidates, done)
    todo = pending[:limit] if limit is not None else pending
    log.info(
        "resolve: %d candidates total, %d already terminal, %d to process this run",
        len(candidates),
        len(candidates) - len(pending),
        len(todo),
    )

    sem = asyncio.Semaphore(concurrency)
    gate = _RateGate(min_interval_seconds(rate_per_min))
    write_lock = asyncio.Lock()
    counts: dict[str, int] = {}

    async def worker(album_id: int, artist: str, album: str) -> None:
        async with sem:
            await gate.wait()
            outcome = await resolve_one(client, album_id, artist, album)
        async with write_lock:
            append_checkpoint(checkpoint_path, outcome)
            done[album_id] = outcome
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            processed = sum(counts.values())
            if processed % 250 == 0:
                log.info("progress: %d processed — %s", processed, dict(counts))

    if todo:
        await asyncio.gather(*(worker(aid, art, alb) for aid, art, alb in todo))
        log.info("resolve pass complete: %s", dict(counts))
    else:
        log.info("resolve: nothing to process this run (all candidates already terminal)")

    # Emit the output TSV even when there was no new work: the documented
    # workflow (a --dry-run pass, which checkpoints every candidate terminal,
    # then a real run) leaves ``todo`` empty on the emitting pass, and a re-run
    # to regenerate a deleted/stale TSV also has an all-terminal checkpoint.
    # The output is always the full ``done`` set, not just this pass's work.
    if dry_run:
        log.info("dry-run: %d matched this pass (no TSV emitted)", counts.get(STATUS_MATCHED, 0))
    elif out_path is not None:
        n = write_url_tsv(out_path, done)
        log.info("wrote %d matched (album_id, url) rows -> %s", n, out_path)
    return done


# ---------------------------------------------------------------------------
# TSV I/O
# ---------------------------------------------------------------------------


def load_candidates(path: Path) -> list[tuple[int, str, str]]:
    """Read the ``album_id\\tartist\\talbum`` candidate TSV (read-only BS export).

    Ids go through the shared :func:`parse_positive_int`; a blank/non-integer/
    non-positive id, a blank artist, or a blank album skips the row rather than
    aborting the whole rate-limited run. Opens ``utf-8-sig`` so a BOM-prefixed
    export still matches the header.

    ``quoting=QUOTE_NONE`` because a BS export is genuine (unescaped) TSV: a
    double-quote in an artist/album cell (``7" Single``, a quoted album title)
    is a literal character. With csv's default quote processing a leading quote
    would be stripped — silently altering the Apple query — and an unbalanced
    quote would swallow every following line into one field, dropping candidates.
    """
    rows: list[tuple[int, str, str]] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            album_id = parse_positive_int(r.get("album_id"))
            artist = (r.get("artist") or "").strip()
            album = (r.get("album") or "").strip()
            if album_id is None or not artist or not album:
                continue
            rows.append((album_id, artist, album))
    return rows


def write_url_tsv(path: Path, outcomes: dict[int, AlbumResolveOutcome]) -> int:
    """Write ``album_id\\tapple_music_url`` for matched outcomes only.

    This is the fill payload the BS apply consumes; ``no_match`` / ``api_error``
    carry no URL and are omitted. Rows are sorted by ``album_id`` for a stable
    diff-able output.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    matched = sorted(
        (o for o in outcomes.values() if o.status == STATUS_MATCHED and o.apple_music_url),
        key=lambda o: o.album_id,
    )
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(_TSV_HEADER)
        for o in matched:
            w.writerow([o.album_id, o.apple_music_url])
    return len(matched)


# ---------------------------------------------------------------------------
# Apple client construction (off-prod precedent: recheck_streaming_after_mojibake)
# ---------------------------------------------------------------------------


def _build_apple_client():
    """Construct an ``AppleMusicClient`` from ``os.environ`` Apple JWT creds.

    Modeled on the off-prod precedent
    ``scripts/recheck_streaming_after_mojibake.py:193-197`` — same
    ``team_id``/``key_id``/``private_key`` construction and missing-creds skip.
    Raises ``SystemExit`` if the creds are not all present (nothing to resolve).
    """
    from clients.streaming.apple_music import AppleMusicClient

    team = os.environ.get("APPLE_MUSIC_TEAM_ID")
    key = os.environ.get("APPLE_MUSIC_KEY_ID")
    priv = os.environ.get("APPLE_MUSIC_PRIVATE_KEY")
    if not (team and key and priv):
        raise SystemExit("APPLE_MUSIC_TEAM_ID/KEY_ID/PRIVATE_KEY not all set — cannot resolve")
    return AppleMusicClient(team_id=team, key_id=key, private_key=priv)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> None:
    # Load a local ``.env`` so Apple JWT creds kept there resolve, matching the
    # off-prod precedent ``recheck_streaming_after_mojibake.py`` and the ~10
    # other scripts in this repo. ``os.environ`` (e.g. Railway) still wins.
    load_dotenv()
    candidates = load_candidates(Path(args.candidates))
    log.info("loaded %d candidate albums from %s", len(candidates), args.candidates)

    client = _build_apple_client()
    try:
        await run_resolve(
            client,
            candidates,
            Path(args.checkpoint),
            concurrency=args.concurrency,
            limit=args.limit,
            rate_per_min=args.rate_per_min,
            dry_run=args.dry_run,
            out_path=Path(args.out),
        )
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 — best-effort close
            log.exception("failed to close Apple Music client")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidates",
        required=True,
        help="TSV of (album_id, artist, album) — read-only BS still-null export",
    )
    parser.add_argument(
        "--checkpoint",
        required=True,
        help="JSONL checkpoint path (resumable; never re-spends Apple budget)",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output TSV (album_id, apple_music_url) for the BS fill-only apply",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Max in-flight find_album_match calls (default 10)",
    )
    parser.add_argument(
        "--rate-per-min",
        type=int,
        default=None,
        help="Cap Apple call-starts per minute (default: unbounded; set for shared token)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap candidates processed this run (smoke-test a batch before the full run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve + tally + checkpoint but emit no output TSV",
    )
    return parser


if __name__ == "__main__":
    asyncio.run(_run(_build_arg_parser().parse_args()))
