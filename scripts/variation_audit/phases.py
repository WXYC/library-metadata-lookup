"""Analysis phases for the variation audit."""

import logging
import sqlite3

from scripts.variation_audit.classifier import UNRESOLVED, RelationshipClassifier
from scripts.variation_audit.loaders import GraphData, normalize_name

logger = logging.getLogger(__name__)


def _build_play_counts(graph_db_path: str | None) -> dict[str, int]:
    """Build {normalized_name: total_plays} from the graph DB."""
    if not graph_db_path:
        return {}
    conn = sqlite3.connect(graph_db_path)
    rows = conn.execute(
        "SELECT canonical_name, total_plays FROM artist WHERE total_plays > 0"
    ).fetchall()
    conn.close()
    return {normalize_name(name): plays for name, plays in rows}


def _build_own_release_counts(library_db_path: str | None) -> dict[str, int]:
    """Build {artist_name: release_count} from library.db."""
    if not library_db_path:
        return {}
    conn = sqlite3.connect(library_db_path)
    rows = conn.execute("SELECT artist, count(*) FROM library GROUP BY artist").fetchall()
    conn.close()
    return dict(rows)


def phase_1_cross_references(
    library_codes: dict[int, str],
    xrefs: list[tuple[int, int, int, str]],
    classifier: RelationshipClassifier,
    play_counts: dict[str, int] | None = None,
    own_release_counts: dict[str, int] | None = None,
) -> list[dict]:
    """Classify all LIBRARY_CODE_CROSS_REFERENCE entries.

    Args:
        library_codes: {code_id: presentation_name}
        xrefs: [(id, source_code_id, target_code_id, comment)]
        classifier: RelationshipClassifier instance
        play_counts: {normalized_name: total_plays} from graph DB
        own_release_counts: {artist_name: release_count} from library.db
    """
    play_counts = play_counts or {}
    own_release_counts = own_release_counts or {}
    results = []
    for xref_id, source_id, target_id, comment in xrefs:
        name_a = library_codes.get(source_id, f"[unknown code {source_id}]")
        name_b = library_codes.get(target_id, f"[unknown code {target_id}]")

        if name_a.startswith("[unknown") or name_b.startswith("[unknown"):
            classification, evidence = UNRESOLVED, f"Missing library code: {name_a} or {name_b}"
        else:
            classification, evidence = classifier.classify(name_a, name_b)

        norm_a = normalize_name(name_a)
        norm_b = normalize_name(name_b)
        graph = classifier._graph

        results.append(
            {
                "xref_id": xref_id,
                "artist_a": name_a,
                "artist_b": name_b,
                "comment": comment,
                "discogs_id_a": graph.name_to_discogs.get(norm_a),
                "discogs_id_b": graph.name_to_discogs.get(norm_b),
                "entity_id_a": graph.name_to_entity.get(norm_a),
                "entity_id_b": graph.name_to_entity.get(norm_b),
                "plays_a": play_counts.get(norm_a, 0),
                "plays_b": play_counts.get(norm_b, 0),
                "own_releases_a": own_release_counts.get(name_a, 0),
                "own_releases_b": own_release_counts.get(name_b, 0),
                "classification": classification,
                "evidence": evidence,
            }
        )

    logger.info("Phase 1: Classified %d cross-references", len(results))
    return results


def phase_2_alt_names(
    alt_pairs: list[tuple[str, str, str, str, int, int]],
    classifier: RelationshipClassifier,
    play_counts: dict[str, int] | None = None,
    own_release_counts: dict[str, int] | None = None,
) -> list[dict]:
    """Classify alternate_artist_name variations.

    Args:
        alt_pairs: [(artist, alternate_name, genre, call_letters, call_number, release_count)]
        classifier: RelationshipClassifier instance
        play_counts: {normalized_name: total_plays} from graph DB
        own_release_counts: {artist_name: release_count} from library.db
    """
    play_counts = play_counts or {}
    own_release_counts = own_release_counts or {}
    results = []
    for artist, alt_name, genre, cl, cn, count in alt_pairs:
        classification, evidence = classifier.classify(artist, alt_name)

        norm_alt = normalize_name(alt_name)
        results.append(
            {
                "library_artist": artist,
                "alternate_name": alt_name,
                "genre": genre,
                "call_letters": cl,
                "call_number": cn,
                "release_count": count,
                "alt_flowsheet_plays": play_counts.get(norm_alt, 0),
                "alt_own_releases": own_release_counts.get(alt_name, 0),
                "classification": classification,
                "evidence": evidence,
            }
        )

    logger.info("Phase 2: Classified %d alternate name pairs", len(results))
    return results


def phase_3_duplicates(
    graph: GraphData,
    artist_to_codes: dict[str, list[tuple[str, str, int]]],
) -> list[dict]:
    """Find library codes that map to the same real-world person.

    Uses entity groups from the semantic index to find cases where
    multiple WXYC library artists resolve to the same entity.
    """
    # Build normalized name -> original name lookup for library artists
    norm_to_orig: dict[str, str] = {}
    for name in artist_to_codes:
        norm_to_orig[normalize_name(name)] = name

    results = []
    for entity_id, entity_names in graph.entity_groups.items():
        # Find which entity members are in the library catalog
        library_matches = []
        for norm_name in entity_names:
            if norm_name in norm_to_orig:
                orig_name = norm_to_orig[norm_name]
                codes = artist_to_codes[orig_name]
                library_matches.append((orig_name, codes))

        if len(library_matches) < 2:
            continue

        # Collect Discogs IDs for these artists
        discogs_ids = []
        for orig_name, _ in library_matches:
            norm = normalize_name(orig_name)
            did = graph.name_to_discogs.get(norm)
            discogs_ids.append(str(did) if did else "None")

        artist_names = [m[0] for m in library_matches]
        code_strs = [
            f"{codes[0][0]} {codes[0][1]} {codes[0][2]}" if codes else "?"
            for _, codes in library_matches
        ]

        results.append(
            {
                "entity_id": entity_id,
                "artist_names": artist_names,
                "library_codes": code_strs,
                "discogs_ids": discogs_ids,
                "recommendation": "Consider merging or cross-referencing",
            }
        )

    logger.info("Phase 3: Found %d duplicate library code groups", len(results))
    return results


def phase_4_missing_links(
    graph: GraphData,
    artist_to_codes: dict[str, list[tuple[str, str, int]]],
    existing_xref_pairs: set[tuple[str, str]],
) -> list[dict]:
    """Find group-member relationships not in cross-references.

    Args:
        graph: GraphData with member_of relationships
        artist_to_codes: library artist code mappings
        existing_xref_pairs: set of (norm_a, norm_b) for already-linked pairs
    """
    # Build normalized name -> original name lookup for library artists
    norm_to_orig: dict[str, str] = {}
    for name in artist_to_codes:
        norm_to_orig[normalize_name(name)] = name

    results = []
    for group_norm, member_norm in graph.member_of:
        # Both must be in the library catalog
        if group_norm not in norm_to_orig or member_norm not in norm_to_orig:
            continue

        # Skip if already linked (check both directions)
        if (group_norm, member_norm) in existing_xref_pairs:
            continue
        if (member_norm, group_norm) in existing_xref_pairs:
            continue

        group_orig = norm_to_orig[group_norm]
        member_orig = norm_to_orig[member_norm]
        group_codes = artist_to_codes[group_orig]
        member_codes = artist_to_codes[member_orig]

        group_discogs = graph.name_to_discogs.get(group_norm)
        member_discogs = graph.name_to_discogs.get(member_norm)

        results.append(
            {
                "group_name": group_orig,
                "member_name": member_orig,
                "group_discogs_id": group_discogs,
                "member_discogs_id": member_discogs,
                "group_library_code": f"{group_codes[0][0]} {group_codes[0][1]} {group_codes[0][2]}"
                if group_codes
                else "?",
                "member_library_code": f"{member_codes[0][0]} {member_codes[0][1]} {member_codes[0][2]}"
                if member_codes
                else "?",
            }
        )

    logger.info("Phase 4: Found %d missing group-member links", len(results))
    return results
