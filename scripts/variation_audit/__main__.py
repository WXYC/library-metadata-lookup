"""WXYC artist name variation audit.

Analyzes cross-references and alternate_artist_name entries to determine
which artists sharing a library code should have independent codes.
Cross-references against local Discogs and MusicBrainz datasets.

Usage:
    python -m scripts.variation_audit [OPTIONS]
"""

import argparse
import csv
import logging
from collections import Counter
from pathlib import Path

from scripts.variation_audit.classifier import (
    ALIAS,
    COLLABORATION,
    MEMBER_OF_GROUP,
    SEPARATE_ARTIST,
    SPELLING_VARIANT,
    SPLIT_RELEASE,
    UNRESOLVED,
    RelationshipClassifier,
)
from scripts.variation_audit.loaders import (
    load_cross_references_from_dump,
    load_discogs_aliases,
    load_discogs_members,
    load_graph_db,
    load_library_artists,
    load_library_codes_from_dump,
    load_mb_aliases,
    normalize_name,
)
from scripts.variation_audit.phases import (
    phase_1_cross_references,
    phase_2_alt_names,
    phase_3_duplicates,
    phase_4_missing_links,
)

logger = logging.getLogger("variation_audit")


def write_csv_rows(rows: list[dict], path: Path, fieldnames: list[str]) -> None:
    """Write a list of dicts to a CSV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote %d rows to %s", len(rows), path)


def write_summary(
    phase_1_results: list[dict],
    phase_2_results: list[dict],
    phase_3_results: list[dict],
    phase_4_results: list[dict],
    path: Path,
) -> None:
    """Write a human-readable summary."""
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append("WXYC Artist Name Variation Audit")
    lines.append("=" * 40)
    lines.append("")

    # Phase 1 summary
    p1_counts = Counter(r["classification"] for r in phase_1_results)
    lines.append(f"Phase 1: Cross-References ({len(phase_1_results)} total)")
    lines.append("-" * 40)
    for cls in [
        ALIAS,
        MEMBER_OF_GROUP,
        COLLABORATION,
        SEPARATE_ARTIST,
        SPELLING_VARIANT,
        SPLIT_RELEASE,
        UNRESOLVED,
    ]:
        if p1_counts[cls]:
            lines.append(f"  {cls}: {p1_counts[cls]}")
    lines.append("")

    # Phase 2 summary
    p2_counts = Counter(r["classification"] for r in phase_2_results)
    lines.append(f"Phase 2: Alternate Artist Names ({len(phase_2_results)} total)")
    lines.append("-" * 40)
    for cls in [
        ALIAS,
        MEMBER_OF_GROUP,
        COLLABORATION,
        SEPARATE_ARTIST,
        SPELLING_VARIANT,
        SPLIT_RELEASE,
        UNRESOLVED,
    ]:
        if p2_counts[cls]:
            lines.append(f"  {cls}: {p2_counts[cls]}")
    needs_own_code = p2_counts[MEMBER_OF_GROUP] + p2_counts[SEPARATE_ARTIST]
    lines.append(f"  -> Should have own library code: {needs_own_code}")
    lines.append("")

    # Phase 3 summary
    lines.append(f"Phase 3: Duplicate Library Codes ({len(phase_3_results)} groups)")
    lines.append("-" * 40)
    total_artists = sum(len(r["artist_names"]) for r in phase_3_results)
    lines.append(f"  {total_artists} artists across {len(phase_3_results)} entity groups")
    if phase_3_results:
        lines.append("  Top groups:")
        for r in sorted(phase_3_results, key=lambda x: len(x["artist_names"]), reverse=True)[:10]:
            lines.append(f"    {' / '.join(r['artist_names'])}")
    lines.append("")

    # Phase 4 summary
    lines.append(f"Phase 4: Missing Group-Member Links ({len(phase_4_results)} pairs)")
    lines.append("-" * 40)
    if phase_4_results:
        lines.append("  Examples:")
        for r in phase_4_results[:10]:
            lines.append(f"    {r['member_name']} -> {r['group_name']}")
    lines.append("")

    # Overall recommendations
    lines.append("Recommendations")
    lines.append("=" * 40)
    lines.append(f"1. {needs_own_code} alternate name entries should have their own library code")
    lines.append(
        f"   (MEMBER_OF_GROUP: {p2_counts[MEMBER_OF_GROUP]}, SEPARATE_ARTIST: {p2_counts[SEPARATE_ARTIST]})"
    )
    lines.append(
        f"2. {len(phase_3_results)} entity groups have duplicate library codes (consider merging)"
    )
    lines.append(f"3. {len(phase_4_results)} group-member relationships lack cross-references")
    xref_separate = p1_counts[SEPARATE_ARTIST] + p1_counts[MEMBER_OF_GROUP]
    lines.append(f"4. {xref_separate} existing cross-references link separate artists")
    lines.append("   (these artists may need their own library code)")

    text = "\n".join(lines) + "\n"
    path.write_text(text)
    # Also print to stdout
    print(text)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="WXYC artist name variation audit",
    )
    parser.add_argument(
        "--library-db",
        required=True,
        help="Path to library.db (SQLite export of WXYC catalog)",
    )
    parser.add_argument(
        "--graph-db",
        required=True,
        help="Path to wxyc_artist_graph.db (semantic index)",
    )
    parser.add_argument(
        "--sql-dump",
        required=True,
        help="Path to tubafrenzy MySQL dump (.sql)",
    )
    parser.add_argument(
        "--discogs-csv-dir",
        default=None,
        help="Directory with artist_alias.csv and artist_member.csv from discogs-xml-converter",
    )
    parser.add_argument(
        "--mb-alias-tsv",
        default=None,
        help="Path to MusicBrainz artist_alias TSV",
    )
    parser.add_argument(
        "--mb-artist-tsv",
        default=None,
        help="Path to MusicBrainz artist TSV (for int_id -> UUID mapping)",
    )
    parser.add_argument(
        "--output-dir",
        default="../docs/variation-audit",
        help="Output directory for CSV reports",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load data
    logger.info("Loading library data...")
    artist_to_codes, alt_pairs = load_library_artists(args.library_db)
    logger.info(
        "  %d unique artists, %d alternate name pairs", len(artist_to_codes), len(alt_pairs)
    )

    logger.info("Loading SQL dump...")
    library_codes = load_library_codes_from_dump(args.sql_dump)
    xrefs = load_cross_references_from_dump(args.sql_dump)

    logger.info("Loading graph database...")
    graph = load_graph_db(args.graph_db)

    # Discogs data (optional)
    discogs_aliases: dict[int, set[str]] = {}
    discogs_members: dict[int, set[tuple[int, str]]] = {}
    if args.discogs_csv_dir:
        relevant_discogs_ids = set(graph.name_to_discogs.values())
        logger.info("Loading Discogs CSVs (filtering to %d IDs)...", len(relevant_discogs_ids))
        discogs_aliases = load_discogs_aliases(args.discogs_csv_dir, relevant_discogs_ids)
        discogs_members = load_discogs_members(args.discogs_csv_dir, relevant_discogs_ids)
    else:
        logger.warning(
            "No --discogs-csv-dir provided; skipping Discogs alias/member classification"
        )

    # MusicBrainz data (optional)
    mb_aliases: dict[str, set[str]] = {}
    if args.mb_alias_tsv and args.mb_artist_tsv:
        relevant_mb_uuids = set(graph.name_to_mb.values())
        logger.info("Loading MusicBrainz TSVs (filtering to %d UUIDs)...", len(relevant_mb_uuids))
        mb_aliases = load_mb_aliases(args.mb_alias_tsv, args.mb_artist_tsv, relevant_mb_uuids)
    else:
        logger.warning(
            "No --mb-alias-tsv/--mb-artist-tsv provided; skipping MB alias classification"
        )

    # Build classifier
    classifier = RelationshipClassifier(
        graph=graph,
        discogs_aliases=discogs_aliases,
        discogs_members=discogs_members,
        mb_aliases=mb_aliases,
    )

    # Build play count and release count lookups for enrichment
    logger.info("Building play count and release count lookups...")
    play_counts = {}
    conn = __import__("sqlite3").connect(args.graph_db)
    for name, plays in conn.execute(
        "SELECT canonical_name, total_plays FROM artist WHERE total_plays > 0"
    ):
        play_counts[normalize_name(name)] = plays
    conn.close()

    own_release_counts = {}
    conn = __import__("sqlite3").connect(args.library_db)
    for name, count in conn.execute("SELECT artist, count(*) FROM library GROUP BY artist"):
        own_release_counts[name] = count
    conn.close()
    logger.info(
        "  %d artists with plays, %d with own releases", len(play_counts), len(own_release_counts)
    )

    # Run phases
    logger.info("Running Phase 1: Cross-references...")
    p1 = phase_1_cross_references(library_codes, xrefs, classifier, play_counts, own_release_counts)

    logger.info("Running Phase 3: Duplicate library codes...")
    p3 = phase_3_duplicates(graph, artist_to_codes)

    logger.info("Running Phase 2: Alternate artist names...")
    p2 = phase_2_alt_names(alt_pairs, classifier, play_counts, own_release_counts)

    # Build existing xref pairs for Phase 4
    existing_xref_pairs: set[tuple[str, str]] = set()
    for r in p1:
        norm_a = normalize_name(r["artist_a"])
        norm_b = normalize_name(r["artist_b"])
        existing_xref_pairs.add((norm_a, norm_b))
        existing_xref_pairs.add((norm_b, norm_a))

    logger.info("Running Phase 4: Missing group-member links...")
    p4 = phase_4_missing_links(graph, artist_to_codes, existing_xref_pairs)

    # Write output
    logger.info("Writing output...")
    write_csv_rows(
        p1,
        output_dir / "cross-refs.csv",
        [
            "xref_id",
            "artist_a",
            "artist_b",
            "comment",
            "discogs_id_a",
            "discogs_id_b",
            "entity_id_a",
            "entity_id_b",
            "plays_a",
            "plays_b",
            "own_releases_a",
            "own_releases_b",
            "classification",
            "evidence",
        ],
    )
    write_csv_rows(
        p2,
        output_dir / "alt-names.csv",
        [
            "library_artist",
            "alternate_name",
            "genre",
            "call_letters",
            "call_number",
            "release_count",
            "alt_flowsheet_plays",
            "alt_own_releases",
            "classification",
            "evidence",
        ],
    )

    # Phase 3 needs special handling for list fields
    p3_flat = []
    for r in p3:
        p3_flat.append(
            {
                "entity_id": r["entity_id"],
                "artist_names": " / ".join(r["artist_names"]),
                "library_codes": " / ".join(r["library_codes"]),
                "discogs_ids": " / ".join(r["discogs_ids"]),
                "recommendation": r["recommendation"],
            }
        )
    write_csv_rows(
        p3_flat,
        output_dir / "duplicates.csv",
        ["entity_id", "artist_names", "library_codes", "discogs_ids", "recommendation"],
    )

    write_csv_rows(
        p4,
        output_dir / "missing-links.csv",
        [
            "group_name",
            "member_name",
            "group_discogs_id",
            "member_discogs_id",
            "group_library_code",
            "member_library_code",
        ],
    )

    write_summary(p1, p2, p3, p4, output_dir / "summary.txt")
    logger.info("Done. Output written to %s", output_dir)


if __name__ == "__main__":
    main()
