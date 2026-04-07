"""Extract data from the tubafrenzy MySQL database via SSH.

Follows the subprocess SSH pattern from discogs-cache/scripts/export_to_sqlite.py.
Fetches VA flowsheet entries, LIBRARY_CODE, and compilation LIBRARY_RELEASE rows.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

from scripts.va_disambiguate.models import (
    CompilationRelease,
    FlowsheetEntry,
    LibraryCodeEntry,
)

logger = logging.getLogger(__name__)

SSH_HOST = os.environ.get("TUBAFRENZY_SSH_HOST", "horus.kattare.com")
SSH_USER = os.environ.get("TUBAFRENZY_SSH_USER", "tubafrenzy")
MYSQL_HOST = os.environ.get("TUBAFRENZY_DB_HOST", "dc1-mysql-01.kattare.com")
MYSQL_USER = os.environ.get("TUBAFRENZY_DB_USER", "tubafrenzy")
MYSQL_PASSWORD = os.environ.get("TUBAFRENZY_DB_PASSWORD", "")
MYSQL_DATABASE = os.environ.get("TUBAFRENZY_DB_NAME", "wxycmusic")

_VA_FLOWSHEET_QUERY = """
SELECT ID, ARTIST_NAME, SONG_TITLE, RELEASE_TITLE, LIBRARY_RELEASE_ID, ARTIST_ID
FROM FLOWSHEET_ENTRY_PROD
WHERE (
    LOWER(ARTIST_NAME) LIKE '%various%'
    OR LOWER(ARTIST_NAME) IN ('v/a', 'v.a.', 'v.a')
)
AND FLOWSHEET_ENTRY_TYPE_CODE_ID < 7
"""

_LIBRARY_CODE_QUERY = """
SELECT ID, PRESENTATION_NAME, CALL_LETTERS, GENRE_ID
FROM LIBRARY_CODE
"""

_COMPILATION_RELEASE_QUERY = """
SELECT lr.ID, lr.TITLE, lr.LIBRARY_CODE_ID, lr.ALTERNATE_ARTIST_NAME
FROM LIBRARY_RELEASE lr
JOIN LIBRARY_CODE lc ON lr.LIBRARY_CODE_ID = lc.ID
WHERE lc.CALL_LETTERS LIKE 'Z-%'
AND (lr.ALTERNATE_ARTIST_NAME IS NULL OR lr.ALTERNATE_ARTIST_NAME = '')
"""


def _run_query(query: str, columns: list[str]) -> list[dict]:
    """Run a MySQL query on the remote host via SSH and parse tab-separated results."""
    if not MYSQL_PASSWORD:
        raise RuntimeError("TUBAFRENZY_DB_PASSWORD not set. Export it or add to .env.")

    ssh_target = f"{SSH_USER}@{SSH_HOST}"
    mysql_cmd = (
        f"mysql -h {MYSQL_HOST} -u {MYSQL_USER} -p'{MYSQL_PASSWORD}' "
        f'-B -N {MYSQL_DATABASE} -e "{query}"'
    )

    logger.info(f"Fetching data from {ssh_target}...")
    start = time.time()

    result = subprocess.run(
        ["ssh", ssh_target, mysql_cmd],
        capture_output=True,
    )
    elapsed = time.time() - start

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"MySQL query failed ({elapsed:.1f}s): {stderr}")

    # Decode — MySQL 5.1 may use latin-1
    try:
        output = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        output = result.stdout.decode("latin-1")

    rows = []
    for line in output.strip().split("\n"):
        if not line:
            continue
        values = line.split("\t")
        if len(values) >= len(columns):
            cleaned = [None if v == "\\N" else v for v in values[: len(columns)]]
            rows.append(dict(zip(columns, cleaned, strict=True)))

    logger.info(f"Fetched {len(rows):,} rows in {elapsed:.1f}s")
    return rows


def fetch_va_entries() -> list[FlowsheetEntry]:
    """Fetch all Various Artists flowsheet entries from MySQL."""
    columns = [
        "id",
        "artist_name",
        "song_title",
        "release_title",
        "library_release_id",
        "artist_id",
    ]
    rows = _run_query(_VA_FLOWSHEET_QUERY, columns)
    entries = []
    for row in rows:
        entries.append(
            FlowsheetEntry(
                id=int(row["id"]),
                artist_name=row["artist_name"] or "",
                song_title=row["song_title"],
                release_title=row["release_title"],
                library_release_id=int(row["library_release_id"])
                if row["library_release_id"]
                else None,
                artist_id=int(row["artist_id"]) if row["artist_id"] else None,
            )
        )
    return entries


def fetch_library_codes() -> list[LibraryCodeEntry]:
    """Fetch all LIBRARY_CODE entries from MySQL."""
    columns = ["id", "presentation_name", "call_letters", "genre_id"]
    rows = _run_query(_LIBRARY_CODE_QUERY, columns)
    return [
        LibraryCodeEntry(
            id=int(row["id"]),
            presentation_name=row["presentation_name"] or "",
            call_letters=row["call_letters"] or "",
            genre_id=int(row["genre_id"]) if row["genre_id"] else None,
        )
        for row in rows
    ]


def fetch_compilation_releases() -> list[CompilationRelease]:
    """Fetch compilation LIBRARY_RELEASE entries missing ALTERNATE_ARTIST_NAME."""
    columns = ["id", "title", "library_code_id", "alternate_artist_name"]
    rows = _run_query(_COMPILATION_RELEASE_QUERY, columns)
    return [
        CompilationRelease(
            id=int(row["id"]),
            title=row["title"] or "",
            library_code_id=int(row["library_code_id"]),
            alternate_artist_name=row["alternate_artist_name"],
        )
        for row in rows
    ]


def apply_sql_file(sql_path: str) -> None:
    """Execute a SQL file on the remote MySQL server via SSH."""
    if not MYSQL_PASSWORD:
        raise RuntimeError("TUBAFRENZY_DB_PASSWORD not set.")

    ssh_target = f"{SSH_USER}@{SSH_HOST}"
    mysql_cmd = f"mysql -h {MYSQL_HOST} -u {MYSQL_USER} -p'{MYSQL_PASSWORD}' {MYSQL_DATABASE}"

    logger.info(f"Applying {sql_path} via {ssh_target}...")
    start = time.time()

    with open(sql_path) as f:
        sql_content = f.read()

    result = subprocess.run(
        ["ssh", ssh_target, mysql_cmd],
        input=sql_content.encode("utf-8"),
        capture_output=True,
    )
    elapsed = time.time() - start

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"SQL execution failed ({elapsed:.1f}s): {stderr}")

    logger.info(f"Applied {sql_path} in {elapsed:.1f}s")
