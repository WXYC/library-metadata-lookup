#!/usr/bin/env python3
"""
Export library catalog from MySQL to SQLite.

Usage:
    python scripts/export_to_sqlite.py

Connects to a remote MySQL database via SSH and exports the library
catalog to a local SQLite database with FTS5 full-text search support.

Required environment variables:
    LIBRARY_SSH_HOST    - SSH host to connect to
    LIBRARY_SSH_USER    - SSH username
    LIBRARY_DB_HOST     - MySQL host (as seen from SSH host)
    LIBRARY_DB_USER     - MySQL username
    LIBRARY_DB_PASSWORD - MySQL password
    LIBRARY_DB_NAME     - MySQL database name
"""

import itertools
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

# Configuration from environment
SSH_HOST = os.environ.get("LIBRARY_SSH_HOST", "")
SSH_USER = os.environ.get("LIBRARY_SSH_USER", "")
MYSQL_HOST = os.environ.get("LIBRARY_DB_HOST", "localhost")
MYSQL_USER = os.environ.get("LIBRARY_DB_USER", "root")
MYSQL_PASSWORD = os.environ.get("LIBRARY_DB_PASSWORD", "")
MYSQL_DATABASE = os.environ.get("LIBRARY_DB_NAME", "wxyc_library")

# Output path (override with LIBRARY_DB_OUTPUT_PATH env var)
OUTPUT_PATH = Path(os.environ.get("LIBRARY_DB_OUTPUT_PATH", "")) or (
    Path(__file__).parent.parent / "library.db"
)

# SQL query to extract library data
LIBRARY_QUERY = """
SELECT
    r.ID as id,
    r.TITLE as title,
    lc.PRESENTATION_NAME as artist,
    lc.CALL_LETTERS as call_letters,
    lc.CALL_NUMBERS as artist_call_number,
    r.CALL_NUMBERS as release_call_number,
    g.REFERENCE_NAME as genre,
    f.REFERENCE_NAME as format,
    r.ALTERNATE_ARTIST_NAME as alternate_artist_name
FROM LIBRARY_RELEASE r
JOIN LIBRARY_CODE lc ON r.LIBRARY_CODE_ID = lc.ID
JOIN FORMAT f ON r.FORMAT_ID = f.ID
JOIN GENRE g ON lc.GENRE_ID = g.ID
"""


def format_size(size_bytes: int) -> str:
    """Format byte size as human-readable string."""
    size_float = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size_float < 1024:
            return f"{size_float:.1f} {unit}"
        size_float /= 1024
    return f"{size_float:.1f} TB"


def fetch_from_remote() -> list[dict]:
    """Fetch library data by running MySQL query on remote host via SSH."""
    ssh_target = f"{SSH_USER}@{SSH_HOST}"

    # Build mysql command to run on remote host
    # Use -B for batch mode (tab-separated), -N to skip column names
    mysql_cmd = (
        f"mysql -h {MYSQL_HOST} -u {MYSQL_USER} -p'{MYSQL_PASSWORD}' "
        f'-B -N {MYSQL_DATABASE} -e "{LIBRARY_QUERY}"'
    )

    print(f"Fetching data from {ssh_target}...", end="", flush=True)

    # Start spinner in background
    stop_spinner = threading.Event()

    def spin():
        for char in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            if stop_spinner.is_set():
                break
            print(f"\rFetching data from {ssh_target}... {char}", end="", flush=True)
            time.sleep(0.1)

    spinner = threading.Thread(target=spin)
    spinner.start()

    start_time = time.time()
    result = subprocess.run(
        ["ssh", ssh_target, mysql_cmd],
        capture_output=True,
    )
    elapsed = time.time() - start_time

    stop_spinner.set()
    spinner.join()

    if result.returncode != 0:
        print()  # Clear spinner line
        raise RuntimeError(f"MySQL query failed: {result.stderr.decode('utf-8', errors='replace')}")

    size = len(result.stdout)

    # Decode output - MySQL 5.1 may use latin-1
    try:
        output = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        output = result.stdout.decode("latin-1")

    # Parse tab-separated output
    rows = []
    columns = [
        "id",
        "title",
        "artist",
        "call_letters",
        "artist_call_number",
        "release_call_number",
        "genre",
        "format",
        "alternate_artist_name",
    ]

    for line in output.strip().split("\n"):
        if not line:
            continue
        values = line.split("\t")
        if len(values) == len(columns):
            # MySQL batch mode renders NULL as \N
            cleaned = [None if v == "\\N" else v for v in values]
            rows.append(dict(zip(columns, cleaned, strict=True)))

    print(f"\rFetched {len(rows):,} rows ({format_size(size)}) in {elapsed:.1f}s" + " " * 10)
    return rows


def export():
    # Check required environment variables when using SSH
    if SSH_HOST:
        missing = []
        for var in [
            "LIBRARY_SSH_HOST",
            "LIBRARY_SSH_USER",
            "LIBRARY_DB_HOST",
            "LIBRARY_DB_USER",
            "LIBRARY_DB_PASSWORD",
            "LIBRARY_DB_NAME",
        ]:
            if not os.environ.get(var):
                missing.append(var)
        if missing:
            print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
            sys.exit(1)

        rows = fetch_from_remote()
    else:
        # Local MySQL connection (legacy support)
        import pymysql  # type: ignore[import-untyped]

        print(f"Connecting to MySQL ({MYSQL_DATABASE}) directly...")
        conn = pymysql.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
            charset="utf8",
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cursor:
            cursor.execute(LIBRARY_QUERY)
            rows = cursor.fetchall()
        conn.close()

    _do_export(rows)


def _do_export(rows: list[dict]):
    """Export rows to SQLite database."""
    # Remove existing SQLite file
    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()
        print(f"Removed existing {OUTPUT_PATH}")

    print(f"Creating SQLite database at {OUTPUT_PATH}...")
    sqlite_conn = sqlite3.connect(OUTPUT_PATH)
    sqlite_cur = sqlite_conn.cursor()

    # Create main table
    sqlite_cur.execute("""
        CREATE TABLE library (
            id INTEGER PRIMARY KEY,
            title TEXT,
            artist TEXT,
            call_letters TEXT,
            artist_call_number INTEGER,
            release_call_number INTEGER,
            genre TEXT,
            format TEXT,
            alternate_artist_name TEXT
        )
    """)

    # Create FTS5 virtual table for full-text search
    sqlite_cur.execute("""
        CREATE VIRTUAL TABLE library_fts USING fts5(
            title,
            artist,
            alternate_artist_name,
            content='library',
            content_rowid='id'
        )
    """)

    print(f"Exporting {len(rows)} rows...")

    # Insert into SQLite
    for row in rows:
        sqlite_cur.execute(
            """
            INSERT INTO library (id, title, artist, call_letters, artist_call_number, release_call_number, genre, format, alternate_artist_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["title"],
                row["artist"],
                row["call_letters"],
                row["artist_call_number"],
                row["release_call_number"],
                row["genre"],
                row["format"],
                row.get("alternate_artist_name"),
            ),
        )

    # Populate FTS index
    print("Building full-text search index...")
    sqlite_cur.execute("""
        INSERT INTO library_fts(rowid, title, artist, alternate_artist_name)
        SELECT id, title, artist, alternate_artist_name FROM library
    """)

    # Create additional indexes for filtered searches
    sqlite_cur.execute("CREATE INDEX idx_artist ON library(artist)")
    sqlite_cur.execute("CREATE INDEX idx_title ON library(title)")
    sqlite_cur.execute("CREATE INDEX idx_alternate_artist ON library(alternate_artist_name)")

    sqlite_conn.commit()

    # Verify
    sqlite_cur.execute("SELECT COUNT(*) FROM library")
    count = sqlite_cur.fetchone()[0]
    print(f"Exported {count} rows to {OUTPUT_PATH}")

    # Test FTS
    sqlite_cur.execute("""
        SELECT l.* FROM library l
        JOIN library_fts fts ON l.id = fts.rowid
        WHERE library_fts MATCH 'stone roses'
        LIMIT 3
    """)
    test_results = sqlite_cur.fetchall()
    print(f"FTS test query 'stone roses' returned {len(test_results)} results")

    # File size
    size_mb = OUTPUT_PATH.stat().st_size / (1024 * 1024)
    print(f"Database size: {size_mb:.2f} MB")

    sqlite_conn.close()
    print("Done!")


if __name__ == "__main__":
    export()
