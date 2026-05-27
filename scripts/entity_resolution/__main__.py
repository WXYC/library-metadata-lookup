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

from scripts.entity_resolution.dedup import EntityDeduplicator
from scripts.entity_resolution.discogs import DiscogsReconciler
from scripts.entity_resolution.musicbrainz import MusicBrainzReconciler
from scripts.entity_resolution.sources import PgSource, SparqlSource
from scripts.entity_resolution.store import EntityStore
from scripts.entity_resolution.wikidata import WikidataReconciler

logger = logging.getLogger(__name__)

# Default threshold knobs for `prune_orphan_identities`. Calibrated against
# the ~24K-row prod baseline: the absolute floor (100) lets day-to-day name
# drift through; the 10% fractional cap (~2,400 rows) bounds the worst-case
# bulk-rename run. Both are overridable via CLI flags.
_DEFAULT_ORPHAN_THRESHOLD_FRAC = 0.10
_DEFAULT_ORPHAN_THRESHOLD_ABS = 100

_LIBRARY_ARTISTS_SQL = (
    "SELECT DISTINCT artist FROM library WHERE artist IS NOT NULL ORDER BY artist"
)


class OrphanDrainAbortError(RuntimeError):
    """Raised when orphan count exceeds the drain-safety threshold.

    ``main()`` catches this, logs at ERROR with the orphan count + sample
    names, and exits non-zero. The operator re-runs with
    ``--allow-orphan-drain`` after confirming the ``library.db`` snapshot
    is correct. Guards LML#377's failure mode where a corrupted / partial
    library export would otherwise look like "every artist was renamed at
    once" and silently wipe every ``entity.reconciliation_log`` row.
    """


async def get_library_artists(db_path: str) -> list[str]:
    """Get all distinct artist names from the library SQLite database."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(_LIBRARY_ARTISTS_SQL)
        rows = await cursor.fetchall()
        return [row[0] for row in rows if row[0]]


async def prune_orphan_identities(
    store: EntityStore,
    current_names: set[str],
    *,
    allow_orphan_drain: bool = False,
    threshold_frac: float = _DEFAULT_ORPHAN_THRESHOLD_FRAC,
    threshold_abs: int = _DEFAULT_ORPHAN_THRESHOLD_ABS,
) -> tuple[int, int, list[str]]:
    """Remove or merge ``entity.identity`` rows no longer in the snapshot.

    Computes ``stored_names - current_names`` and resolves each orphan via a
    canonical-form lookup against ``current_names``: when exactly one current
    name canonicalizes to the same form as the orphan, the orphan's
    reconciliation_log provenance and external IDs are merged into the
    current row; otherwise the orphan is hard-deleted.

    Args:
        store: EntityStore wired to the discogs-cache database.
        current_names: Set of ``library.artist`` values from the current
            ``library.db`` snapshot.
        allow_orphan_drain: When False (default), raises ``OrphanDrainAbortError``
            if orphan count exceeds ``max(threshold_abs, threshold_frac *
            len(current_names))``. Pass True for an authorized one-shot
            bulk-rename run.
        threshold_frac: Fraction of the current snapshot above which the
            drain guard fires.
        threshold_abs: Absolute floor; the guard fires above
            ``max(threshold_abs, threshold_frac * len(current_names))``.

    Returns:
        ``(merged_count, deleted_count, orphan_names)``.

    Raises:
        OrphanDrainAbortError: When the orphan count crosses the threshold and
            ``allow_orphan_drain`` is False.
    """
    # Local import keeps the wxyc_etl Rust extension off the module-load
    # path for callers that don't reach this code path.
    from identity.normalize import canonicalize_for_identity_lookup

    stored_names = await store.fetch_all_identity_library_names()
    orphans = sorted(stored_names - current_names)

    if not orphans:
        logger.info("Orphan pass: no orphans (stored=%d)", len(stored_names))
        return (0, 0, [])

    threshold = max(threshold_abs, int(threshold_frac * len(current_names)))
    if len(orphans) > threshold and not allow_orphan_drain:
        sample = orphans[:20]
        logger.error(
            "Orphan pass: %d orphans exceeds threshold %d (current=%d, stored=%d). "
            "Sample: %s. Re-run with --allow-orphan-drain after verifying the "
            "library.db snapshot is correct.",
            len(orphans),
            threshold,
            len(current_names),
            len(stored_names),
            sample,
        )
        raise OrphanDrainAbortError(f"{len(orphans)} orphans exceeds threshold {threshold}")

    # Build a canonical-form index over current names so each orphan's merge
    # target lookup is O(1). Collisions (multiple current names sharing one
    # canonical form) get tracked here so the orphan pass falls through to
    # delete rather than guessing.
    canonical_to_current: dict[str, list[str]] = {}
    for name in current_names:
        c = canonicalize_for_identity_lookup(name)
        if c:
            canonical_to_current.setdefault(c, []).append(name)

    merged_count = 0
    deleted_count = 0
    for orphan in orphans:
        c = canonicalize_for_identity_lookup(orphan)
        candidates = canonical_to_current.get(c, []) if c else []
        if len(candidates) == 1:
            target_name = candidates[0]
            target = await store.get_identity(target_name)
            if target is not None:
                did_merge = await store.merge_identity_by_library_name(
                    from_name=orphan, into_id=target.id
                )
                if did_merge:
                    logger.info(
                        "Orphan pass merge: %r -> %r (into id=%d)",
                        orphan,
                        target_name,
                        target.id,
                    )
                    merged_count += 1
                    continue
                # merge_identity_by_library_name returned False — orphan row
                # was already gone (concurrent run) or already merged. Skip
                # delete to avoid touching the canonical row by mistake.
                logger.info("Orphan pass skip: %r (no row to merge)", orphan)
                continue
            logger.warning(
                "Orphan pass: canonical target %r for orphan %r not found in store; "
                "falling through to delete",
                target_name,
                orphan,
            )
        elif len(candidates) > 1:
            logger.warning(
                "Orphan pass: orphan %r canonicalizes to %r which matches multiple "
                "current names %s; falling through to delete to avoid ambiguous merge",
                orphan,
                c,
                candidates,
            )

        log_rows = await store.delete_identity_by_library_name(orphan)
        logger.info(
            "Orphan pass delete: %r (removed %d reconciliation_log rows)",
            orphan,
            log_rows,
        )
        deleted_count += 1

    return (merged_count, deleted_count, orphans)


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
        discogs_ids: set[int] = {i.discogs_artist_id for i in need_qid if i.discogs_artist_id}
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
        found_qid = await reconciler.search_musician_by_name(identity.library_name)
        if found_qid:
            await store.upsert_identity(
                library_name=identity.library_name,
                wikidata_qid=found_qid,
            )
            await store.update_status(identity.id, "reconciled")
            await store.log_reconciliation(
                identity_id=identity.id,
                source="wikidata",
                external_id=found_qid,
                method="name_search",
            )
            name_searched += 1

    # Stage 3: Streaming IDs for all identities with QIDs
    all_identities = await store.get_identities_by_status("reconciled")
    need_streaming = [
        i
        for i in all_identities
        if i.wikidata_qid and not (i.spotify_artist_id or i.apple_music_artist_id or i.bandcamp_id)
    ]
    streaming_fetched = 0
    if need_streaming:
        qids: list[str] = [i.wikidata_qid for i in need_streaming if i.wikidata_qid]
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
        qid_bridged,
        name_searched,
        streaming_fetched,
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
        mb_qids: set[str] = {i.wikidata_qid for i in with_qid if i.wikidata_qid}
        qid_to_identity = {i.wikidata_qid: i for i in with_qid}
        qid_results = await reconciler.resolve_from_qids(mb_qids)

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

        # Step 1a: Prune orphans (LML#377). Runs AFTER seed so the merge path
        # has a target row to merge INTO. On a librarian-driven rename
        # ("Beyonce" → "Beyoncé"), seed_identities first creates the empty
        # "Beyoncé" row; the orphan pass then merges "Beyonce"'s accumulated
        # reconciliation_log + external IDs into it, then deletes the "Beyonce"
        # row. If the pass ran before seed, get_identity(new_name) would
        # return None and every rename would fall through to hard-delete,
        # losing the provenance the pass exists to preserve.
        try:
            merged, deleted, orphans = await prune_orphan_identities(
                store,
                set(artists),
                allow_orphan_drain=args.allow_orphan_drain,
                threshold_frac=args.orphan_threshold_frac,
                threshold_abs=args.orphan_threshold_abs,
            )
            logger.info(
                "Orphan pass: %d merged, %d deleted (of %d total orphans)",
                merged,
                deleted,
                len(orphans),
            )
        except OrphanDrainAbortError as exc:
            logger.error("Aborting seeding run: %s", exc)
            sys.exit(1)

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
            len(reconciled),
            total,
            rate,
            len(no_match),
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
        "--allow-orphan-drain",
        action="store_true",
        help=(
            "Bypass the orphan-count safety threshold. Required when a "
            "legitimate bulk rename pushes the orphan count above "
            "max(--orphan-threshold-abs, --orphan-threshold-frac * snapshot)."
        ),
    )
    parser.add_argument(
        "--orphan-threshold-frac",
        type=float,
        default=_DEFAULT_ORPHAN_THRESHOLD_FRAC,
        help=(
            "Fractional cap on orphan count vs current snapshot size "
            f"(default: {_DEFAULT_ORPHAN_THRESHOLD_FRAC})"
        ),
    )
    parser.add_argument(
        "--orphan-threshold-abs",
        type=int,
        default=_DEFAULT_ORPHAN_THRESHOLD_ABS,
        help=(
            "Absolute floor for the orphan threshold; the guard fires above "
            "max(--orphan-threshold-abs, --orphan-threshold-frac * snapshot) "
            f"(default: {_DEFAULT_ORPHAN_THRESHOLD_ABS})"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
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
