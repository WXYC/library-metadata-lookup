"""Export streaming URLs from streaming_availability.db into library.db.

Creates a streaming_links table in library.db that maps library release IDs
to verified streaming service URLs. This makes streaming data available to
the LML API and all downstream clients.

The word "verified" is load-bearing downstream: ``lookup/enrichment/item.py``
serves whatever lands here and labels it ``streaming_status.spotify = "verified"``
after a host + well-formedness check only (LML#1352). So this script is the last
place that can refuse a URL for which no match evidence exists at all --
see ``_has_match_provenance`` for the one such refusal it makes.

This script only ever READS streaming_availability.db, and enforces that by opening
it ``mode=ro`` rather than by containing no UPDATE. That file is the single
bucket-canonical copy of rate-limited Apple/Spotify/Deezer results and is expensive
to recollect, and a read-write open would silently roll back a hot journal left by a
crashed upstream run.

Usage:
    .venv/bin/python scripts/export_streaming_links.py [--library-db PATH] [--streaming-db PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# The two `albums` columns that record what a stored spotify_url was matched
# against. A row with neither is unauditable without re-fetching the Spotify page.
SPOTIFY_PROVENANCE_COLUMNS = ("spotify_matched_artist", "spotify_matched_title")


def _has_match_provenance(matched_artist: str | None, matched_title: str | None) -> bool:
    """Does this row record anything about what its spotify_url was matched against?

    True when *either* provenance field carries a non-blank value. The gate is
    deliberately this weak. Measured on the production artifact (2026-09-25) over
    the 46,907 `albums` rows with a non-empty spotify_url:

    - 28,238 rows (60.2%) hold a real artist and title. They pass.
    - 18,281 rows (39.0%) hold a writer *tag* rather than an artist
      (``backfill-wiki (spotify)``, ``llm+wikidata``, ``web-search (spotify)``, ...)
      and an empty title -- the one eight-day 2026-04 campaign described in
      LML#1353. Their measured defect is URL *shape* (they are Spotify artist
      pages), which is fixed at the serve seam, not absent provenance. Nulling 39%
      of Spotify coverage on a provenance technicality is not warranted by
      anything measured, so one non-blank field is enough here.
    - 388 rows (0.83%) hold neither. 363 of those are *every* compilation row in
      the artifact carrying a Spotify URL, written by the title-only override in
      ``scripts/search_unmatched_compilations.py`` that discards the provenance it
      computed (LML#1353). An audit of all 363 against Spotify's public og tags
      found ~18 of the 28 lowest-scoring were plainly the wrong album --
      ``Simple Machines`` -> Shinedown "Simple Man", ``Sweet Lies`` -> Anita Baker
      "Sweet Love". Those are the rows this returns False for.

    Why negative evidence rather than the identity cross-check LML#1352 suggests
    first: an agreement test against ``entity.release_identity`` needs *positive*
    evidence that this population mostly lacks -- 96.0% of the 21,093 ``/album/``
    URLs Backend-Service serves have no matcher row at all in
    ``lml_cache.album_streaming_url_cache`` -- so it would demote essentially
    everything and delete ~20,000 working links. And an 80/80 string floor cannot
    be the gate either: per LML#1147 the artist axis carries no signal for shelf
    credits, so it false-rejects correct links (Lower Dens/Nootropics,
    Don Covay's expanded credit, the Kollektion 04 curator credit). "No record of
    what was compared" is the only claim that can be made without either.
    """
    return bool((matched_artist or "").strip()) or bool((matched_title or "").strip())


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Casefolded column names of `table`, or an empty set if it does not exist.

    Casefolded because SQLite resolves column names case-insensitively while
    ``PRAGMA table_info`` reports whatever case they were DECLARED in. A
    case-sensitive comparison would fail open on a re-declared artifact: the gate
    would go inert on a database whose columns it could in fact have read.

    Deliberately NOT imported from ``routers/admin.py``, which holds a twin of this
    helper: ``discogs-etl/scripts/sync-library.sh`` runs this script under that
    repo's bare venv, where importing the FastAPI-dependent ``routers`` package
    would fail. Deduplicating the two would break the daily library sync.
    """
    return {row[1].casefold() for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def main(args: argparse.Namespace) -> None:
    if not os.path.exists(args.streaming_db):
        log.error(f"Streaming database {args.streaming_db} does not exist; skipping export")
        return

    # Read-ONLY on purpose, and structurally rather than by convention. Opening a
    # SQLite file read-write performs hot-journal rollback at open time, so if the
    # upstream streaming pipeline died mid-transaction, merely connecting would
    # mutate the bucket-canonical artifact. `mode=ro` turns that into a loud refusal
    # instead of a silent repair of a file that is expensive to recollect.
    sa = sqlite3.connect(f"file:{args.streaming_db}?mode=ro", uri=True)
    lib = sqlite3.connect(args.library_db)

    # The provenance gate reads two columns that older/fixture-shaped databases do
    # not have (discogs-etl's sync e2e fixture is one). Absent columns mean "this
    # database cannot answer the question", NOT "no row has provenance" -- reading
    # it the second way would strip every Spotify URL from such a file.
    album_columns = _table_columns(sa, "albums")
    provenance_gate_active = {c.casefold() for c in SPOTIFY_PROVENANCE_COLUMNS} <= album_columns
    if provenance_gate_active:
        provenance_select = ", ".join(SPOTIFY_PROVENANCE_COLUMNS)
    else:
        log.warning(
            f"albums table lacks {' / '.join(SPOTIFY_PROVENANCE_COLUMNS)}; "
            "the Spotify match-provenance gate is inert for this database"
        )
        provenance_select = "NULL, NULL"

    # Get all albums with at least one streaming URL
    rows = sa.execute(f"""
        SELECT library_ids, spotify_url, apple_url, deezer_url,
               bandcamp_url, tidal_url, youtube_music_url, soundcloud_url,
               {provenance_select}
        FROM albums
        WHERE spotify_url IS NOT NULL
           OR apple_url IS NOT NULL
           OR deezer_url IS NOT NULL
           OR bandcamp_url IS NOT NULL
           OR tidal_url IS NOT NULL
           OR youtube_music_url IS NOT NULL
           OR soundcloud_url IS NOT NULL
    """).fetchall()
    log.info(f"Albums with streaming URLs: {len(rows)}")

    # Map to library release IDs
    links: dict[int, dict] = {}
    spotify_skipped_no_provenance = 0
    for (
        lib_ids_json,
        spotify,
        apple,
        deezer,
        bandcamp,
        tidal,
        ytmusic,
        soundcloud,
        spotify_matched_artist,
        spotify_matched_title,
    ) in rows:
        # LML#1352: refuse a spotify_url whose match provenance is entirely absent.
        # Downstream labels it "verified", and these rows carry no record of what was
        # ever compared. Spotify-only on purpose: cross-artist URL sharing in what is
        # SERVED is a Spotify anomaly (1.78%, vs Apple 0.13% and Discogs 0.40%), and
        # every other service's URL for the same row is left untouched.
        if (
            provenance_gate_active
            and spotify
            and not _has_match_provenance(spotify_matched_artist, spotify_matched_title)
        ):
            spotify = None
            spotify_skipped_no_provenance += 1

        lib_ids = json.loads(lib_ids_json)
        for lib_id in lib_ids:
            if lib_id not in links:
                links[lib_id] = {}
            entry = links[lib_id]
            if spotify and "spotify_url" not in entry:
                entry["spotify_url"] = spotify
            if apple and "apple_music_url" not in entry:
                entry["apple_music_url"] = apple
            if deezer and "deezer_url" not in entry:
                entry["deezer_url"] = deezer
            if bandcamp and "bandcamp_url" not in entry:
                entry["bandcamp_url"] = bandcamp
            if tidal and "tidal_url" not in entry:
                entry["tidal_url"] = tidal
            if ytmusic and "youtube_music_url" not in entry:
                entry["youtube_music_url"] = ytmusic
            if soundcloud and "soundcloud_url" not in entry:
                entry["soundcloud_url"] = soundcloud

    if provenance_gate_active:
        # ALBUM ROWS, not exported URLs. The two differ: a row can carry an empty
        # `library_ids`, several rows can share a `library_id`, and the track
        # supplement below can refill a slot this gate emptied. Treat it as "how
        # much the gate fired", not as the `streaming_links.spotify_url` delta.
        log.info(
            "Album rows whose spotify_url was skipped for "
            f"absent match provenance: {spotify_skipped_no_provenance}"
        )

    log.info(f"Library release IDs with streaming links (album-level): {len(links)}")

    # Supplement with track-level results for singles and compilations
    #
    # NOTE (unchanged here, measured and reported for routing): this block writes a
    # track-shaped `tr.spotify_url` into `entry["spotify_url"]`, which is an *album*
    # field. That is the origin of the 841 `/track/`-shaped values Backend-Service
    # serves (WXYC/Backend-Service#2689). Measured on the 2026-09-25 production
    # artifact, 3,638 albums get their spotify_url ONLY from this supplement, so
    # removing or reshaping it is a coverage decision, not a bug fix, and it is not
    # made in this change. Note the interaction with the provenance gate above: a
    # gated album leaves its spotify slot empty, so a gated row that has a resolved
    # track can be refilled from here. `tests/unit/test_export_streaming_links.py`
    # pins that behavior so it cannot drift silently while the decision is pending.
    #
    # Check if track_results table exists
    has_tracks = sa.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='track_results'"
    ).fetchone()
    track_links_added = 0
    if has_tracks:
        track_rows = sa.execute("""
            SELECT a.library_ids, tr.spotify_url, tr.deezer_url
            FROM track_results tr
            JOIN albums a ON a.id = tr.album_id
            WHERE tr.resolution_status IN ('local_match', 'api_match')
              AND (tr.spotify_url IS NOT NULL OR tr.deezer_url IS NOT NULL)
        """).fetchall()
        for lib_ids_json, spotify, deezer in track_rows:
            lib_ids = json.loads(lib_ids_json)
            for lib_id in lib_ids:
                if lib_id not in links:
                    links[lib_id] = {}
                entry = links[lib_id]
                if spotify and "spotify_url" not in entry:
                    entry["spotify_url"] = spotify
                    track_links_added += 1
                if deezer and "deezer_url" not in entry:
                    entry["deezer_url"] = deezer
                    track_links_added += 1
        log.info(f"Track-level URLs added: {track_links_added}")

    # A release whose only URL was a gated Spotify one now has an empty entry.
    # Drop it rather than inserting an all-NULL streaming_links row: that row would
    # read as "on streaming" to /admin/upload-library-db's streaming diff
    # (`_get_streaming_ids` selects library_id with no URL predicate) while carrying
    # no link at all. The gate is the main way to get here but not the only one: the
    # SELECT above filters on IS NOT NULL while the assignments above test
    # truthiness, so a row whose URL columns are empty STRINGS already reached this
    # path before this change. Hence the neutral wording below -- the drop is not
    # attributed to the gate, since it cannot tell the two apart.
    empty = [lib_id for lib_id, entry in links.items() if not entry]
    for lib_id in empty:
        del links[lib_id]
    if empty:
        log.info(f"Library release IDs dropped for carrying no URL at all: {len(empty)}")

    log.info(f"Library release IDs with streaming links (total): {len(links)}")
    sa.close()

    if args.dry_run:
        # Show stats
        from collections import Counter

        service_counts: Counter[str] = Counter()
        for entry in links.values():
            for k in entry:
                service_counts[k] += 1
        log.info("Service coverage:")
        for service, count in service_counts.most_common():
            log.info(f"  {service}: {count:,}")
        return

    # Create/replace streaming_links table in library.db
    lib.execute("DROP TABLE IF EXISTS streaming_links")
    lib.execute("""
        CREATE TABLE streaming_links (
            library_id INTEGER PRIMARY KEY,
            spotify_url TEXT,
            apple_music_url TEXT,
            deezer_url TEXT,
            bandcamp_url TEXT,
            tidal_url TEXT,
            youtube_music_url TEXT,
            soundcloud_url TEXT
        )
    """)

    inserted = 0
    for lib_id, entry in links.items():
        lib.execute(
            "INSERT OR IGNORE INTO streaming_links "
            "(library_id, spotify_url, apple_music_url, deezer_url, "
            "bandcamp_url, tidal_url, youtube_music_url, soundcloud_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lib_id,
                entry.get("spotify_url"),
                entry.get("apple_music_url"),
                entry.get("deezer_url"),
                entry.get("bandcamp_url"),
                entry.get("tidal_url"),
                entry.get("youtube_music_url"),
                entry.get("soundcloud_url"),
            ),
        )
        inserted += 1
        if inserted % 10000 == 0:
            lib.commit()

    lib.commit()
    log.info(f"Inserted {inserted:,} streaming_links rows into {args.library_db}")

    # Verify
    count = lib.execute("SELECT COUNT(*) FROM streaming_links").fetchone()[0]
    log.info(f"Verified: {count:,} rows in streaming_links table")
    lib.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export streaming URLs to library.db")
    parser.add_argument(
        "--library-db", default="library.db", help="Path to library.db (default: library.db)"
    )
    parser.add_argument(
        "--streaming-db",
        default="streaming_availability.db",
        help="Path to streaming_availability.db (default: streaming_availability.db)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show stats without writing")
    args = parser.parse_args()
    main(args)
