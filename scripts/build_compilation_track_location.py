"""Build ``lml_cache.compilation_track_location``, the V/A recall index (LML#1019).

Answers "which library shelf locations contain track *T* credited to artist
*A*?" by walking every compilation-shelf row in ``library.db`` (any artist
``wxyc_etl.text.is_compilation_artist`` recognizes -- "Various Artists - *"
and "Soundtracks - *" shelf buckets alike), matching it to a Discogs release
via the same normalized-title cascade ``scripts/match_compilations.py`` uses
against ``va_release``, and persisting every ``release_track_artist`` credit
for that release -- not just the primary artist -- tiered into a coarse
``credit_role``. Precision ranking is LML#1022's job; this only guarantees
every candidate row exists for it to rank.

Runs standalone, outside the FastAPI service (a discogs-etl-cloned checkout
per the ship-dag triage resolution on LML#1019), so schema bootstrap is
self-sufficient: ``set_up_compilation_track_location_schema`` issues its own
``CREATE SCHEMA/TABLE IF NOT EXISTS`` rather than assuming the LML lifespan
already ran.

Data-safe: every insert is ``ON CONFLICT (library_id, track_position,
track_artist) DO NOTHING`` -- a successfully-populated row is never
overwritten. A comp that fails to match (or matches a release with no cached
tracklist) produces zero rows, so it stays absent and is retried on the next
run for free, with no separate failure-tracking table needed.

Modes:

* ``--incremental`` -- only comps with **no existing row at all** (the cheap
  "what's new since last run" diff, bounded per-run cost -- the daily
  library.db-sync cadence per the issue's Freshness section).
  * ``--full`` -- every compilation-shelf row in ``library.db``, including ones
  already populated. Existing successful triples are untouched (``DO
  NOTHING``); this mode's value is picking up NEW track/artist credits or a
  now-resolvable match for a previously-failed comp after a discogs-cache
  rebuild shifted the underlying data (the monthly cadence).

Usage:
    uv run python -m scripts.build_compilation_track_location --incremental
    uv run python -m scripts.build_compilation_track_location --full --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass

import aiosqlite
import asyncpg
from wxyc_etl.text import is_compilation_artist, to_match_form

from entity.compilation_track_location import set_up_compilation_track_location_schema
from entity.sources import PgSource
from lookup.artwork import _resolve_fallback_artwork
from scripts.match_compilations import (
    CompAlbum,
    exact_match,
    normalize_comp_title,
    prefix_strip_match,
    trigram_match,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("build_compilation_track_location")

DEFAULT_LIBRARY_DB_PATH = "library.db"

# release_track_artist.extra=1 covers every non-primary credit (featured
# artists, remixers, producers, writers, ...) undifferentiated at the source;
# `role` is the only signal that separates a featured performer from the
# rest. Coarse on purpose -- LML#1022 owns precision ranking, not this build.
_FEATURED_ROLE_MARKERS = ("featur", "vocal", "voice")


def tier_credit_role(extra: int, role: str | None) -> str:
    """Coarse ``credit_role`` tier from ``release_track_artist.extra``/``role``.

    ``extra == 0`` is always the track's primary credit. A non-primary credit
    is tiered ``featured`` when its role text names a performer credit
    (Featuring/Vocals/Voice), else ``extra`` (Producer, Remix, Written-By,
    etc. -- non-performer contributions still worth carrying as a candidate,
    just ranked lower downstream).
    """
    if not extra:
        return "primary"
    role_lower = (role or "").lower()
    if any(marker in role_lower for marker in _FEATURED_ROLE_MARKERS):
        return "featured"
    return "extra"


@dataclass(frozen=True)
class CompCandidate:
    """One compilation-shelf row from ``library.db`` awaiting a Discogs match."""

    library_id: int
    title: str
    artist: str


async def load_library_compilations(db_path: str) -> list[CompCandidate]:
    """Every compilation-shelf ``library.db`` row, filtered via ``is_compilation_artist``.

    Covers both the "Various Artists - *" and "Soundtracks - *" shelf
    buckets (and any other form the shared classifier recognizes) -- the
    issue's explicit requirement not to limit this to Various Artists alone.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall("SELECT id, title, artist FROM library")
    return [
        CompCandidate(library_id=row["id"], title=row["title"], artist=row["artist"])
        for row in rows
        if is_compilation_artist(row["artist"])
    ]


async def get_processed_library_ids(conn: asyncpg.Connection) -> set[int]:
    """``library_id``s that already carry at least one recall-index row.

    Used by ``--incremental`` to find "new since last run" comps. A comp
    with zero rows is either never-attempted or previously match-failed --
    both are retryable, and this diff doesn't need to tell them apart.
    """
    rows = await conn.fetch("SELECT DISTINCT library_id FROM lml_cache.compilation_track_location")
    return {row["library_id"] for row in rows}


async def match_comp_release(
    conn: asyncpg.Connection, comps: list[CompCandidate]
) -> dict[int, int]:
    """Match each comp to a Discogs release id, reusing ``match_compilations.py``.

    Runs the same exact -> prefix-strip -> trigram cascade the standalone VA
    matcher uses against ``va_release``, so this build doesn't reinvent
    comp-title matching. Returns ``library_id -> discogs_release_id`` for
    every comp that matched; an unmatched comp is simply absent from the
    result (retried on the next run).
    """
    if not comps:
        return {}
    albums = [
        CompAlbum(
            id=comp.library_id,
            title=comp.title,
            display_artist=comp.artist,
            normalized_title=normalize_comp_title(comp.title),
        )
        for comp in comps
    ]
    await conn.execute("SET pg_trgm.similarity_threshold = 0.3")
    exact_matches, remaining, title_map = await exact_match(conn, albums)
    prefix_matches, remaining = await prefix_strip_match(title_map, remaining)
    fuzzy_matches, _unmatched = await trigram_match(conn, remaining)
    return {
        match.comp_id: match.discogs_release_id
        for match in (*exact_matches, *prefix_matches, *fuzzy_matches)
    }


_CREDITS_SQL = """\
SELECT rta.release_id, rta.artist_name, rta.extra, rta.role,
       rt.position, rt.title AS track_title, rt.sequence
FROM release_track_artist rta
JOIN release_track rt
    ON rt.release_id = rta.release_id AND rt.sequence = rta.track_sequence
WHERE rta.release_id = ANY($1)
ORDER BY rta.release_id, rt.sequence\
"""


async def fetch_track_credits(
    conn: asyncpg.Connection, release_ids: list[int]
) -> dict[int, list[dict]]:
    """All ``release_track_artist`` credits for ``release_ids`` (every tier, not just primary).

    Joined to ``release_track`` for the track's position/title. Grouped by
    ``release_id`` so the caller can build rows per matched comp without a
    second round-trip per release.
    """
    if not release_ids:
        return {}
    rows = await conn.fetch(_CREDITS_SQL, release_ids)
    by_release: dict[int, list[dict]] = {}
    for row in rows:
        by_release.setdefault(row["release_id"], []).append(dict(row))
    return by_release


_INSERT_SQL = """\
INSERT INTO lml_cache.compilation_track_location
    (library_id, track_position, track_artist, track_title, credit_role,
     discogs_release_id, artwork_url)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (library_id, track_position, track_artist) DO NOTHING\
"""


def build_rows(
    *,
    library_id: int,
    discogs_release_id: int,
    credits: list[dict],
    artwork_url: str | None,
) -> list[tuple]:
    """One row per credit, keyed to the recall index's normalized reverse-lookup columns.

    ``track_artist``/``track_title`` are normalized with the same
    ``wxyc_etl.text.to_match_form`` a runtime probe (LML#1022) applies to its
    own input, so an exact-match probe hits these rows directly. Falls back
    to the track sequence number when Discogs carries no ``position`` string
    (e.g. some digital releases), so the primary-key component is never
    empty.
    """
    rows = []
    for credit in credits:
        position = credit["position"] or str(credit["sequence"])
        rows.append(
            (
                library_id,
                position,
                to_match_form(credit["artist_name"]),
                to_match_form(credit["track_title"]),
                tier_credit_role(credit["extra"], credit["role"]),
                discogs_release_id,
                artwork_url,
            )
        )
    return rows


async def build_compilation_track_location(
    *,
    library_db_path: str,
    discogs_conn: asyncpg.Connection,
    discogs_service,
    full: bool,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Core build routine: discover comps, match, fetch credits, precompute art, write.

    ``discogs_service`` is a live ``DiscogsService`` wired to the shared
    Discogs rate bucket (LML#879) for the artwork precompute
    (``lookup/artwork.py:_resolve_fallback_artwork``); pass ``None`` to skip
    artwork resolution entirely (rows still insert with ``artwork_url =
    NULL``), which is how the test suite exercises the match+insert path
    without a live Discogs dependency.
    """
    comps = await load_library_compilations(library_db_path)
    if not full:
        processed = await get_processed_library_ids(discogs_conn)
        comps = [comp for comp in comps if comp.library_id not in processed]
    if limit is not None:
        comps = comps[:limit]
    if not comps:
        log.info("no compilation candidates to process")
        return {"candidates": 0, "matched": 0, "rows_inserted": 0}

    matches = await match_comp_release(discogs_conn, comps)
    log.info("matched %d/%d comps to a Discogs release", len(matches), len(comps))

    release_ids = sorted(set(matches.values()))
    credits_by_release = await fetch_track_credits(discogs_conn, release_ids)

    rows_inserted = 0
    for comp in comps:
        release_id = matches.get(comp.library_id)
        if release_id is None:
            continue
        credits = credits_by_release.get(release_id, [])
        if not credits:
            log.warning(
                "library_id=%d matched release %d but it has no cached tracklist -- skipping",
                comp.library_id,
                release_id,
            )
            continue
        artwork_url = None
        if discogs_service is not None:
            artwork_url = await _resolve_fallback_artwork(discogs_service, release_id)
        rows = build_rows(
            library_id=comp.library_id,
            discogs_release_id=release_id,
            credits=credits,
            artwork_url=artwork_url,
        )
        if not dry_run:
            await discogs_conn.executemany(_INSERT_SQL, rows)
        rows_inserted += len(rows)

    return {
        "candidates": len(comps),
        "matched": len(matches),
        "rows_inserted": rows_inserted,
    }


async def main(args: argparse.Namespace) -> None:
    from config.settings import get_settings
    from discogs.cache_service import DiscogsCacheService
    from discogs.service import DiscogsService

    settings = get_settings()
    db_url = settings.database_url_discogs
    if not db_url:
        raise SystemExit("DATABASE_URL_DISCOGS is not configured -- cannot build the recall index")

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    try:
        pg = PgSource(pool=pool)
        await set_up_compilation_track_location_schema(pg)

        discogs_service = None
        if not args.dry_run and settings.discogs_token:
            discogs_service = DiscogsService(
                token=settings.discogs_token, cache_service=DiscogsCacheService(pool)
            )

        async with pool.acquire() as conn:
            stats = await build_compilation_track_location(
                library_db_path=args.library_db,
                discogs_conn=conn,
                discogs_service=discogs_service,
                full=args.full,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        log.info(
            "build complete: %d candidates, %d matched, %d rows inserted%s",
            stats["candidates"],
            stats["matched"],
            stats["rows_inserted"],
            " [dry-run]" if args.dry_run else "",
        )
    finally:
        await pool.close()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--incremental",
        action="store_true",
        help="Only process comps with no existing recall-index row (daily cadence)",
    )
    mode.add_argument(
        "--full",
        action="store_true",
        help="Reprocess every compilation-shelf row (monthly cadence)",
    )
    parser.add_argument(
        "--library-db",
        default=DEFAULT_LIBRARY_DB_PATH,
        help=f"Path to library.db (default: {DEFAULT_LIBRARY_DB_PATH})",
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of comps processed")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Match + report only; no artwork resolution, no writes",
    )
    return parser


if __name__ == "__main__":
    asyncio.run(main(_build_arg_parser().parse_args()))
