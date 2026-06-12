"""LML#537 probe #1 — Cache miss provenance sample.

For a sample of recent cache-miss → API-call events, classify each by:

  (a) did the ``release`` row already exist at miss time?
  (b) if (a), did it carry any ``release_track`` rows?
  (c) which lookup method missed (where extractable from the log line)?
  (d) was the release ID seen earlier in the window (any line referencing it)?

Output: CSV at ``--out`` (default ``/tmp/lml-537-cache-miss-provenance.csv``)
with one row per release_id extracted from the input. The (a)/(b) split
separates first-time misses (H1: ETL coverage gap) from
release-row-exists-with-tracks misses (H3': ``validate_track_on_release``
returning ``False`` as a cache hit). The (d) flag flags artist-warm hits
where the validate path's miss was the only gap.

Input shapes accepted (pick one):

* ``--log PATH`` — a text log file (``railway logs`` export, ``-`` for stdin).
  The script tries a default set of regexes against each line; lines that
  match contribute one row to the sample. The regex set captures the
  INFO-level lines that already exist in production:

    * ``Validated: 'TRACK' by 'ARTIST' found on release RELEASE_ID``
    * ``Validated (fuzzy): 'TRACK' by 'ARTIST' on release RELEASE_ID``
    * ``Track 'TRACK' by 'ARTIST' NOT found on release RELEASE_ID``
    * ``Failed to fetch release RELEASE_ID``

  Each of these fires on the API leg of ``validate_track_on_release`` —
  i.e., the cache missed on that release_id. ``method`` is set to
  ``validate_track_on_release`` for all four.

* ``--ids PATH`` — a CSV with columns ``release_id`` and ``method`` (and
  optional ``timestamp``). Use this when feeding the sample from a
  Sentry trace export rather than a stdout log.

The script does not write to PG. Read-only.

Usage::

    DATABASE_URL_DISCOGS=postgresql://... python scripts/cache_miss_provenance.py --log railway.log
    DATABASE_URL_DISCOGS=postgresql://... python scripts/cache_miss_provenance.py --log - < railway.log
    DATABASE_URL_DISCOGS=postgresql://... python scripts/cache_miss_provenance.py --ids sentry-export.csv --limit 50
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg


@dataclass(frozen=True)
class MissEvent:
    """A single cache-miss event extracted from the input."""

    release_id: int
    method: str
    timestamp: str  # original log timestamp prefix or ""


# The regex set targeting the INFO-level log lines that already exist in prod.
# Each carries a ``release_id`` extractable group. ``method`` is fixed per
# pattern — all four fire on the validate path which is the dominant warm-up
# channel per the issue's H1.
_LOG_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Validated: '.+' by '.+' found on release (\d+)"), "validate_track_on_release"),
    (
        re.compile(r"Validated \(fuzzy\): '.+' by '.+' on release (\d+)"),
        "validate_track_on_release",
    ),
    (re.compile(r"Track '.+' by '.+' NOT found on release (\d+)"), "validate_track_on_release"),
    (re.compile(r"Failed to fetch release (\d+)"), "get_release"),
)

# Best-effort timestamp prefix (ISO-8601-ish or RFC 3339); empty if absent.
_TIMESTAMP_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ][\d:.+Z\-]+)\s")


def extract_from_log(stream: Iterable[str]) -> list[MissEvent]:
    """Parse a log stream and return one ``MissEvent`` per matching line."""
    events: list[MissEvent] = []
    for line in stream:
        timestamp = ""
        m = _TIMESTAMP_PREFIX.match(line)
        if m:
            timestamp = m.group(1)
        for pattern, method in _LOG_PATTERNS:
            hit = pattern.search(line)
            if hit:
                release_id = int(hit.group(1))
                events.append(MissEvent(release_id, method, timestamp))
                break
    return events


def extract_from_ids_csv(path: Path) -> list[MissEvent]:
    """Read a CSV with ``release_id`` and ``method`` columns."""
    events: list[MissEvent] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                release_id = int(row["release_id"])
            except (KeyError, ValueError):
                continue
            method = row.get("method", "")
            timestamp = row.get("timestamp", "")
            events.append(MissEvent(release_id, method, timestamp))
    return events


def classify(conn: psycopg.Connection, events: list[MissEvent]) -> list[dict[str, object]]:
    """For each unique release_id, query PG for existence + release_track count.

    The query is one round-trip with an ``= ANY`` filter — cheap even for
    a few hundred IDs.

    Returns one classified row per input event (in input order).
    """
    if not events:
        return []

    unique_ids = sorted({e.release_id for e in events})
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, count(rt.release_id) AS release_track_count
            FROM release r
            LEFT JOIN release_track rt ON rt.release_id = r.id
            WHERE r.id = ANY(%s)
            GROUP BY r.id
            """,
            (unique_ids,),
        )
        # Maps release_id -> release_track_count. Absent key = row didn't exist.
        present: dict[int, int] = {row[0]: row[1] for row in cur.fetchall()}

    # First-occurrence index per release_id, for the "prior_seen_in_window" flag.
    first_seen: dict[int, int] = {}
    for idx, event in enumerate(events):
        first_seen.setdefault(event.release_id, idx)

    classified: list[dict[str, object]] = []
    for idx, event in enumerate(events):
        existed = event.release_id in present
        track_count = present.get(event.release_id, 0)
        prior = first_seen[event.release_id] < idx
        classified.append(
            {
                "timestamp": event.timestamp,
                "method": event.method,
                "release_id": event.release_id,
                "release_existed": existed,
                "release_track_count": track_count,
                "prior_seen_in_window": prior,
            }
        )
    return classified


def emit_csv(rows: list[dict[str, object]], out) -> None:
    fields = [
        "timestamp",
        "method",
        "release_id",
        "release_existed",
        "release_track_count",
        "prior_seen_in_window",
    ]
    w = csv.DictWriter(out, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)


def print_summary(rows: list[dict[str, object]]) -> None:
    total = len(rows)
    if not total:
        print("(no events extracted — try expanding --log patterns or use --ids)")
        return
    existed = sum(1 for r in rows if r["release_existed"])
    with_tracks = sum(1 for r in rows if r["release_existed"] and r["release_track_count"])
    prior_seen = sum(1 for r in rows if r["prior_seen_in_window"])

    def pct(n: int) -> str:
        return f"({n / total:.1%})"

    print(f"events:                                        {total:>5}")
    print(f"  release row existed at miss time:            {existed:>5} {pct(existed)}")
    print(f"    ... with release_track count > 0:          {with_tracks:>5} {pct(with_tracks)}")
    print(f"  release_id repeated within window:           {prior_seen:>5} {pct(prior_seen)}")
    print()
    print("Interpretation per LML#537:")
    print("  existed=False                  → first-time miss (H1 — ETL coverage gap)")
    print("  existed=True, track_count=0    → first-time miss after artist-only seed (still H1)")
    print(
        "  existed=True, track_count>0    → release was already known with tracks "
        "(H3' — validate returned False as a hit)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        type=str,
        help="Path to a log file (use '-' for stdin). Mutually exclusive with --ids.",
    )
    parser.add_argument(
        "--ids",
        type=Path,
        help="CSV with release_id,method[,timestamp] columns. Mutually exclusive with --log.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Random sample size from the extracted events (default: 50). 0 = keep all.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="RNG seed for the --limit sample (default: nondeterministic).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("/tmp/lml-537-cache-miss-provenance.csv"),
        help="Output CSV path (default: /tmp/lml-537-cache-miss-provenance.csv).",
    )
    args = parser.parse_args(argv)

    if not args.log and not args.ids:
        print("error: pass either --log or --ids", file=sys.stderr)
        return 2
    if args.log and args.ids:
        print("error: --log and --ids are mutually exclusive", file=sys.stderr)
        return 2

    dsn = os.environ.get("DATABASE_URL_DISCOGS")
    if not dsn:
        print("error: DATABASE_URL_DISCOGS env var required", file=sys.stderr)
        return 2

    if args.log:
        if args.log == "-":
            events = extract_from_log(sys.stdin)
        else:
            with Path(args.log).open() as f:
                events = extract_from_log(f)
    else:
        events = extract_from_ids_csv(args.ids)

    if not events:
        print("error: no events extracted from input", file=sys.stderr)
        print("  log patterns tried:", file=sys.stderr)
        for pattern, method in _LOG_PATTERNS:
            print(f"    {method}: {pattern.pattern}", file=sys.stderr)
        return 1

    if args.limit and len(events) > args.limit:
        rng = random.Random(args.seed)
        events = rng.sample(events, args.limit)

    import psycopg

    with psycopg.connect(dsn) as conn:
        classified = classify(conn, events)

    with args.out.open("w", newline="") as f:
        emit_csv(classified, f)

    print_summary(classified)
    print()
    print(f"CSV: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
