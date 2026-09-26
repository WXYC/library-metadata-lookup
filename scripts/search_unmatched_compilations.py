"""Search unmatched compilations by title against Discogs cache, then streaming APIs.

Phase 1: Discogs PG cache title search (exact + fuzzy) → gets release_id + tracklist
Phase 2: Deezer/Spotify album search for Discogs misses → gets streaming URLs

Usage:
    railway run -- .venv/bin/python -m scripts.search_unmatched_compilations [OPTIONS]
"""

from __future__ import annotations

import asyncio
import logging
import os
from argparse import ArgumentParser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiosqlite

from clients.streaming.matching import (
    normalize_album_title,
    strip_format_suffix,
)
from scripts._lib.match_decision import (
    AXES_TITLE_ONLY,
    ServiceMatch,
    best_title_only_candidate,
    decide_service_match,
    update_service_match,
)
from scripts._lib.runtime import set_up_script_runtime
from scripts._lib.signals import ShutdownFlag
from scripts.track_streaming.compilation_search import build_compilation_query

logger = logging.getLogger("search_compilations")

_shutdown = ShutdownFlag(logger=logger, unit="item", log_force_quit=False)


# Deezer/Spotify accessors for album search
_DEEZER_ARTIST = lambda r: r.get("artist", {}).get("name", "")  # noqa: E731
_DEEZER_TITLE = lambda r: r.get("title", "")  # noqa: E731
_DEEZER_URL = lambda r: r.get("link", "")  # noqa: E731
_SPOTIFY_ARTIST = lambda r: r.get("artists", [{}])[0].get("name", "")  # noqa: E731
_SPOTIFY_TITLE = lambda r: r.get("name", "")  # noqa: E731
_SPOTIFY_URL = lambda r: r.get("external_urls", {}).get("spotify", "")  # noqa: E731
_SPOTIFY_ID = lambda r: r.get("id", "")  # noqa: E731

# The credit every lane scores against when ``build_compilation_query`` found no
# artist at all — the row is ``is_compilation = 1`` filed under a shelf
# convention, so the V/A sentinel is what the query side actually means.
VA_QUERY_CREDIT = "Various"

# This lane's historical title floor, below the shared 80 acceptance floor.
# LML#1353 changes which candidates may be judged on the title axis alone, not
# where that axis's bar sits, so the number is preserved as it was found.
DISCOGS_TITLE_FLOOR = 70.0


async def search_discogs_by_title(
    pool, title: str, *, query_artist: str = VA_QUERY_CREDIT
) -> dict | None:
    """Search the Discogs PG cache by title. Returns the best match or None.

    This lane has no artist gate at all: it took the best title score above 70
    whatever the release was credited to, which is how a compilation search lands
    on a named artist's same-titled album (LML#1353). The scan now runs through
    ``best_title_only_candidate``, which admits a one-axis judgement only where
    the artist axis is uninformative — both the shelf credit and the Discogs
    release credit are V/A credits, and "Various" is Discogs's own primary credit
    for a compilation.

    ``query_artist`` defaults to the V/A sentinel because callers pass rows whose
    ``display_artist`` ``build_compilation_query`` already reduced to a filing
    convention. A caller that *did* recover a real artist name passes it, and the
    relaxation then declines: the artist axis carries information there and this
    lane has no way to check it.

    Unlike the streaming lanes, ``albums`` has no ``discogs_confidence`` column,
    so the returned score is log-only; ``axes`` names what it measured so no
    reader can take it for a two-axis confidence.
    """
    normalized = normalize_album_title(title)
    if not normalized:
        return None

    # Exact title match
    rows = await pool.fetch(
        """SELECT r.id, r.title, ra.artist_name
           FROM release r
           JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
           WHERE lower(f_unaccent(r.title)) = lower(f_unaccent($1))
           LIMIT 20""",
        title,
    )
    if not rows:
        # Try with stripped format suffix
        stripped = strip_format_suffix(title)
        if stripped != title:
            rows = await pool.fetch(
                """SELECT r.id, r.title, ra.artist_name
                   FROM release r
                   JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                   WHERE lower(f_unaccent(r.title)) = lower(f_unaccent($1))
                   LIMIT 20""",
                stripped,
            )

    if not rows:
        return None

    winner = best_title_only_candidate(
        rows,
        query_artist=query_artist,
        query_title=title,
        artist_fn=lambda r: r["artist_name"],
        title_fn=lambda r: r["title"],
        key_fn=lambda r: str(r["id"]),
        floor=DISCOGS_TITLE_FLOOR,
    )
    if winner is None:
        return None
    release, title_score = winner
    return {
        "release_id": release["id"],
        "title": release["title"],
        "artist": release["artist_name"],
        "confidence": title_score,
        "axes": AXES_TITLE_ONLY,
    }


@dataclass(frozen=True)
class StreamingLane:
    """One streaming service's search call and result extractors."""

    service: str
    search: Callable[[str, str], Awaitable[list[dict]]]
    artist_fn: Callable[[dict], str]
    title_fn: Callable[[dict], str]
    url_fn: Callable[[dict], str]
    id_fn: Callable[[dict], str] | None = None
    # What to send as the *search* artist term when the row carries no artist at
    # all. Preserved per-lane as found: Deezer was queried with an empty term and
    # Spotify with the literal "Various Artists". It is a recall knob on the
    # query, not part of the match decision — both lanes score against
    # ``VA_QUERY_CREDIT``.
    search_credit: str = ""


async def resolve_lane(
    lane: StreamingLane,
    db: aiosqlite.Connection,
    *,
    album_id: int,
    search_artist: str | None,
    search_title: str,
    dry_run: bool,
) -> ServiceMatch | None:
    """Search one lane for an album and record the decision with its provenance.

    Returns the decision, or None when neither the guarded 80/80 matcher nor the
    V/A title-only relaxation admitted a candidate. Nothing is written in that
    case: the row keeps its ``skipped`` status and stays available to a later
    pass, which is the right outcome for a candidate no axis can justify.
    """
    results = await lane.search(
        search_artist or lane.search_credit, strip_format_suffix(search_title)
    )
    decision = decide_service_match(
        results,
        query_artist=search_artist or VA_QUERY_CREDIT,
        query_title=search_title,
        artist_fn=lane.artist_fn,
        title_fn=lane.title_fn,
        url_fn=lane.url_fn,
        id_fn=lane.id_fn,
    )
    if decision is None:
        return None
    if not dry_run:
        await update_service_match(db, album_id=album_id, service=lane.service, match=decision)
    return decision


async def run(args) -> None:
    db = await aiosqlite.connect(args.db_path)
    db.row_factory = aiosqlite.Row

    cursor = await db.execute(
        """SELECT id, display_artist, display_title, discogs_release_id
           FROM albums
           WHERE is_compilation = 1
             AND spotify_status = 'skipped'
             AND id NOT IN (SELECT DISTINCT album_id FROM track_results)
           ORDER BY id
           LIMIT ?""",
        (args.limit,),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    logger.info("Loaded %d unmatched compilations", len(rows))

    if not rows:
        await db.close()
        return

    # Phase 1: Discogs title search
    discogs_found = 0
    discogs_misses = []
    discogs_url = os.environ.get("DATABASE_URL_DISCOGS")

    if discogs_url:
        import asyncpg

        pool = await asyncpg.create_pool(discogs_url, min_size=2, max_size=5)
        logger.info("Phase 1: Searching Discogs cache by title...")

        try:
            for i, row in enumerate(rows, 1):
                if _shutdown.requested:
                    break

                search_artist, search_title = build_compilation_query(
                    row["display_artist"], row["display_title"]
                )
                match = await search_discogs_by_title(
                    pool, search_title, query_artist=search_artist or VA_QUERY_CREDIT
                )

                if match:
                    discogs_found += 1
                    if not args.dry_run:
                        await db.execute(
                            """UPDATE albums SET discogs_release_id = ?,
                               discogs_artist = ?, discogs_title = ?,
                               discogs_status = 'found'
                               WHERE id = ?""",
                            (
                                match["release_id"],
                                match["artist"],
                                match["title"],
                                row["id"],
                            ),
                        )
                    logger.debug(
                        "Discogs: %s → %s (%s, %.0f%% on the %s axis)",
                        row["display_title"],
                        match["title"],
                        match["artist"],
                        match["confidence"],
                        match["axes"],
                    )
                else:
                    discogs_misses.append(row)

                if i % 200 == 0:
                    if not args.dry_run:
                        await db.commit()
                    logger.info(
                        "  Discogs: %d / %d | found: %d | miss: %d",
                        i,
                        len(rows),
                        discogs_found,
                        len(discogs_misses),
                    )
        finally:
            if not args.dry_run:
                await db.commit()
            await pool.close()

        logger.info(
            "Phase 1 complete: %d Discogs matches, %d misses",
            discogs_found,
            len(discogs_misses),
        )
    else:
        logger.warning("DATABASE_URL_DISCOGS not set, skipping Discogs search")
        discogs_misses = rows

    if _shutdown.requested or args.discogs_only:
        await db.close()
        return

    # Phase 2: Streaming API search for Discogs misses
    logger.info("Phase 2: Searching streaming APIs for %d Discogs misses...", len(discogs_misses))

    from clients.streaming.deezer import DeezerClient

    deezer = DeezerClient()
    spotify = None

    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if client_id and client_secret:
        from clients.streaming.spotify import SpotifyClient

        spotify = SpotifyClient(client_id, client_secret)

    lanes = [
        StreamingLane(
            service="deezer",
            search=deezer.search_album,
            artist_fn=_DEEZER_ARTIST,
            title_fn=_DEEZER_TITLE,
            url_fn=_DEEZER_URL,
        )
    ]
    if spotify:
        lanes.append(
            StreamingLane(
                service="spotify",
                search=spotify.search_album,
                artist_fn=_SPOTIFY_ARTIST,
                title_fn=_SPOTIFY_TITLE,
                url_fn=_SPOTIFY_URL,
                id_fn=_SPOTIFY_ID,
                search_credit="Various Artists",
            )
        )

    streaming_found = 0
    streaming_miss = 0

    try:
        for i, row in enumerate(discogs_misses, 1):
            if _shutdown.requested:
                break

            search_artist, search_title = build_compilation_query(
                row["display_artist"], row["display_title"]
            )
            found = False

            for lane in lanes:
                try:
                    decision = await resolve_lane(
                        lane,
                        db,
                        album_id=row["id"],
                        search_artist=search_artist,
                        search_title=search_title,
                        dry_run=args.dry_run,
                    )
                except Exception:
                    logger.warning("%s error for %s", lane.service, row["display_title"])
                    continue
                if decision is not None:
                    found = True
                    logger.debug(
                        "%s: %s → %s (%s, %.0f%% on the %s axis)",
                        lane.service,
                        row["display_title"],
                        decision.matched_title,
                        decision.matched_artist,
                        decision.confidence,
                        decision.axes,
                    )

            if found:
                streaming_found += 1
            else:
                streaming_miss += 1

            if i % 100 == 0:
                if not args.dry_run:
                    await db.commit()
                hit_rate = streaming_found / i * 100
                logger.info(
                    "  Streaming: %d / %d | found: %d (%.0f%%) | miss: %d",
                    i,
                    len(discogs_misses),
                    streaming_found,
                    hit_rate,
                    streaming_miss,
                )
    finally:
        await deezer.close()
        if spotify:
            await spotify.close()
        if not args.dry_run:
            await db.commit()

    logger.info(
        "Phase 2 complete: %d streaming matches, %d misses",
        streaming_found,
        streaming_miss,
    )
    logger.info(
        "Total: %d Discogs + %d streaming = %d found out of %d",
        discogs_found,
        streaming_found,
        discogs_found + streaming_found,
        len(rows),
    )

    await db.close()


def main() -> None:
    global _shutdown
    parser = ArgumentParser(description="Search unmatched compilations by title")
    parser.add_argument("--db-path", default="streaming_availability.db")
    parser.add_argument("--limit", type=int, default=100000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discogs-only", action="store_true", help="Skip streaming API search")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    _shutdown = set_up_script_runtime(
        logger=logger,
        verbose=args.verbose,
        shutdown_unit="item",
        log_force_quit=False,
        quiet_httpx=True,
        include_logger_name=False,
    )

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
