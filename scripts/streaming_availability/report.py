"""CSV report generation for streaming availability analysis."""

from __future__ import annotations

import csv
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.streaming_availability.results_db import ResultsDB


async def generate_csv_report(results_db: ResultsDB, output_path: str) -> dict:
    """Generate a CSV report of albums not available on any streaming platform.

    Returns the stats dict from the results database.
    """
    rows = await results_db.get_not_on_streaming()
    stats = await results_db.get_stats()

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "artist",
                "title",
                "genre",
                "label",
                "formats",
                "library_ids",
                "spotify_status",
                "apple_status",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "artist": row["display_artist"],
                    "title": row["display_title"],
                    "genre": row["genre"] or "",
                    "label": row["label"] or "",
                    "formats": row["formats"],
                    "library_ids": row["library_ids"],
                    "spotify_status": row["spotify_status"],
                    "apple_status": row["apple_status"],
                }
            )

    return stats


async def generate_summary(results_db: ResultsDB) -> str:
    """Generate a human-readable summary string for console output."""
    stats = await results_db.get_stats()
    total = stats["total"]
    if total == 0:
        return "No albums in database."

    spotify = stats.get("spotify", {})
    apple = stats.get("apple", {})

    lines = [
        "",
        f"  Total albums: {total}",
        "",
        "  Spotify:",
    ]
    for status in ("found", "not_found", "skipped", "error", "pending"):
        count = spotify.get(status, 0)
        if count:
            pct = count / total * 100
            lines.append(f"    {status}: {count} ({pct:.1f}%)")

    lines.append("")
    lines.append("  Apple Music:")
    for status in ("found", "not_found", "skipped", "error", "pending"):
        count = apple.get(status, 0)
        if count:
            pct = count / total * 100
            lines.append(f"    {status}: {count} ({pct:.1f}%)")

    # Not on either
    not_on_streaming = await results_db.get_not_on_streaming()
    neither_count = len(not_on_streaming)
    if neither_count:
        pct = neither_count / total * 100
        lines.append("")
        lines.append(f"  Not on either platform: {neither_count} ({pct:.1f}%)")

    return "\n".join(lines)
