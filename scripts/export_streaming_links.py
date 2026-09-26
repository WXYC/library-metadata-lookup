"""Export streaming URLs from streaming_availability.db into library.db.

Creates a streaming_links table in library.db that maps library release IDs
to verified streaming service URLs. This makes streaming data available to
the LML API and all downstream clients.

The word "verified" is load-bearing downstream: ``lookup/enrichment/item.py``
serves whatever lands here and labels it ``streaming_status.spotify = "verified"``
after a host + well-formedness check only (LML#1352). So this script is the last
place that can refuse a URL for which no match evidence exists at all --
see ``_has_match_provenance`` for the one such refusal it makes.

This script only ever READS streaming_availability.db, structurally -- see the
``mode=ro`` open in ``main`` for why that is the open mode rather than a convention.

Callers, neither of which tolerates this script mutating the artifact:

- ``discogs-etl/scripts/sync-library.sh``, in the daily library sync, WITHOUT
  ``--dry-run``. Its ``tests/e2e/test_sync_library_e2e.py`` also loads ``main``
  dynamically.
- this repo's ``.github/workflows/refresh-streaming.yml`` "Verify export" step, WITH
  ``--dry-run``. It runs between the pipeline writing the artifact and
  ``POST /admin/upload-streaming-db`` pushing it back to the canonical bucket -- so a
  read-write open here could have repaired-then-uploaded a silently altered artifact.

Usage:
    .venv/bin/python scripts/export_streaming_links.py [--library-db PATH] [--streaming-db PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from collections import Counter
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# The two `albums` columns that record what a stored spotify_url was matched against.
# A row with neither records no comparison at all: it may still carry a
# `spotify_confidence` score, but a score with no record of the strings that produced
# it cannot be audited without re-fetching the Spotify page.
#
# Entries must stay lowercase ASCII -- they are the target `_ascii_fold` compares
# declared column names against, and a mixed-case entry here would silently fail the
# subset check and leave the gate inert.
SPOTIFY_PROVENANCE_COLUMNS = ("spotify_matched_artist", "spotify_matched_title")


def _has_match_provenance(matched_artist: str | None, matched_title: str | None) -> bool:
    """Does this row record anything about what its spotify_url was matched against?

    True when *either* provenance field carries a non-blank value. The gate is
    deliberately this weak.

    The census below partitions the column on PROVENANCE ONLY. It is deliberately
    silent about whether the URL is well-shaped or even on a Spotify host -- a
    separate and larger defect class, owned by the serve seam's per-service host
    check and by the sibling album-shape guard. Read it as "what evidence does the
    row carry", not as an account of the column's health.

    Measured on the production artifact (2026-09-25) over the 46,907 `albums` rows
    with a non-empty spotify_url:

    - 28,238 rows (60.2%) hold a real artist and title. They pass.
    - 18,281 rows (39.0%) hold a writer *tag* rather than an artist
      (``backfill-wiki (spotify)``, ``llm+wikidata``, ``web-search (spotify)``, ...)
      and an empty title -- the one eight-day 2026-04 campaign described in
      LML#1353. Their measured defect is URL *shape* (they are Spotify artist
      pages), which is fixed at the serve seam, not absent provenance. Gating them
      would null 39% of the artifact's STORED spotify_url rows on a provenance
      technicality -- a smaller number of *served* URLs, since the serve seam
      already nulls the off-host ones, but not a number anything measured
      justifies. So one non-blank field is enough here.
    - 388 rows (0.83%) hold neither. 363 of those are *every* compilation row in
      the artifact carrying a Spotify URL, written by
      ``scripts/search_unmatched_compilations.py``, whose single Spotify UPDATE omits
      the provenance columns on BOTH its match paths -- the title-only fallback
      additionally computes a ``matched_title`` and then discards it (LML#1353). An
      audit of all 363 against Spotify's public og tags found ~18 of the 28
      LOWEST-SCORING were plainly the wrong album -- ``Simple Machines`` ->
      Shinedown "Simple Man", ``Sweet Lies`` -> Anita Baker "Sweet Love". Only that
      bottom band was audited, so that ratio is not a precision estimate for the
      whole 388. Those are the rows this returns False for.

    Why negative evidence rather than the identity cross-check LML#1352 suggests
    first: an agreement test against ``entity.release_identity`` needs *positive*
    evidence that this population mostly lacks -- 96.0% of the 21,093 ``/album/``
    URLs Backend-Service serves have no matcher row at all in
    ``lml_cache.album_streaming_url_cache`` -- so it would demote essentially
    everything and delete ~20,000 working links.

    A string floor is not available at this seam for a structural reason rather than
    an accuracy one: the export never reads ``display_artist``/``display_title``, so
    there is nothing here to score the stored provenance AGAINST. That is the entire
    argument. It is deliberately NOT "a floor would be too permissive here", which
    would be a claim about scores this seam cannot compute.

    (An earlier draft did argue it that way, citing three named shelf-credit shapes as
    80/80 false-rejects. Those figures are real but they belong to the serve seam's
    comparison -- the library credit against the artist a URL resolves to -- not to
    this one, which only ever sees the artifact's own stored strings. The seam
    distinction and the artist-axis asymmetry LML#1147 describes live on LML#1352,
    deliberately not restated here.) "No record of what was compared" is the only
    claim this seam can make.
    """
    return bool((matched_artist or "").strip()) or bool((matched_title or "").strip())


def _ascii_fold(name: str) -> str:
    """Lowercase `name` only when it is pure ASCII, for comparison against an ASCII target.

    NOT a faithful model of SQLite's identifier folding, which is per-character: this
    is all-or-nothing on the whole string, so a mixed name like ``ſpotify_Matched_Artist``
    comes back untouched where SQLite would still fold its ASCII half. That is
    sufficient here for a structural reason worth stating, since it is what makes the
    helper safe rather than merely adequate: the only targets are the pure-ASCII
    ``SPOTIFY_PROVENANCE_COLUMNS``, and SQLite's fold never maps a non-ASCII byte onto
    an ASCII one, so no non-ASCII declared name can ever fold onto one of them. The
    comparison therefore cannot fail open OR crash. Do not reuse this against a
    non-ASCII target without revisiting that.

    Python's own folds are what this avoids: ``str.casefold()`` maps U+017F onto ``s``
    and ``str.lower()`` maps U+212A onto ``k``, either of which would make the gate
    activate on a column the SELECT cannot resolve -- turning the documented inert
    fallback into a hard crash in the daily sync.
    """
    return name.lower() if name.isascii() else name


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """ASCII-folded column names of `table`, or an empty set if it does not exist.

    Folded because ``PRAGMA table_info`` reports whatever case a column was DECLARED
    in, while the SELECT that reads it resolves case-insensitively. A case-sensitive
    comparison would fail open on a re-declared artifact: the gate would go inert on
    a database whose columns it could in fact have read.

    Deliberately NOT imported from ``routers/admin.py``, which holds a twin of this
    helper: ``discogs-etl/scripts/sync-library.sh`` runs this script under that
    repo's bare venv, where importing the FastAPI-dependent ``routers`` package
    would fail. Deduplicating the two would break the daily library sync. Note the
    twin is NOT folded, so do not read this fix as covering both -- there the same
    fail-open makes ``_streaming_coverage`` read three URL columns as 0 and
    ``POST /admin/upload-streaming-db`` 409 a healthy artifact. Tracked separately;
    it is not in this change's diff.
    """
    return {_ascii_fold(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def main(args: argparse.Namespace) -> None:
    if not os.path.exists(args.streaming_db):
        log.error(f"Streaming database {args.streaming_db} does not exist; skipping export")
        return

    # Read-ONLY on purpose, and structurally rather than by convention. If the upstream
    # streaming pipeline died mid-transaction it leaves a hot journal, and a read-write
    # handle rolls that journal back -- mutating the bucket-canonical artifact, a file
    # expensive enough to recollect that CLAUDE.md names it. (Measured: the rollback
    # fires on the FIRST READ, not at connect; connect itself leaves the file
    # byte-identical either way. The distinction matters only for where the error
    # surfaces, not for whether the mutation happens.)
    #
    # The trade, stated because it is a new failure mode in a cross-repo daily job:
    # where a read-write handle used to roll the journal back and carry on, this now
    # raises at the first read and the file stays byte-identical. That is the intended
    # direction for a precious artifact -- a loud failed sync is recoverable, a
    # silently altered artifact is not -- but it does mean a stray hot journal beside
    # the artifact fails the run rather than being quietly repaired.
    #
    # `as_uri()` rather than an f-string: a `#`, `?` or `%` in the path is
    # significant inside a URI, and interpolating it raw makes SQLite end the path
    # early, drop `mode=ro` into an unparsed fragment, and open a TRUNCATED path
    # READ-WRITE -- losing exactly the guarantee this line exists to make.
    sa = sqlite3.connect(Path(args.streaming_db).resolve().as_uri() + "?mode=ro", uri=True)

    # The provenance gate reads two columns that older/fixture-shaped databases do
    # not have (discogs-etl's sync e2e fixture is one). Absent columns mean "this
    # database cannot answer the question", NOT "no row has provenance" -- reading
    # it the second way would strip every Spotify URL from such a file.
    album_columns = _table_columns(sa, "albums")
    provenance_gate_active = set(SPOTIFY_PROVENANCE_COLUMNS) <= album_columns
    if provenance_gate_active:
        provenance_select = ", ".join(SPOTIFY_PROVENANCE_COLUMNS)
    else:
        log.warning(
            f"albums table lacks {' / '.join(SPOTIFY_PROVENANCE_COLUMNS)}; "
            "the Spotify match-provenance gate is inert for this database"
        )
        # Derived rather than a hardcoded "NULL, NULL" so both branches select the same
        # column count. To be clear about what that does and does not buy: the row
        # unpack below still names ten locals, so adding a third provenance column
        # breaks either way. What this changes is that it then breaks on BOTH paths
        # instead of only the gate-active one -- which is the path no legacy fixture
        # and no cross-repo caller exercises, so the break would otherwise pass CI and
        # surface in the daily production sync.
        provenance_select = ", ".join("NULL" for _ in SPOTIFY_PROVENANCE_COLUMNS)

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
        #
        # ROUTED, deliberately not closed here: this predicate keys on provenance being
        # ABSENT, so it says nothing about a row whose provenance is present but
        # ONE-AXIS. Sibling LML#1353's drain work introduces exactly that shape -- a
        # title-only acceptance that DOES record what it compared -- which passes this
        # gate and is then served as a two-axis `verified`. Catching it needs a signal
        # this table does not carry, and hard-coding the sibling's status vocabulary
        # here would couple the two merge orders, so it belongs to the serve seam
        # alongside the `verified` demotion rather than to this refusal.
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

    # Non-empty entries only: the gate can leave a release with an entry and no URL,
    # so a bare len(links) here would not share a denominator with the total below.
    log.info(
        "Library release IDs with streaming links (album-level): "
        f"{sum(1 for entry in links.values() if entry)}"
    )

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
    # no link at all.
    #
    # Applies regardless of `provenance_gate_active`: an all-NULL row is wrong
    # whatever emptied it. The gate is the main way to get here but not the only one
    # -- the SELECT above filters on IS NOT NULL while the assignments test
    # truthiness, so a row whose URL columns are empty STRINGS already reached this
    # path. That cohort measured zero on the 2026-09-25 artifact, so in practice
    # these are gate casualties; the wording stays neutral because the code cannot
    # tell the two apart.
    #
    # ROUTED, not handled here: dropping a library_id is itself a signal. The LML#1313
    # streaming webhook turns `old_ids - new_ids` into `{on_streaming: false}` for
    # Backend-Service, which is a cross-repo flip for every release the gate empties,
    # and `on_streaming: null` (the honest value for a row just declared unauditable)
    # is in the wire contract but never emitted. Left to the serve-seam decision along
    # with the `verified` demotion, since both want a provenance signal this table
    # does not carry.
    empty = [lib_id for lib_id, entry in links.items() if not entry]
    for lib_id in empty:
        del links[lib_id]
    if empty:
        log.info(f"Library release IDs dropped for carrying no URL at all: {len(empty)}")

    log.info(f"Library release IDs with streaming links (total): {len(links)}")
    sa.close()

    # Per-service coverage on BOTH paths, not only --dry-run. NOTHING downstream
    # guards `streaming_links.spotify_url` -- see the three upload/floor guards
    # enumerated in docs/scripts.md, none of which can observe this column -- so a
    # provenance column that is present but unpopulated would strip Spotify wholesale
    # and exit 0 silently. This log is the daily sync's only tell: `spotify_url: 0`.
    service_counts: Counter[str] = Counter()
    for entry in links.values():
        for k in entry:
            service_counts[k] += 1
    log.info("Service coverage:")
    for service, count in service_counts.most_common():
        log.info(f"  {service}: {count:,}")

    if args.dry_run:
        return

    # Opened here rather than at the top so --dry-run genuinely writes nothing: a
    # connect() creates the file, which left a 0-byte library.db beside the artifact
    # whenever the documented sizing command ran in a fresh directory.
    lib = sqlite3.connect(args.library_db)

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
