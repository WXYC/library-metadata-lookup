"""Regenerate stat values inside `streaming_analysis_report.md`.

The report contains marker-delimited spans like:

    <!-- gen:KEY -->VALUE<!-- /gen -->

Each KEY is bound to a query in `QUERIES` below. When run, this script
executes every query, swaps the new value into each marker, and writes
the file back. It errors loudly on unknown or unresolved markers so the
report and the registry can never silently drift apart.

Usage:
    uv run python -m scripts.regenerate_report_stats [--dry-run]

Required env:
    streaming_availability.db on disk at repo root
    TUBAFRENZY_SSH_HOST / TUBAFRENZY_DB_* for the MySQL queries
        (omit to skip MySQL-backed markers and warn instead)
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = REPO_ROOT / "streaming_analysis_report.md"
SQLITE_PATH = REPO_ROOT / "streaming_availability.db"

MARKER_RE = re.compile(
    r"<!-- gen:(?P<key>[\w.-]+) -->(?P<value>.*?)<!-- /gen -->",
    re.DOTALL,
)


class UnknownMarkerError(KeyError):
    """A registered key has no corresponding marker in the doc."""


class UnresolvedMarkerError(KeyError):
    """A marker in the doc has no corresponding registry entry."""


def substitute_markers(text: str, results: dict[str, str]) -> str:
    """Swap each `<!-- gen:KEY -->...<!-- /gen -->` value for `results[KEY]`."""
    seen: set[str] = set()

    def repl(m: re.Match[str]) -> str:
        key = m.group("key")
        seen.add(key)
        if key not in results:
            raise UnresolvedMarkerError(key)
        return f"<!-- gen:{key} -->{results[key]}<!-- /gen -->"

    new_text = MARKER_RE.sub(repl, text)
    extra = set(results) - seen
    if extra:
        raise UnknownMarkerError(", ".join(sorted(extra)))
    return new_text


@dataclass(frozen=True)
class Query:
    """A single registry entry: how to compute one marker's value."""

    source: str  # "sqlite" | "mysql"
    sql: str
    formatter: Callable[[object], str] = staticmethod(lambda v: f"{v:,}")


# Registry of marker keys to queries. Add an entry for every new method that
# produces a number worth tracking; the regenerator will refuse to commit until
# the doc is marked up to consume it.
QUERIES: dict[str, Query] = {
    # Full Catalog
    "catalog.total": Query("sqlite", "SELECT COUNT(*) FROM albums"),
    "catalog.non_compilation": Query(
        "sqlite", "SELECT COUNT(*) FROM albums WHERE is_compilation = 0"
    ),
    "catalog.compilation": Query("sqlite", "SELECT COUNT(*) FROM albums WHERE is_compilation = 1"),
    # Streaming URL Coverage (current persisted state)
    "url.spotify": Query("sqlite", "SELECT COUNT(*) FROM albums WHERE spotify_url IS NOT NULL"),
    "url.deezer": Query("sqlite", "SELECT COUNT(*) FROM albums WHERE deezer_url IS NOT NULL"),
    "url.bandcamp": Query("sqlite", "SELECT COUNT(*) FROM albums WHERE bandcamp_url IS NOT NULL"),
    "url.apple": Query("sqlite", "SELECT COUNT(*) FROM albums WHERE apple_url IS NOT NULL"),
    "url.tidal": Query("sqlite", "SELECT COUNT(*) FROM albums WHERE tidal_url IS NOT NULL"),
    "url.any": Query(
        "sqlite",
        """SELECT COUNT(*) FROM albums
           WHERE spotify_url IS NOT NULL OR deezer_url IS NOT NULL
              OR bandcamp_url IS NOT NULL OR apple_url IS NOT NULL
              OR tidal_url IS NOT NULL""",
    ),
    # Discogs Coverage (non-compilation)
    "discogs.matched_non_comp": Query(
        "sqlite",
        "SELECT COUNT(*) FROM albums WHERE is_compilation = 0 AND discogs_release_id IS NOT NULL",
    ),
    # ON_STREAMING in Production (live MySQL)
    "on_streaming.yes": Query(
        "mysql", "SELECT COUNT(*) FROM LIBRARY_RELEASE WHERE ON_STREAMING = 1"
    ),
    "on_streaming.no": Query(
        "mysql", "SELECT COUNT(*) FROM LIBRARY_RELEASE WHERE ON_STREAMING = 0"
    ),
    "on_streaming.null": Query(
        "mysql", "SELECT COUNT(*) FROM LIBRARY_RELEASE WHERE ON_STREAMING IS NULL"
    ),
    "on_streaming.total": Query("mysql", "SELECT COUNT(*) FROM LIBRARY_RELEASE"),
}


def run_sqlite_queries(db_path: Path, queries: dict[str, Query]) -> dict[str, str]:
    """Execute all sqlite-source queries against `db_path`."""
    results: dict[str, str] = {}
    if not db_path.exists():
        logger.error("SQLite DB not found at %s — skipping all sqlite queries", db_path)
        return results
    conn = sqlite3.connect(db_path)
    try:
        for key, q in queries.items():
            if q.source != "sqlite":
                continue
            (value,) = conn.execute(q.sql).fetchone()
            results[key] = q.formatter(value)
    finally:
        conn.close()
    return results


def run_mysql_queries(queries: dict[str, Query]) -> dict[str, str]:
    """Execute all mysql-source queries via SSH-tunneled mysql CLI."""
    results: dict[str, str] = {}
    mysql_jobs = {k: q for k, q in queries.items() if q.source == "mysql"}
    if not mysql_jobs:
        return results
    ssh_host = os.environ.get("TUBAFRENZY_SSH_HOST", "horus.kattare.com")
    ssh_user = os.environ.get("TUBAFRENZY_SSH_USER", "tubafrenzy")
    db_host = os.environ.get("TUBAFRENZY_DB_HOST", "dc1-mysql-01.kattare.com")
    db_user = os.environ.get("TUBAFRENZY_DB_USER", "tubafrenzy")
    db_pass = os.environ.get("TUBAFRENZY_DB_PASSWORD", "")
    db_name = os.environ.get("TUBAFRENZY_DB_NAME", "wxycmusic")
    if not db_pass:
        logger.warning(
            "TUBAFRENZY_DB_PASSWORD not set — skipping %d mysql markers (%s)",
            len(mysql_jobs),
            ", ".join(sorted(mysql_jobs)),
        )
        return results
    union_sql = "; ".join(f"SELECT '{k}' AS k, ({q.sql}) AS v" for k, q in mysql_jobs.items())
    mysql_cmd = f"mysql -h {db_host} -u {db_user} -p'{db_pass}' -B -N {db_name} -e \"{union_sql}\""
    proc = subprocess.run(
        ["ssh", f"{ssh_user}@{ssh_host}", mysql_cmd],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"mysql query failed: {proc.stderr.decode('utf-8', 'replace')}")
    output = proc.stdout.decode("utf-8", "replace").strip()
    for line in output.splitlines():
        if "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        if key in mysql_jobs:
            results[key] = mysql_jobs[key].formatter(int(value))
    return results


def regenerate(report_path: Path, results: dict[str, str], *, dry_run: bool) -> str:
    text = report_path.read_text()
    new_text = substitute_markers(text, results)
    if dry_run:
        logger.info("Dry run: would update %d markers in %s", len(results), report_path)
    elif new_text != text:
        report_path.write_text(new_text)
        logger.info("Updated %d markers in %s", len(results), report_path)
    else:
        logger.info("No changes needed in %s", report_path)
    return new_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--sqlite-db", type=Path, default=SQLITE_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    results = {
        **run_sqlite_queries(args.sqlite_db, QUERIES),
        **run_mysql_queries(QUERIES),
    }
    try:
        regenerate(args.report, results, dry_run=args.dry_run)
    except (UnknownMarkerError, UnresolvedMarkerError) as e:
        logger.error("Marker/registry mismatch: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
