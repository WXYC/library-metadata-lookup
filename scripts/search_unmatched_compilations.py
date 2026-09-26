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

from clients.streaming.matching import (
    find_best_typed_match,
    normalize_album_title,
    score_match,
    strip_format_suffix,
)
from scripts._lib.match_decision import (
    AXES_ARTIST_AND_TITLE,
    AXES_TITLE_ONLY,
    STATUS_FOUND,
    ServiceMatch,
    best_title_only_candidate,
    decide_service_match,
    query_credit_is_va,
)
from scripts._lib.runtime import set_up_script_runtime
from scripts._lib.signals import ShutdownFlag
from scripts.streaming_availability.errors import StreamingServiceRoutingError
from scripts.streaming_availability.results_db import ResultsDB
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


async def search_discogs_by_title(pool, title: str, *, query_artist: str) -> dict | None:
    """Search the Discogs PG cache by title. Returns the best match or None.

    This lane used to have no artist gate at all: the best title score above 70
    won whatever the release was credited to, which is how a compilation search
    lands on a named artist's same-titled album (LML#1353). It now picks its rule
    from the shelf credit, because ``build_compilation_query`` does not always
    reduce that credit to a V/A one — its "X mixes various artists" arm and its
    "real artist misclassified as a compilation" fall-through both return real
    names:

    * **V/A shelf credit** → the title axis alone, at this lane's historical 70
      floor, and only against V/A-credited releases. The artist axis is *not*
      scored: the query credit is the ``VA_QUERY_CREDIT`` sentinel, so a release
      credited exactly "Various" would clear 100 on no information at all and
      could outrank a better title — LML#1139 on the accept path.
    * **A recovered real name** → the guarded 80/80 matcher, which is the right
      gate there and is what a row like "Stereolab / Aluminum Tunes" misfiled as
      a compilation needs. The relaxation cannot serve it: it requires a V/A
      credit on both sides and so refuses every candidate.

    The narrowing that remains is deliberate: a compilation whose correct Discogs
    release is credited to a named entity the shelf credit does *not* name (a
    single-composer soundtrack under "Soundtracks - M") no longer yields a
    ``discogs_release_id``. The loss is recoverable — the row stays a Phase 1 miss
    and falls through to Phase 2 — and the alternative is a release id whose
    tracklist belongs to a different record, which every downstream track
    resolution would then inherit.

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

    artist_of = lambda r: r["artist_name"]  # noqa: E731
    title_of = lambda r: r["title"]  # noqa: E731
    # One row per primary credit, so a multi-credit release appears several times
    # under one ``r.id``. The tie-break key has to be total, or equal titles fall
    # back to whatever order the unordered ``LIMIT 20`` returned (LML#1097).
    key_of = lambda r: f"{r['id']}|{r['artist_name']}"  # noqa: E731

    # Which axes may decide is a property of the *query* credit, and the two
    # branches are disjoint: the relaxation requires a V/A credit on both sides, so
    # it refuses every candidate for a real name, and the guarded matcher would
    # score a vacuous artist axis for a V/A one. Same normalization contract as
    # ``va_artist_axis_is_uninformative``, whose query half this is.
    if query_credit_is_va(query_artist):
        winner = best_title_only_candidate(
            rows,
            query_artist=query_artist,
            query_title=title,
            artist_fn=artist_of,
            title_fn=title_of,
            key_fn=key_of,
            floor=DISCOGS_TITLE_FLOOR,
        )
        if winner is None:
            return None
        release, score = winner
        axes = AXES_TITLE_ONLY
    else:
        guarded = find_best_typed_match(
            rows,
            query_artist=query_artist,
            query_title=title,
            artist_fn=artist_of,
            title_fn=title_of,
            key_fn=key_of,
        )
        if guarded is None:
            return None
        release, axes = guarded, AXES_ARTIST_AND_TITLE
        score = (
            score_match(query_artist, artist_of(release)) + score_match(title, title_of(release))
        ) / 2
    return {
        "release_id": release["id"],
        "title": release["title"],
        "artist": release["artist_name"],
        "confidence": score,
        "axes": axes,
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
    results_db: ResultsDB,
    *,
    album_id: int,
    search_artist: str | None,
    search_title: str,
    dry_run: bool,
) -> ServiceMatch | None:
    """Search one lane for an album and record the decision with its provenance.

    Returns the decision only when it was *recorded*, so the caller's hit count
    and log line describe what actually landed. None means nothing was written and
    the row stays available to a later pass: either no axis admitted a candidate,
    or the decision was weaker than the answer the row already holds.

    Under ``--dry-run`` nothing is written and the decision is returned unchecked,
    so a dry run's hit count is an **upper bound**: it cannot know which rows the
    ``skip_if_resolved`` guard would have declined, and that is reachable on the
    Deezer lane because Phase 2 selects on ``spotify_status`` alone. Reading the
    current status to simulate it would cost a query per row, for a number that is
    only ever a preview.
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
    if decision is None or dry_run:
        return decision
    landed = await results_db.update_result(
        album_id,
        lane.service,
        decision.status,
        url=decision.url,
        spotify_id=decision.service_item_id,
        confidence=decision.confidence,
        matched_artist=decision.matched_artist,
        matched_title=decision.matched_title,
        # A one-axis decision never displaces a guarded 80/80 one. Reachable on
        # the Deezer lane: Phase 2 selects on ``spotify_status`` alone.
        skip_if_resolved=decision.status != STATUS_FOUND,
    )
    if not landed:
        # Normally the ``skip_if_resolved`` guard declining to demote a guarded
        # match. Also 0 for an album id that isn't there and for a service with no
        # column family in ``albums``, so the message doesn't name a single cause.
        logger.debug("%s: %s → not recorded, the UPDATE matched no row", lane.service, search_title)
        return None
    return decision


async def run(args) -> None:
    # Refuse a path that isn't there rather than creating one. Both SQLite and
    # ``ResultsDB.connect``'s ``CREATE TABLE IF NOT EXISTS`` will happily answer a
    # typo with a fresh empty artifact, and the drain would then log "Loaded 0
    # unmatched compilations" — indistinguishable from a finished run.
    if args.db_path != ":memory:" and not os.path.exists(args.db_path):
        raise SystemExit(f"no streaming-availability artifact at {args.db_path!r}")

    # Opened through its owner because the write goes through
    # ``ResultsDB.update_result``, which owns this column family. ``connect`` also
    # runs ``_migrate``, which covers the Deezer provenance columns on an older
    # artifact — but *not* ``spotify_matched_artist``/``_title``/``_checked_at``,
    # which exist only in ``_SCHEMA``'s no-op ``CREATE TABLE IF NOT EXISTS``. On an
    # artifact predating those, a Spotify write raises and the per-lane handler
    # logs it as a Spotify error; the prod artifact has them (it already holds
    # rows with stale spotify provenance), so this is a gap in ``_migrate``'s list
    # rather than a live failure. Tracked in LML#1358.
    results_db = ResultsDB(args.db_path)
    await results_db.connect(bootstrap=not args.dry_run)
    db = results_db._db
    assert db is not None

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
        await results_db.close()
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
        await results_db.close()
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
    lane_error: Exception | None = None

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
                        results_db,
                        album_id=row["id"],
                        search_artist=search_artist,
                        search_title=search_title,
                        dry_run=args.dry_run,
                    )
                except StreamingServiceRoutingError:
                    # A mis-addressed service token is a programming error in the
                    # lane table, not a per-album condition. ``update_result``
                    # raises it precisely so it cannot become a silent no-op.
                    #
                    # Narrower than the ``ValueError`` this first caught, because
                    # that also caught two ordinary Spotify runtime conditions --
                    # ``int()`` on a date-form ``Retry-After`` and ``resp.json()``
                    # on a non-JSON 200 body (see ``streaming_availability.errors``)
                    # -- and turned a 429 in hour three into a dead run.
                    raise
                except Exception as exc:
                    # exc_info because the write surface is wide now: a schema
                    # problem and a service outage both land here, and a bare
                    # message reads as the latter.
                    logger.warning(
                        "%s error for %s", lane.service, row["display_title"], exc_info=True
                    )
                    lane_error = exc
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

    # Per-album tolerance above, but a phase that recorded *nothing* while every
    # album errored is an errored source, not a clean zero-match run (LML#376).
    # ``decide_service_match`` raises only when every row of a response failed
    # extraction, so reporting that as "0 streaming matches" is the one outcome the
    # verdict was added to prevent. Raised after the clients are closed, and before
    # the summary, so the run cannot look complete.
    # ``_shutdown.requested`` excluded: an operator interrupting Phase 2 before the
    # first match produces the same "errored and recorded nothing" shape, and
    # ``ShutdownFlag`` exists to make that stop clean. A traceback is not a clean stop.
    if lane_error is not None and not streaming_found and not _shutdown.requested:
        raise lane_error

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

    await results_db.close()


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
