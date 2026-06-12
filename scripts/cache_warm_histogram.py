"""LML#537 probe #2 — Dynamic-write contribution histogram.

Counts ``release`` rows by the day they were stamped with
``artwork_checked_at``. Separates the ETL load (large early-day buckets,
populated when ``discogs-etl`` first ran) from API-driven warm-up
(post-Alembic-0009 daily delta — i.e., the dynamic cache fill that has
been working since 2026-06-07 when LML#534's PK fix landed).

If the post-2026-06-07 daily mean is small relative to the ETL load, the
warm-up channel is structurally limited — confirming H1 from #537.

Usage::

    DATABASE_URL_DISCOGS=postgresql://... python scripts/cache_warm_histogram.py
    DATABASE_URL_DISCOGS=postgresql://... python scripts/cache_warm_histogram.py --csv
    DATABASE_URL_DISCOGS=postgresql://... python scripts/cache_warm_histogram.py --since 2026-06-07

Read-only; one query. No state is touched.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

# The deploy that fixed dynamic write-back. See LML#537 issue body and
# discogs-etl alembic 0009 (cache_metadata_unique). Used as the default
# split point for "before" (ETL fill) vs "after" (dynamic warm-up) buckets.
_DEFAULT_CUTOFF = dt.date(2026, 6, 7)


def fetch_histogram(conn: psycopg.Connection) -> list[tuple[dt.date, int]]:
    """All non-null ``artwork_checked_at`` rows, bucketed by day."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                date_trunc('day', artwork_checked_at)::date AS day,
                count(*) AS rows
            FROM release
            WHERE artwork_checked_at IS NOT NULL
            GROUP BY 1
            ORDER BY 1
            """
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def summarise(rows: list[tuple[dt.date, int]], cutoff: dt.date) -> dict[str, float | int]:
    """Compute totals + before/after means around ``cutoff``."""
    if not rows:
        return {
            "total_rows": 0,
            "days_before": 0,
            "days_after": 0,
            "rows_before": 0,
            "rows_after": 0,
            "mean_before": 0.0,
            "mean_after": 0.0,
        }

    rows_before = sum(c for d, c in rows if d < cutoff)
    rows_after = sum(c for d, c in rows if d >= cutoff)
    days_before = sum(1 for d, _ in rows if d < cutoff)
    days_after = sum(1 for d, _ in rows if d >= cutoff)

    return {
        "total_rows": rows_before + rows_after,
        "days_before": days_before,
        "days_after": days_after,
        "rows_before": rows_before,
        "rows_after": rows_after,
        "mean_before": rows_before / days_before if days_before else 0.0,
        "mean_after": rows_after / days_after if days_after else 0.0,
    }


def print_table(rows: list[tuple[dt.date, int]], cutoff: dt.date) -> None:
    if not rows:
        print("(no rows with artwork_checked_at IS NOT NULL)")
        return
    max_count = max(c for _, c in rows)
    width = len(f"{max_count:,}")
    print(f"{'day':<12} {'rows':>{width}}  marker")
    for d, c in rows:
        marker = "POST" if d >= cutoff else ""
        print(f"{d.isoformat():<12} {c:>{width},}  {marker}")


def print_summary(summary: dict[str, float | int], cutoff: dt.date) -> None:
    print()
    print(f"total rows (artwork_checked_at not null): {summary['total_rows']:>10,}")
    print(f"cutoff:                                   {cutoff.isoformat()}")
    print(
        f"  before ({summary['days_before']} days):"
        f"                          {summary['rows_before']:>10,}"
        f"  (mean {summary['mean_before']:.1f}/day)"
    )
    print(
        f"  on/after ({summary['days_after']} days):"
        f"                        {summary['rows_after']:>10,}"
        f"  (mean {summary['mean_after']:.1f}/day)"
    )
    if summary["mean_after"] and summary["mean_before"]:
        ratio = summary["mean_after"] / summary["mean_before"]
        print(f"  post-cutoff / pre-cutoff daily-mean ratio: {ratio:.3f}")


def emit_csv(rows: list[tuple[dt.date, int]], out) -> None:
    w = csv.writer(out)
    w.writerow(["day", "rows"])
    for d, c in rows:
        w.writerow([d.isoformat(), c])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=dt.date.fromisoformat,
        default=_DEFAULT_CUTOFF,
        help=(
            "Cutoff date (YYYY-MM-DD) that splits pre/post-deploy buckets. "
            f"Default: {_DEFAULT_CUTOFF.isoformat()} (Alembic 0009 deploy)."
        ),
    )
    parser.add_argument("--csv", action="store_true", help="Emit CSV to stdout instead of a table")
    args = parser.parse_args(argv)

    dsn = os.environ.get("DATABASE_URL_DISCOGS")
    if not dsn:
        print("error: DATABASE_URL_DISCOGS env var required", file=sys.stderr)
        return 2

    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = fetch_histogram(conn)

    if args.csv:
        emit_csv(rows, sys.stdout)
        return 0

    print_table(rows, args.since)
    print_summary(summarise(rows, args.since), args.since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
