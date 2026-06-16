"""Match VA compilations from streaming_availability.db to Discogs releases and extract track artists.

Usage:
    .venv/bin/python scripts/match_compilations.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from dataclasses import dataclass

import aiosqlite
import asyncpg
from rapidfuzz import fuzz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DISCOGS_URL = "postgresql://jake@localhost/discogs"
SQLITE_PATH = "streaming_availability.db"

# Strip annotations: [techno comp], (14 cds), etc.
_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
_PAREN_ANNOTATION_RE = re.compile(r"\s*\([^)]*\)\s*$")
# Strip trailing format suffixes
_FORMAT_SUFFIX_RE = re.compile(
    r"""\s+(?:\d{1,2}[""]|LP|EP|CD|x\s*\d+)$""",
    re.IGNORECASE,
)
# Strip leading label/annotation prefixes like "tribute: " or "FA 05-90 - "
_LABEL_PREFIX_RE = re.compile(r"^[A-Z]{2,}\s*[-:]?\s*(?:\d{2,}-\d{2,}\s*[-:]\s*)?", re.IGNORECASE)


def normalize_comp_title(title: str) -> str:
    """Normalize a compilation title for matching against Discogs."""
    result = _BRACKET_RE.sub("", title)
    result = _PAREN_ANNOTATION_RE.sub("", result)
    result = _FORMAT_SUFFIX_RE.sub("", result)
    return result.strip()


@dataclass
class CompAlbum:
    id: int
    title: str
    display_artist: str
    normalized_title: str


@dataclass
class DiscogsMatch:
    comp_id: int
    comp_title: str
    discogs_release_id: int
    discogs_title: str
    confidence: float
    track_count: int


async def load_compilations(db_path: str) -> list[CompAlbum]:
    """Load compilation albums from streaming_availability.db."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT id, display_title, display_artist FROM albums WHERE is_compilation = 1"
        )
        return [
            CompAlbum(
                id=r["id"],
                title=r["display_title"],
                display_artist=r["display_artist"],
                normalized_title=normalize_comp_title(r["display_title"]),
            )
            for r in rows
        ]


async def exact_match(
    conn: asyncpg.Connection, comps: list[CompAlbum]
) -> tuple[list[DiscogsMatch], list[CompAlbum]]:
    """Try exact case-insensitive title match against VA Discogs releases."""
    matched: list[DiscogsMatch] = []
    unmatched: list[CompAlbum] = []

    # Load all VA release titles from pre-built va_release table
    va_releases = await conn.fetch("SELECT id, title, norm_title FROM va_release")
    log.info(f"Loaded {len(va_releases):,} VA releases from va_release table")

    # Build title -> release_ids mapping
    title_map: dict[str, list[tuple[int, str]]] = {}
    for row in va_releases:
        key = row["title"].lower()
        title_map.setdefault(key, []).append((row["id"], row["title"]))

    for comp in comps:
        # Try both original and normalized
        candidates = title_map.get(comp.title.lower(), [])
        if not candidates:
            candidates = title_map.get(comp.normalized_title.lower(), [])

        if candidates:
            # Pick any — check for track artists later
            release_id, discogs_title = candidates[0]
            matched.append(
                DiscogsMatch(
                    comp_id=comp.id,
                    comp_title=comp.title,
                    discogs_release_id=release_id,
                    discogs_title=discogs_title,
                    confidence=100.0,
                    track_count=0,
                )
            )
        else:
            unmatched.append(comp)

    log.info(f"Exact match: {len(matched):,} matched, {len(unmatched):,} remaining")
    return matched, unmatched, title_map


async def prefix_strip_match(
    title_map: dict[str, list[tuple[int, str]]],
    comps: list[CompAlbum],
) -> tuple[list[DiscogsMatch], list[CompAlbum]]:
    """Try matching after stripping label/series prefixes from titles.

    Many WXYC compilation titles have a label or series name prepended, e.g.
    "Sugar Hill - The Great Rap Hits" where the Discogs title is "The Great Rap Hits",
    or "Ninja Tune - Hip Hop & Jazz" where the Discogs title is "Ninja Tune".
    """
    matched: list[DiscogsMatch] = []
    unmatched: list[CompAlbum] = []

    for comp in comps:
        found = False
        candidates_to_try = []

        # Dash separator: try both sides
        if " - " in comp.title:
            parts = comp.title.split(" - ", 1)
            candidates_to_try.append(parts[1].strip().strip('"'))
            candidates_to_try.append(parts[0].strip())

        # Colon separator: try suffix
        if ": " in comp.title:
            candidates_to_try.append(comp.title.split(": ", 1)[1].strip())
            candidates_to_try.append(comp.title.split(": ", 1)[0].strip())

        for candidate in candidates_to_try:
            if len(candidate) < 3:
                continue
            hits = title_map.get(candidate.lower(), [])
            if hits:
                release_id, discogs_title = hits[0]
                matched.append(
                    DiscogsMatch(
                        comp_id=comp.id,
                        comp_title=comp.title,
                        discogs_release_id=release_id,
                        discogs_title=discogs_title,
                        confidence=95.0,
                        track_count=0,
                    )
                )
                found = True
                break

        if not found:
            unmatched.append(comp)

    log.info(f"Prefix-strip match: {len(matched):,} matched, {len(unmatched):,} remaining")
    return matched, unmatched


async def trigram_match(
    conn: asyncpg.Connection,
    comps: list[CompAlbum],
    *,
    fuzz_threshold: float = 80.0,
) -> tuple[list[DiscogsMatch], list[CompAlbum]]:
    """Use pg_trgm trigram similarity to fuzzy-match compilation titles.

    Runs individual queries per title using the GIN index on va_release.norm_title.
    Each query is fast (~10-50ms) with the index.
    """
    matched: list[DiscogsMatch] = []
    unmatched: list[CompAlbum] = []

    await conn.execute("SET pg_trgm.similarity_threshold = 0.3")

    total = len(comps)
    for i, comp in enumerate(comps):
        rows = await conn.fetch(
            """
            SELECT vr.id as release_id, vr.title,
                   similarity(vr.norm_title, lower($1)) as sim
            FROM va_release vr
            WHERE vr.norm_title % lower($1)
            ORDER BY sim DESC
            LIMIT 5
            """,
            comp.normalized_title,
        )

        if not rows:
            unmatched.append(comp)
        else:
            # Score with rapidfuzz
            best_match = None
            best_score = 0.0
            for row in rows:
                score = fuzz.token_sort_ratio(comp.normalized_title.lower(), row["title"].lower())
                if score >= fuzz_threshold and score > best_score:
                    best_score = score
                    best_match = row

            if best_match:
                matched.append(
                    DiscogsMatch(
                        comp_id=comp.id,
                        comp_title=comp.title,
                        discogs_release_id=best_match["release_id"],
                        discogs_title=best_match["title"],
                        confidence=best_score,
                        track_count=0,
                    )
                )
            else:
                unmatched.append(comp)

        if (i + 1) % 500 == 0 or i + 1 == total:
            log.info(f"Trigram match: {i + 1}/{total} processed, {len(matched)} matched")

    log.info(f"Trigram match: {len(matched):,} matched, {len(unmatched):,} remaining")
    return matched, unmatched


async def enrich_with_track_artists(
    conn: asyncpg.Connection,
    matches: list[DiscogsMatch],
) -> list[dict]:
    """For each matched release, get track-level artists from release_track_artist."""
    results = []
    release_ids = [m.discogs_release_id for m in matches]

    # Batch fetch track artists
    batch_size = 500
    track_data: dict[int, list[dict]] = {}

    for i in range(0, len(release_ids), batch_size):
        batch_ids = release_ids[i : i + batch_size]
        rows = await conn.fetch(
            """
            SELECT rta.release_id, rta.track_sequence, rta.artist_name,
                   rt.title as track_title, rt.position
            FROM release_track_artist rta
            JOIN release_track rt ON rt.release_id = rta.release_id
                AND rt.sequence = rta.track_sequence
            WHERE rta.release_id = ANY($1)
              AND rta.extra = 0
            ORDER BY rta.release_id, rta.track_sequence
            """,
            batch_ids,
        )
        for row in rows:
            rid = row["release_id"]
            track_data.setdefault(rid, []).append(
                {
                    "sequence": row["track_sequence"],
                    "position": row["position"],
                    "track_title": row["track_title"],
                    "artist": row["artist_name"],
                }
            )
        log.info(
            f"Fetched track artists for {min(i + batch_size, len(release_ids))}/{len(release_ids)} releases"
        )

    for match in matches:
        tracks = track_data.get(match.discogs_release_id, [])

        # Deduplicate: group by sequence, pick primary credited artist per track
        track_artists: dict[int, dict] = {}
        for t in tracks:
            seq = t["sequence"]
            if seq not in track_artists:
                track_artists[seq] = {
                    "position": t["position"],
                    "title": t["track_title"],
                    "artists": [],
                }
            track_artists[seq]["artists"].append(t["artist"])

        match.track_count = len(track_artists)
        results.append(
            {
                "comp_id": match.comp_id,
                "comp_title": match.comp_title,
                "discogs_release_id": match.discogs_release_id,
                "discogs_title": match.discogs_title,
                "confidence": match.confidence,
                "tracks": [
                    {
                        "position": v["position"],
                        "title": v["title"],
                        "artists": v["artists"],
                    }
                    for v in sorted(track_artists.values(), key=lambda x: x["position"])
                ],
            }
        )

    with_tracks = sum(1 for r in results if r["tracks"])
    log.info(
        f"Enriched: {with_tracks:,} releases have track artist data, {len(results) - with_tracks:,} do not"
    )
    return results


async def main(args: argparse.Namespace) -> None:
    comps = await load_compilations(SQLITE_PATH)
    log.info(f"Loaded {len(comps):,} compilations from {SQLITE_PATH}")

    if args.limit:
        comps = comps[: args.limit]
        log.info(f"Limited to {args.limit}")

    conn = await asyncpg.connect(DISCOGS_URL)
    try:
        # Set trigram threshold
        await conn.execute("SET pg_trgm.similarity_threshold = 0.3")

        # Phase 1: Exact match
        exact_matches, remaining, title_map = await exact_match(conn, comps)

        # Phase 2: Prefix-stripping exact match (label/series name before dash or colon)
        prefix_matches, remaining = await prefix_strip_match(title_map, remaining)

        # Phase 3: Trigram fuzzy match
        fuzzy_matches, still_unmatched = await trigram_match(conn, remaining)

        all_matches = exact_matches + prefix_matches + fuzzy_matches
        log.info(
            f"Total matched: {len(all_matches):,} / {len(comps):,} "
            f"({len(all_matches) / len(comps) * 100:.1f}%)"
        )

        if not args.dry_run:
            # Phase 3: Enrich with track artists
            results = await enrich_with_track_artists(conn, all_matches)

            # Save results
            output_path = "compilation_track_artists.json"
            with open(output_path, "w") as f:
                json.dump(results, f, indent=2)
            log.info(f"Saved {len(results):,} results to {output_path}")

            # Also save unmatched for review
            unmatched_path = "compilation_unmatched.json"
            unmatched_data = [
                {"id": c.id, "title": c.title, "display_artist": c.display_artist}
                for c in still_unmatched
            ]
            with open(unmatched_path, "w") as f:
                json.dump(unmatched_data, f, indent=2)
            log.info(f"Saved {len(still_unmatched):,} unmatched to {unmatched_path}")

        # Sample unmatched
        if still_unmatched:
            log.info("Sample unmatched titles:")
            for c in still_unmatched[:20]:
                log.info(f"  {c.title}")

    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Match compilations to Discogs")
    parser.add_argument("--dry-run", action="store_true", help="Only show match stats")
    parser.add_argument("--limit", type=int, help="Limit to first N compilations")
    args = parser.parse_args()
    asyncio.run(main(args))
