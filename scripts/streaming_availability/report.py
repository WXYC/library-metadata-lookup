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
                "deezer_status",
                "spotify_status",
                "discogs_artist",
                "discogs_title",
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
                    "deezer_status": row["deezer_status"],
                    "spotify_status": row["spotify_status"],
                    "discogs_artist": row["discogs_artist"] or "",
                    "discogs_title": row["discogs_title"] or "",
                }
            )

    return stats


async def generate_summary(results_db: ResultsDB) -> str:
    """Generate a human-readable summary string for console output."""
    stats = await results_db.get_stats()
    total = stats["total"]
    if total == 0:
        return "No albums in database."

    discogs = stats.get("discogs", {})
    deezer = stats.get("deezer", {})
    spotify = stats.get("spotify", {})

    non_compilation = total - spotify.get("skipped", 0)

    lines = [
        "",
        f"  Total albums: {total:,} ({non_compilation:,} non-compilation)",
        "",
        "  Discogs enrichment:",
    ]
    d_found = discogs.get("found", 0)
    d_total = d_found + discogs.get("not_found", 0)
    if d_total:
        lines.append(f"    {d_found:,} / {d_total:,} matched ({d_found / d_total * 100:.0f}%)")

    lines.append("")
    lines.append("  Deezer (digitally distributed):")
    dz_found = deezer.get("found", 0)
    dz_total = dz_found + deezer.get("not_found", 0)
    if dz_total:
        lines.append(f"    {dz_found:,} / {dz_total:,} found ({dz_found / dz_total * 100:.0f}%)")

    lines.append("")
    lines.append("  Spotify (of Deezer hits):")
    sp_found = spotify.get("found", 0)
    # Real Spotify checks = found + (not_found where deezer=found)
    not_on_streaming = await results_db.get_not_on_streaming()
    sp_checked_miss = sum(1 for r in not_on_streaming if r["deezer_status"] == "found")
    sp_checked = sp_found + sp_checked_miss
    if sp_checked:
        lines.append(
            f"    {sp_found:,} / {sp_checked:,} found ({sp_found / sp_checked * 100:.0f}%)"
        )

    lines.append("")
    lines.append("  Summary:")
    lines.append(f"    On Spotify: {sp_found:,} ({sp_found / non_compilation * 100:.1f}%)")
    not_streaming_count = len(not_on_streaming)
    lines.append(
        f"    Not on streaming: {not_streaming_count:,} ({not_streaming_count / non_compilation * 100:.1f}%)"
    )
    skipped = spotify.get("skipped", 0)
    lines.append(f"    Compilations (skipped): {skipped:,}")

    return "\n".join(lines)
