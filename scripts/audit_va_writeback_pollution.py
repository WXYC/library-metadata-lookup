"""Audit entity.identity for V/A writeback pollution accumulated before LML#379's guard.

LML#379 added an `is_compilation_artist(canonical.artist)` check to release/orchestrator._writeback_identity (currently CLOSED). This script quantifies how many rows in entity.identity were written *before* that guard, classifies them via the wxyc-etl 0.5.0 anchored matcher (post #129 + #130), and outputs the cleanup candidates.

The substring regex below is intentionally a SUPERSET that catches every row the pre-0.5.0 substring matcher could have flagged as compilation. Each candidate is then re-classified with the current `is_compilation_artist`:

  - kept_real_artist: NEW matcher returns False — "The Soundtrack of Our Lives", "Various Production", etc. These are real artists that the OLD substring matcher would have rejected on the read side but should never have been in entity.identity if they came through writeback (none would have, since OLD matcher would have caught them too — so these are mostly clean already).
  - delete_compilation: NEW matcher returns True — confirmed pollution. Candidate for DELETE on entity.identity + entity.reconciliation_log.

Usage:
    DATABASE_URL_DISCOGS=postgresql://... python scripts/audit_va_writeback_pollution.py [--apply]

By default emits counts + writes per-row CSVs to /tmp/audit-lml-379-{delete,keep}.csv.
Pass --apply to execute the DELETE statements; review the dry-run CSVs first.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from wxyc_etl.text import is_compilation_artist

if TYPE_CHECKING:
    import psycopg

SUPERSET_REGEX = r"(various|soundtrack|compilation|v/a|v\.a\.)"

OUT_DIR = Path("/tmp")
DELETE_CSV = OUT_DIR / "audit-lml-379-delete.csv"
KEEP_CSV = OUT_DIR / "audit-lml-379-keep.csv"


def fetch_candidates(conn: psycopg.Connection) -> list[tuple[int, str]]:
    """All entity.identity rows whose library_name matches the V/A superset regex."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, library_name
            FROM entity.identity
            WHERE lower(library_name) ~ %s
            ORDER BY id
            """,
            (SUPERSET_REGEX,),
        )
        return cur.fetchall()


def classify(rows: list[tuple[int, str]]) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Split rows into (delete_compilation, kept_real_artist) via wxyc-etl 0.5.0 matcher."""
    delete: list[tuple[int, str]] = []
    keep: list[tuple[int, str]] = []
    for row_id, name in rows:
        if is_compilation_artist(name):
            delete.append((row_id, name))
        else:
            keep.append((row_id, name))
    return delete, keep


def write_csv(path: Path, rows: list[tuple[int, str]]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "library_name"])
        w.writerows(rows)


def count_reconciliation_log(conn: psycopg.Connection, ids: list[int]) -> int:
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM entity.reconciliation_log WHERE identity_id = ANY(%s)",
            (ids,),
        )
        return cur.fetchone()[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute DELETE statements (default: dry-run with CSV output only)",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL_DISCOGS")
    if not dsn:
        print("error: DATABASE_URL_DISCOGS env var required", file=sys.stderr)
        return 2

    import psycopg  # lazy: keeps `classify()` importable in tests without the DB driver

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM entity.identity")
            total = cur.fetchone()[0]

        candidates = fetch_candidates(conn)
        delete, keep = classify(candidates)
        delete_ids = [r[0] for r in delete]
        recon_count = count_reconciliation_log(conn, delete_ids)

        write_csv(DELETE_CSV, delete)
        write_csv(KEEP_CSV, keep)

        print(f"entity.identity total rows:                 {total:>8,}")
        print(f"superset regex hits (audit candidates):     {len(candidates):>8,}")
        print(f"  → delete (NEW matcher = compilation):     {len(delete):>8,}")
        print(f"  → keep   (NEW matcher = real artist):     {len(keep):>8,}")
        print(f"entity.reconciliation_log rows joined:      {recon_count:>8,}")
        print()
        print("CSV outputs:")
        print(f"  {DELETE_CSV}  ({len(delete)} rows — review before --apply)")
        print(f"  {KEEP_CSV}    ({len(keep)} rows — false-positive control set)")

        if args.apply:
            if not delete_ids:
                print("\nNothing to delete.")
                return 0
            print(
                f"\nApplying DELETE for {len(delete_ids)} identity rows + {recon_count} reconciliation_log rows..."
            )
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM entity.reconciliation_log WHERE identity_id = ANY(%s)",
                    (delete_ids,),
                )
                recon_deleted = cur.rowcount
                cur.execute(
                    "DELETE FROM entity.identity WHERE id = ANY(%s)",
                    (delete_ids,),
                )
                identity_deleted = cur.rowcount
            conn.commit()
            print(f"  deleted {identity_deleted} entity.identity rows")
            print(f"  deleted {recon_deleted} entity.reconciliation_log rows")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
