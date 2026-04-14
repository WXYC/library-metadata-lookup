"""CLI entry point for entity resolution.

Usage:
    python -m scripts.entity_resolution [--library-db PATH] [--batch-size N] [--skip-wikidata] [--skip-musicbrainz]

Environment variables:
    DATABASE_URL_DISCOGS    -- Required. PostgreSQL URL for discogs-cache (entity store lives here).
    DATABASE_URL_WIKIDATA   -- Optional. PostgreSQL URL for wikidata-cache.
    DATABASE_URL_MUSICBRAINZ -- Optional. PostgreSQL URL for musicbrainz-cache.
    LIBRARY_DB_PATH         -- Path to library.db SQLite file (default: library.db).

The reconciliation pipeline:
1. Seeds entity.identity with all distinct artist names from library.db
2. Discogs batch matching (exact -> member/group -> alias -> name variation)
3. Wikidata QID bridging (cache -> SPARQL fallback) + name search for no_match
4. Streaming ID fetch (Spotify, Apple Music, Bandcamp) via Wikidata
5. MusicBrainz matching (QID bridge -> direct name match)
6. Deduplication by shared Wikidata QID
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import aiosqlite
from dotenv import load_dotenv

from wxyc_catalog.sources import PgSource, SparqlSource

from scripts.entity_resolution.dedup import EntityDeduplicator
from scripts.entity_resolution.discogs import DiscogsReconciler
from scripts.entity_resolution.musicbrainz import MusicBrainzReconciler
from scripts.entity_resolution.store import EntityStore
from scripts.entity_resolution.wikidata import WikidataReconciler

logger = logging.getLogger(__name__)

_LIBRARY_ARTISTS_SQL = "SELECT DISTINCT artist FROM library WHERE artist IS NOT NULL ORDER BY artist"


async def get_library_artists(db_path: str) -> list[str]:
    """Get all distinct artist names from the library SQLite database."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(_LIBRARY_ARTISTS_SQL)
        rows = await cursor.fetchall()
        return [row[0] for row in rows if row[0]]


async def seed_identities(store: EntityStore, artists: list[str]) -> int:
    """Seed entity.identity with library artist names. Returns count of new rows."""
    seeded = 0
    for artist in artists:
        result = await store.upsert_identity(library_name=artist)
        if result is not None:
            seeded += 1
    return seeded


async def run_discogs_stage(
    store: EntityStore,
    reconciler: DiscogsReconciler,
    batch_size: int,
) -> tuple[int, int]:
    """Run Discogs reconciliation for unreconciled identities. Returns (matched, no_match)."""
    identities = await store.get_identities_by_status("unreconciled")
    if not identities:
        logger.info("No unreconciled identities for Discogs stage")
        return 0, 0

    names = [i.library_name for i in identities]
    name_to_id = {i.library_name: i.id for i in identities}

    matched_count = 0
    no_match_count = 0

    for i in range(0, len(names), batch_size):
        batch = names[i : i + batch_size]
        matches = await reconciler.reconcile_batch(batch)

        for name in batch:
            identity_id = name_to_id[name]
            if name in matches:
                match = matches[name]
                await store.upsert_identity(
                    library_name=name,
                    discogs_artist_id=match.discogs_artist_id,
                )
                await store.update_status(identity_id, "reconciled")
                await store.log_reconciliation(
                    identity_id=identity_id,
                    source="discogs",
                    external_id=str(match.discogs_artist_id),
                    method=match.method,
                    confidence=1.0,
                )
                matched_count += 1
            else:
                await store.update_status(identity_id, "no_match")
                no_match_count += 1

    logger.info("Discogs: %d matched, %d no_match", matched_count, no_match_count)
    return matched_count, no_match_count


async def run_wikidata_stage(
    store: EntityStore,
    reconciler: WikidataReconciler,
) -> tuple[int, int, int]:
    """Run Wikidata reconciliation. Returns (qid_bridged, name_searched, streaming_fetched)."""
    # Stage 1: QID bridging for reconciled identities with discogs_artist_id
    reconciled = await store.get_identities_by_status("reconciled")
    need_qid = [i for i in reconciled if i.discogs_artist_id and not i.wikidata_qid]

    qid_bridged = 0
    if need_qid:
        discogs_ids = {i.discogs_artist_id for i in need_qid}
        id_to_identity = {i.discogs_artist_id: i for i in need_qid}
        qid_map = await reconciler.resolve_qids_from_discogs_ids(discogs_ids)

        for discogs_id, qid in qid_map.items():
            identity = id_to_identity.get(discogs_id)
            if identity:
                await store.upsert_identity(
                    library_name=identity.library_name,
                    wikidata_qid=qid,
                )
                await store.log_reconciliation(
                    identity_id=identity.id,
                    source="wikidata",
                    external_id=qid,
                    method="discogs_bridge",
                )
                qid_bridged += 1

    # Stage 2: Name search for no_match identities
    no_match = await store.get_identities_by_status("no_match")
    name_searched = 0
    for identity in no_match:
        qid = await reconciler.search_musician_by_name(identity.library_name)
        if qid:
            await store.upsert_identity(
                library_name=identity.library_name,
                wikidata_qid=qid,
            )
            await store.update_status(identity.id, "reconciled")
            await store.log_reconciliation(
                identity_id=identity.id,
                source="wikidata",
                external_id=qid,
                method="name_search",
            )
            name_searched += 1

    # Stage 3: Streaming IDs for all identities with QIDs
    all_identities = await store.get_identities_by_status("reconciled")
    need_streaming = [
        i for i in all_identities
        if i.wikidata_qid and not (i.spotify_artist_id or i.apple_music_artist_id or i.bandcamp_id)
    ]
    streaming_fetched = 0
    if need_streaming:
        qids = [i.wikidata_qid for i in need_streaming]
        qid_to_identity = {i.wikidata_qid: i for i in need_streaming}
        streaming_map = await reconciler.fetch_streaming_ids(qids)

        for qid, ids in streaming_map.items():
            identity = qid_to_identity.get(qid)
            if identity:
                await store.upsert_identity(
                    library_name=identity.library_name,
                    spotify_artist_id=ids.spotify_artist_id,
                    apple_music_artist_id=ids.apple_music_artist_id,
                    bandcamp_id=ids.bandcamp_id,
                )
                streaming_fetched += 1

    logger.info(
        "Wikidata: %d QID bridged, %d name searched, %d streaming fetched",
        qid_bridged, name_searched, streaming_fetched,
    )
    return qid_bridged, name_searched, streaming_fetched


async def run_musicbrainz_stage(
    store: EntityStore,
    reconciler: MusicBrainzReconciler,
) -> int:
    """Run MusicBrainz reconciliation. Returns count of MB IDs resolved."""
    reconciled = await store.get_identities_by_status("reconciled")
    need_mb = [i for i in reconciled if not i.musicbrainz_artist_id]

    mb_resolved = 0

    # Stage 1: QID -> MBID bridge
    with_qid = [i for i in need_mb if i.wikidata_qid]
    if with_qid:
        qids = {i.wikidata_qid for i in with_qid}
        qid_to_identity = {i.wikidata_qid: i for i in with_qid}
        qid_results = await reconciler.resolve_from_qids(qids)

        for qid, mbid in qid_results.items():
            identity = qid_to_identity.get(qid)
            if identity:
                await store.upsert_identity(
                    library_name=identity.library_name,
                    musicbrainz_artist_id=mbid,
                )
                await store.log_reconciliation(
                    identity_id=identity.id,
                    source="musicbrainz",
                    external_id=mbid,
                    method="qid_bridge",
                )
                mb_resolved += 1

    # Stage 2: Direct name match for remaining
    if with_qid and qid_results:
        resolved_names = {qid_to_identity[q].library_name for q in qid_results}
        still_need = [i for i in need_mb if i.library_name not in resolved_names]
    else:
        still_need = list(need_mb)
    if still_need:
        names = [i.library_name for i in still_need]
        name_to_identity = {i.library_name: i for i in still_need}
        name_results = await reconciler.resolve_from_names(names)

        for name, mbid in name_results.items():
            identity = name_to_identity.get(name)
            if identity:
                await store.upsert_identity(
                    library_name=identity.library_name,
                    musicbrainz_artist_id=mbid,
                )
                await store.log_reconciliation(
                    identity_id=identity.id,
                    source="musicbrainz",
                    external_id=mbid,
                    method="name_match",
                )
                mb_resolved += 1

    logger.info("MusicBrainz: %d resolved", mb_resolved)
    return mb_resolved


async def run_dedup_stage(store_pg: PgSource) -> int:
    """Run deduplication. Returns count of merged groups."""
    dedup = EntityDeduplicator(store_pg)
    groups = await dedup.find_duplicate_groups()
    for qid, identities in groups:
        await dedup.merge_group(qid, identities)
    logger.info("Dedup: %d groups merged", len(groups))
    return len(groups)


async def main(args: argparse.Namespace) -> None:
    """Run the full entity resolution pipeline."""
    load_dotenv()

    database_url = os.getenv("DATABASE_URL_DISCOGS")
    if not database_url:
        logger.error("DATABASE_URL_DISCOGS is required")
        sys.exit(1)

    library_db_path = args.library_db or os.getenv("LIBRARY_DB_PATH", "library.db")
    database_url_wikidata = os.getenv("DATABASE_URL_WIKIDATA")
    database_url_musicbrainz = os.getenv("DATABASE_URL_MUSICBRAINZ")

    # Create sources
    discogs_pg = PgSource(database_url)
    wikidata_pg = PgSource(database_url_wikidata) if database_url_wikidata else None
    musicbrainz_pg = PgSource(database_url_musicbrainz) if database_url_musicbrainz else None
    sparql = SparqlSource()

    store = EntityStore(discogs_pg)
    discogs_reconciler = DiscogsReconciler(discogs_pg, batch_size=args.batch_size)
    wikidata_reconciler = WikidataReconciler(sparql=sparql, wikidata_pg=wikidata_pg)
    musicbrainz_reconciler = MusicBrainzReconciler(mb_pg=musicbrainz_pg, wikidata_pg=wikidata_pg)

    try:
        # Step 1: Seed from library
        logger.info("Loading artists from %s", library_db_path)
        artists = await get_library_artists(library_db_path)
        logger.info("Found %d distinct artists in library", len(artists))

        seeded = await seed_identities(store, artists)
        logger.info("Seeded %d identities", seeded)

        # Step 2: Discogs reconciliation
        logger.info("Running Discogs reconciliation...")
        discogs_matched, discogs_no_match = await run_discogs_stage(
            store, discogs_reconciler, args.batch_size
        )

        # Step 3: Wikidata reconciliation
        if not args.skip_wikidata:
            logger.info("Running Wikidata reconciliation...")
            await run_wikidata_stage(store, wikidata_reconciler)
        else:
            logger.info("Skipping Wikidata reconciliation")

        # Step 4: MusicBrainz reconciliation
        if not args.skip_musicbrainz and musicbrainz_pg is not None:
            logger.info("Running MusicBrainz reconciliation...")
            await run_musicbrainz_stage(store, musicbrainz_reconciler)
        else:
            logger.info("Skipping MusicBrainz reconciliation")

        # Step 5: Deduplication
        logger.info("Running deduplication...")
        await run_dedup_stage(discogs_pg)

        # Summary
        total = len(artists)
        reconciled = await store.get_identities_by_status("reconciled")
        no_match = await store.get_identities_by_status("no_match")
        rate = len(reconciled) / total * 100 if total > 0 else 0
        logger.info(
            "Done. %d/%d reconciled (%.1f%%), %d no_match",
            len(reconciled), total, rate, len(no_match),
        )

    finally:
        await discogs_pg.close()
        if wikidata_pg:
            await wikidata_pg.close()
        if musicbrainz_pg:
            await musicbrainz_pg.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Entity resolution: reconcile WXYC library artists to external IDs"
    )
    parser.add_argument(
        "--library-db",
        help="Path to library.db SQLite file (default: LIBRARY_DB_PATH env or library.db)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Batch size for Discogs reconciliation (default: 1000)",
    )
    parser.add_argument(
        "--skip-wikidata",
        action="store_true",
        help="Skip Wikidata reconciliation stage",
    )
    parser.add_argument(
        "--skip-musicbrainz",
        action="store_true",
        help="Skip MusicBrainz reconciliation stage",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main(args))
