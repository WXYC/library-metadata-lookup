"""Generate reviewable SQL UPDATE statements for both tables.

Produces two SQL files:
- Flowsheet updates (FLOWSHEET_ENTRY_PROD: ARTIST_NAME + ARTIST_ID)
- Catalog updates (LIBRARY_RELEASE: ALTERNATE_ARTIST_NAME)
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _escape(value: str) -> str:
    """Escape single quotes for MySQL string literals."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def generate_flowsheet_updates(rows: list[dict], *, batch_size: int = 500) -> list[str]:
    """Generate UPDATE statements for FLOWSHEET_ENTRY_PROD.

    Args:
        rows: List of dicts with keys: id, resolved_artist, library_code_id,
              confidence, strategy.
        batch_size: Number of statements per batch comment group.

    Returns:
        List of SQL lines.
    """
    if not rows:
        return []

    lines: list[str] = []
    lines.append("-- VA Disambiguation: FLOWSHEET_ENTRY_PROD updates")
    lines.append(f"-- Total: {len(rows)} entries")
    lines.append("")

    for i, row in enumerate(rows):
        if i % batch_size == 0:
            batch_num = i // batch_size + 1
            batch_end = min(i + batch_size, len(rows))
            lines.append(f"-- Batch {batch_num}: entries {i + 1}-{batch_end}")

        artist = _escape(row["resolved_artist"])
        code_id = row.get("library_code_id")
        strategy = row.get("strategy", "unknown")
        confidence = row.get("confidence", 0)

        if code_id is not None:
            lines.append(
                f"UPDATE FLOWSHEET_ENTRY_PROD SET ARTIST_NAME = '{artist}', "
                f"ARTIST_ID = {code_id} WHERE ID = {row['id']}; "
                f"-- {strategy} ({confidence:.2f})"
            )
        else:
            lines.append(
                f"UPDATE FLOWSHEET_ENTRY_PROD SET ARTIST_NAME = '{artist}' "
                f"WHERE ID = {row['id']}; "
                f"-- {strategy} ({confidence:.2f})"
            )

    return lines


def generate_catalog_inserts(rows: list[dict], *, batch_size: int = 500) -> list[str]:
    """Generate INSERT statements for COMPILATION_TRACK_ARTIST.

    Args:
        rows: List of dicts with keys: library_release_id, artist_name, track_title.
        batch_size: Number of statements per batch comment group.

    Returns:
        List of SQL lines.
    """
    if not rows:
        return []

    lines: list[str] = []
    lines.append("-- VA Disambiguation: COMPILATION_TRACK_ARTIST inserts")
    lines.append(f"-- Total: {len(rows)} track-artist rows")
    lines.append("")

    for i, row in enumerate(rows):
        if i % batch_size == 0:
            batch_num = i // batch_size + 1
            batch_end = min(i + batch_size, len(rows))
            lines.append(f"-- Batch {batch_num}: rows {i + 1}-{batch_end}")

        artist = _escape(row["artist_name"])
        track = _escape(row["track_title"]) if row.get("track_title") else None
        if track:
            lines.append(
                f"INSERT INTO COMPILATION_TRACK_ARTIST "
                f"(LIBRARY_RELEASE_ID, ARTIST_NAME, TRACK_TITLE) "
                f"VALUES ({row['library_release_id']}, '{artist}', '{track}');"
            )
        else:
            lines.append(
                f"INSERT INTO COMPILATION_TRACK_ARTIST "
                f"(LIBRARY_RELEASE_ID, ARTIST_NAME) "
                f"VALUES ({row['library_release_id']}, '{artist}');"
            )

    return lines


def write_sql_file(lines: list[str], path: Path) -> None:
    """Write SQL lines to a file."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"Wrote {len(lines)} lines to {path}")
