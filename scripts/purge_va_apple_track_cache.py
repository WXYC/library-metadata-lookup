"""Purge pre-LML#1139 Various-Artists rows from the L1 track-URL cache.

``lml_cache.track_streaming_url_cache`` (LML#893) is **hit-only and TTL-less**,
and ``lookup/enrichment/apple_probe.py`` peeks it *before* running the live Apple
probe. So a wrong V/A deep-link cached before the LML#1139 artist-axis guard
shipped would serve forever, on exactly the repeat-play traffic that recurs
most, and the guard would never get a chance to re-adjudicate it. Deploying the
guard without this purge leaves the bug fully live from cache.

Selection is the coarse-net + pure-arbiter idiom (structural donor:
``scripts/audit_va_writeback_pollution.py``, the same shape against
``entity.identity``):

1. A deliberately **unanchored superset** regex bounds the scan in SQL.
2. ``wxyc_etl.text.is_compilation_artist`` — the exact predicate the guard uses,
   applied to the exact string the guard sees — is the true arbiter in Python.

Step 2 is what makes the selection correct; step 1 only keeps the scan cheap.
Real artists like ``various production`` and ``the various`` DO match the regex
and are rejected by the arbiter, which is why the arbiter is load-bearing rather
than decorative.

TWO CORRECTIONS TO THE ISSUE BODY, both verified rather than assumed:

* **Service key.** The issue's SQL says ``service = 'apple_music'``. That value
  can never exist here: ``streaming.service.TRACK_CACHE_KEYS`` maps
  ``APPLE_MUSIC -> "apple_music_track"``, pinned by a named CHECK constraint on
  the table. The literal would have deleted **zero rows** while reporting a
  clean zero — the ordering hazard above, in its silent form. This script
  imports ``APPLE_MUSIC_TRACK_SERVICE`` rather than writing any literal
  (the LML#1037 anti-drift pattern).
* **Regex.** The donor's ``v\\.a\\.`` alternative requires a trailing dot, but
  ``is_compilation_artist`` accepts the dotless form: ``v.a``, ``v.a - jazz``
  and ``v.a 1998`` are all arbiter-True and donor-net-False. Reusing the donor
  regex verbatim would therefore NOT be a superset, and those guard-struck keys
  would never be purged. Widened here to ``v[./]\\s*a\\.?``.

The cache key is ``to_match_form(artist)`` output, which is also what the
LML#1139 guard tests, so the purged key space and the guarded key space are the
same key space by construction rather than by coincidence. (Within it the purge
is a deliberate SUPERSET: it keys on the query artist alone, while the guard
also requires a V/A *candidate* and an album-less pass. Correct album-cleared
V/A links are purged too — see the over-deletion note below.)

ACCEPTED OVER-DELETION, and why it is not free. Deleting is safe for
correctness because the table is hit-only: a missing row is a miss, never a
wrong answer. But ``docs/env-vars.md`` (LML#904) records that at the default
``LML_APPLE_MUSIC_RATE_PER_MIN=60`` roughly **56% of ``find_track_url`` probes
time out** on LML's own self-throttle and return null, and nulls are never
cached (``url NOT NULL``). A purged key therefore does not reliably re-fill on
next play — it re-probes and mostly re-nulls until it wins the throttle. Run
this AFTER rolling ``LML_APPLE_MUSIC_RATE_PER_MIN`` up (60 -> 300 -> 600), and
run it in WAVES via ``--limit`` so the re-probe load arrives gradually.

Every purged row is written to a recovery CSV, so a knowingly-over-deleting
irreversible operation on expensive-to-replace API-derived data stays
recoverable. Two properties make that guarantee actually hold:

* **The rows come from the DELETE itself.** The execute path is a single
  ``DELETE ... RETURNING`` inside one transaction, and the CSV is written
  before that transaction commits. A preceding SELECT plus a separate DELETE
  would be two autocommit statements on two different pooled connections, and
  this guard does NOT block album-CONSTRAINED V/A cache writes — so live
  ``/lookup`` traffic could insert a row for a purge key in the gap and have it
  deleted with no CSV entry. (The dry-run path has no such gap; it only reads.)
* **Waves cannot clobber each other.** ``--out`` defaults to a timestamped
  per-invocation path, and an existing path is refused rather than truncated.
  Copy each wave's file somewhere durable anyway.

Flag naming note: the donor uses ``--apply``; this script uses ``--execute`` to
match ``seed_library_release_overrides.py`` and the downstream BS#2000 job. The
divergence is deliberate, not drift.

Usage:
    # dry-run (default): report + write the recovery CSV, delete nothing
    uv run python -m scripts.purge_va_apple_track_cache

    # first wave of 500 artist keys
    uv run python -m scripts.purge_va_apple_track_cache --limit 500 --execute

    # next wave — resume past the last key the previous wave examined
    uv run python -m scripts.purge_va_apple_track_cache \\
        --limit 500 --after 'various artists - jazz' --execute
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from wxyc_etl.text import is_compilation_artist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("purge_va_apple_track_cache")

# Unanchored SUPERSET of `is_compilation_artist` over `to_match_form` output.
# Widened from the donor's `v\.a\.` (see the module docstring) so the dotless
# `v.a` family is actually reachable. Unanchored on purpose: real artists like
# `the various` reach the arbiter and are rejected BY it, which is the property
# the tests assert.
SUPERSET_REGEX = r"(various|soundtrack|compilation|v[./]\s*a\.?)"

_SELECT_KEYS_SQL = """\
SELECT DISTINCT artist_normalized
FROM lml_cache.track_streaming_url_cache
WHERE service = $1
  AND artist_normalized ~ $2
  AND artist_normalized > $3
ORDER BY artist_normalized\
"""

_SELECT_ROWS_SQL = """\
SELECT service, artist_normalized, album_normalized, song_normalized, url
FROM lml_cache.track_streaming_url_cache
WHERE service = $1 AND artist_normalized = ANY($2)
ORDER BY artist_normalized, album_normalized, song_normalized\
"""

_DELETE_RETURNING_SQL = """\
DELETE FROM lml_cache.track_streaming_url_cache
WHERE service = $1 AND artist_normalized = ANY($2)
RETURNING service, artist_normalized, album_normalized, song_normalized, url\
"""

_CSV_COLUMNS = ("service", "artist_normalized", "album_normalized", "song_normalized", "url")


def classify(keys: list[str]) -> tuple[list[str], list[str]]:
    """Split coarse-net keys into (purge, keep) via the guard's own predicate.

    Pure, so the arbitration that actually decides what gets deleted is
    unit-testable without a database or a driver. ``is_compilation_artist``
    lowercases internally and is leading-anchored; these keys are already
    ``to_match_form`` output, which is exactly what the LML#1139 guard tests.
    """
    purge: list[str] = []
    keep: list[str] = []
    for key in keys:
        (purge if is_compilation_artist(key or "") else keep).append(key)
    return purge, keep


def write_recovery_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write the deleted rows, so the knowing over-deletion stays reversible.

    On the execute path this is called INSIDE the DELETE's transaction and
    before it commits, so rows can never be gone without being recorded: a
    crash here rolls the DELETE back rather than losing the URLs.

    Opened with mode ``"x"``, not ``"w"``. This file is the only record of an
    irreversible operation on expensive-to-replace API-derived data, so a path
    collision with an earlier wave must abort the run rather than truncate it
    (each invocation's default path is already timestamped; an explicit
    ``--out`` reused across waves is the case this catches).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("x", newline="", encoding="utf-8")
    except FileExistsError as exc:
        raise FileExistsError(
            f"recovery CSV {path} already exists — refusing to overwrite an earlier wave's "
            "only record of what it deleted. Pass a different --out."
        ) from exc
    with handle as f:
        writer = csv.DictWriter(f, fieldnames=list(_CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col) for col in _CSV_COLUMNS})
    log.info("wrote recovery CSV: %s (%d rows)", path, len(rows))


async def purge(
    pg: Any,
    *,
    service: str,
    out: Path,
    limit: int,
    after: str,
    execute: bool,
) -> int:
    """Run one wave. Returns the number of rows deleted (0 on a dry-run)."""
    sql = _SELECT_KEYS_SQL + ("\nLIMIT $4" if limit else "")
    args: list[Any] = [service, SUPERSET_REGEX, after]
    if limit:
        args.append(limit)
    key_rows = await pg.fetchall(sql, *args)
    candidate_keys = [r["artist_normalized"] for r in key_rows]

    if not candidate_keys:
        log.info("no candidate keys past cursor %r — nothing to do", after)
        return 0

    purge_keys, keep_keys = classify(candidate_keys)
    log.info(
        "wave: %d keys examined, %d to purge, %d kept as real artists (e.g. %s)",
        len(candidate_keys),
        len(purge_keys),
        len(keep_keys),
        ", ".join(repr(k) for k in keep_keys[:3]) or "none",
    )
    # Advance past every key EXAMINED, not just the purged ones. Keeping the
    # cursor on deletions would stall forever on a wave whose keys are all
    # rejected by the arbiter: those rows are never removed, so the same window
    # would be re-selected on every run.
    next_after = candidate_keys[-1]

    if not purge_keys:
        log.info("no V/A keys in this wave; next --after %r", next_after)
        return 0

    if not execute:
        rows = await pg.fetchall(_SELECT_ROWS_SQL, service, purge_keys)
        write_recovery_csv(out, rows)
        log.info(
            "[dry-run] would delete %d rows across %d keys. Pass --execute to write. "
            "Next wave: --after %r",
            len(rows),
            len(purge_keys),
            next_after,
        )
        return 0

    # One transaction, one statement. A preceding SELECT plus a separate DELETE
    # would be two autocommit round-trips on two different pooled connections,
    # and the guard does NOT block album-constrained V/A cache writes — so live
    # /lookup traffic could insert a row for one of `purge_keys` in the gap and
    # have it deleted with no entry in the recovery CSV. RETURNING closes the
    # gap and, as a bonus, makes the reported count the DELETE's own count
    # rather than a pre-DELETE estimate that `docs/scripts.md` asks the
    # operator to record on the PR.
    #
    # The CSV is written inside the transaction, so the rows are only really
    # gone once they are on disk: a crash before commit rolls the DELETE back.
    async with pg.acquire() as conn:
        async with conn.transaction():
            deleted_records = await conn.fetch(_DELETE_RETURNING_SQL, service, purge_keys)
            # RETURNING has no ORDER BY; sort so the artifact is diffable and
            # matches the dry-run CSV's ordering.
            rows = sorted(
                (dict(r) for r in deleted_records),
                key=lambda r: (
                    r["artist_normalized"] or "",
                    r["album_normalized"] or "",
                    r["song_normalized"] or "",
                ),
            )
            write_recovery_csv(out, rows)

    log.info(
        "deleted %d rows across %d keys. Next wave: --after %r",
        len(rows),
        len(purge_keys),
        next_after,
    )
    return len(rows)


async def main(args: argparse.Namespace) -> None:
    from config.settings import get_settings
    from entity.sources import PgSource
    from entity.track_streaming_url_cache import APPLE_MUSIC_TRACK_SERVICE

    db_url = get_settings().database_url_discogs
    if not db_url:
        raise SystemExit("DATABASE_URL_DISCOGS is not configured — cannot purge")

    pg = PgSource(dsn=db_url)
    try:
        await purge(
            pg,
            service=APPLE_MUSIC_TRACK_SERVICE,
            out=Path(args.out),
            limit=args.limit,
            after=args.after,
            execute=args.execute,
        )
    finally:
        await pg.close()


def _non_negative_int(raw: str) -> int:
    """``argparse`` type for ``--limit``.

    A negative value is truthy, so it would reach ``LIMIT $4`` and make asyncpg
    raise ``LIMIT must not be negative`` partway through a prod wave. Reject it
    before any connection is opened.
    """
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError(f"--limit must be >= 0 (0 means all), got {value}")
    return value


def _default_out_path() -> str:
    """Timestamped, so consecutive waves cannot collide.

    A fixed default would have wave 2 truncate wave 1's recovery CSV — and a
    plain re-check dry-run writes the CSV too, so the collision does not even
    need a second DELETE to destroy the first wave's record. Local time: this
    names a file an operator correlates with their own run log.
    """
    return f"/tmp/lml-1139-purged-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}.csv"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the DELETE (default: dry-run, which still writes the recovery CSV)",
    )
    parser.add_argument(
        "--limit",
        type=_non_negative_int,
        default=0,
        help="Distinct artist keys to examine in this wave (0 = all). Bounds re-probe load.",
    )
    parser.add_argument(
        "--after",
        default="",
        help="Resume cursor: exclusive lower bound on artist_normalized (from the previous wave's log)",
    )
    parser.add_argument(
        "--out",
        default=_default_out_path(),
        help="Recovery CSV path, written from DELETE ... RETURNING inside the transaction. "
        "Defaults to a per-invocation timestamped path under /tmp so waves cannot clobber "
        "each other; an existing path is refused rather than overwritten. Copy each wave's "
        "file somewhere durable.",
    )
    return parser


if __name__ == "__main__":
    asyncio.run(main(_build_arg_parser().parse_args()))
