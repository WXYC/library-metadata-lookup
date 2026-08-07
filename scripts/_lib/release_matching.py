"""Shared VA-compilation-to-Discogs-release matching cascade (LML#1020 D6).

Extracted from the former ``scripts/match_compilations.py`` one-off, which
had grown two consumers -- ``scripts/build_compilation_track_location.py``
(LML#1019's recall-index builder) and the tests pinning both -- making it a
genuine shared-script-helper per this package's own scope ("shared helpers
... extracted to remove cross-script duplication"). Only the matching cascade
moved here: the module's original standalone JSON-writing CLI (``main()``,
its argparse wrapper, ``load_compilations``, and the
``streaming_availability.db``-sourced comp loader) was retired outright with
it, since neither surviving consumer calls any of that -- see
``docs/plans/lml-1020-per-track-identity-matcher.md`` D6 for the full
retirement inventory.

Exact -> prefix-strip -> trigram cascade: try an exact (then normalized)
case-insensitive title match against ``va_release`` first, fall back to
stripping a label/series prefix before the first ``" - "``/``": "``, and
finally a ``pg_trgm`` similarity search. ``enrich_with_track_artists`` then
pulls per-track credits (``rta.extra = 0`` primary credits only) for
whatever matched.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import asyncpg
from rapidfuzz import fuzz

from clients.streaming.matching import _FORMAT_SUFFIX_RE

log = logging.getLogger(__name__)

# Strip annotations: [techno comp], (14 cds), etc. Deliberately unconditional
# (LML#1096 audit), unlike clients/streaming/matching.py's conservative
# canonical bracket/parenthetical regexes: a compilation title's bracket/paren
# content is cataloger noise ("[techno comp]", "(14 cds)") a real compilation
# title never legitimately carries, unlike an album title where similar
# content (e.g. "[Live]"/"[Disc One]") can be meaningful -- so this domain
# intentionally strips more aggressively. See
# tests/unit/test_match_compilations_normalize.py for the pinned behavior.
_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
_PAREN_ANNOTATION_RE = re.compile(r"\s*\([^)]*\)\s*$")
# _FORMAT_SUFFIX_RE above is imported from the canonical implementation
# (LML#1096) -- this one pattern was a byte-for-byte duplicate with no
# domain-specific divergence, unlike the bracket/paren patterns above.
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


async def exact_match(
    conn: asyncpg.Connection, comps: list[CompAlbum]
) -> tuple[list[DiscogsMatch], list[CompAlbum], dict[str, list[tuple[int, str]]]]:
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

        # Group by track sequence; collect all primary (extra=0) credited artists per track.
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
