"""Data loading functions for the variation audit."""

import csv
import logging
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


def normalize_name(name: str) -> str:
    """Normalize an artist name for comparison: NFKD, strip diacritics, lowercase, trim."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower().strip()


# -- Library DB --


def load_library_artists(
    library_db_path: str,
) -> tuple[dict[str, list[tuple[str, str, int]]], list[tuple[str, str, str, str, int, int]]]:
    """Load artist-to-library-code mappings and alternate name pairs from library.db.

    Returns:
        artist_to_codes: {artist_name: [(genre, call_letters, call_number), ...]}
        alt_name_pairs: [(artist, alternate_name, genre, call_letters, call_number, release_count)]
    """
    conn = sqlite3.connect(library_db_path)

    # Unique library codes per artist (excluding Various Artists)
    artist_to_codes: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    rows = conn.execute("""
        SELECT DISTINCT artist, genre, call_letters, artist_call_number
        FROM library
        WHERE artist NOT LIKE 'Various Artists%'
    """).fetchall()
    for artist, genre, cl, cn in rows:
        code = (genre, cl, cn)
        if code not in artist_to_codes[artist]:
            artist_to_codes[artist].append(code)

    # Alternate name pairs where alternate differs from main artist
    alt_name_pairs = []
    alt_rows = conn.execute("""
        SELECT artist, alternate_artist_name, genre, call_letters, artist_call_number, count(*) as cnt
        FROM library
        WHERE artist NOT LIKE 'Various Artists%'
          AND alternate_artist_name IS NOT NULL
          AND alternate_artist_name != ''
          AND alternate_artist_name != artist
        GROUP BY artist, alternate_artist_name, genre, call_letters, artist_call_number
    """).fetchall()
    for artist, alt, genre, cl, cn, count in alt_rows:
        alt_name_pairs.append((artist, alt, genre, cl, cn, count))

    conn.close()
    return dict(artist_to_codes), alt_name_pairs


# -- SQL Dump Parsing --


def _parse_sql_values(sql_line: str) -> list[tuple]:
    """Parse a MySQL INSERT INTO ... VALUES (...),(...); statement into tuples.

    Handles escaped single quotes (\\') and commas within quoted strings.
    """
    # Find the VALUES portion
    match = re.search(r"VALUES\s+", sql_line, re.IGNORECASE)
    if not match:
        return []

    values_str = sql_line[match.end() :]
    results = []
    i = 0
    n = len(values_str)

    while i < n:
        if values_str[i] != "(":
            i += 1
            continue

        # Parse one tuple
        i += 1  # skip (
        fields = []
        current = []
        in_quote = False

        while i < n:
            c = values_str[i]
            if in_quote:
                if c == "\\" and i + 1 < n and values_str[i + 1] == "'":
                    current.append("'")
                    i += 2
                    continue
                elif c == "'":
                    in_quote = False
                else:
                    current.append(c)
            else:
                if c == "'":
                    in_quote = True
                elif c == ",":
                    fields.append("".join(current))
                    current = []
                elif c == ")":
                    fields.append("".join(current))
                    results.append(tuple(fields))
                    i += 1
                    break
                else:
                    current.append(c)
            i += 1

    return results


def load_library_codes_from_dump(sql_dump_path: str) -> dict[int, str]:
    """Parse LIBRARY_CODE table from SQL dump. Returns {id: presentation_name}."""
    codes = {}
    with open(sql_dump_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "INSERT INTO `LIBRARY_CODE`" in line:
                for row in _parse_sql_values(line):
                    # Columns: ID, GENRE_ID, CALL_LETTERS, CALL_NUMBERS, ARTIST_ID,
                    #          TIME_LAST_MODIFIED, TIME_CREATED, PRESENTATION_NAME, ALPHABETICAL_NAME
                    if len(row) >= 8:
                        try:
                            code_id = int(row[0])
                            name = row[7]
                            codes[code_id] = name
                        except (ValueError, IndexError):
                            continue
    logger.info("Loaded %d library codes from SQL dump", len(codes))
    return codes


def load_cross_references_from_dump(
    sql_dump_path: str,
) -> list[tuple[int, int, int, str]]:
    """Parse LIBRARY_CODE_CROSS_REFERENCE from SQL dump.

    Returns list of (id, source_code_id, target_code_id, comment).
    """
    xrefs = []
    with open(sql_dump_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "INSERT INTO `LIBRARY_CODE_CROSS_REFERENCE`" in line:
                for row in _parse_sql_values(line):
                    # Columns: ID, CROSS_REFERENCING_ARTIST_ID, CROSS_REFERENCED_LIBRARY_CODE_ID,
                    #          COMMENT, TIME_LAST_MODIFIED, TIME_CREATED
                    if len(row) >= 4:
                        try:
                            xrefs.append((int(row[0]), int(row[1]), int(row[2]), row[3]))
                        except (ValueError, IndexError):
                            continue
    logger.info("Loaded %d cross-references from SQL dump", len(xrefs))
    return xrefs


# -- Semantic Index (Graph DB) --


@dataclass
class GraphData:
    """Data loaded from the semantic-index graph database."""

    name_to_discogs: dict[str, int] = field(default_factory=dict)
    name_to_mb: dict[str, str] = field(default_factory=dict)
    name_to_entity: dict[str, int] = field(default_factory=dict)
    entity_groups: dict[int, list[str]] = field(default_factory=dict)
    member_of: set[tuple[str, str]] = field(default_factory=set)  # (group_name, member_name)


def load_graph_db(graph_db_path: str) -> GraphData:
    """Load identity resolution data from wxyc_artist_graph.db."""
    conn = sqlite3.connect(graph_db_path)
    data = GraphData()

    # Name to Discogs/MB ID mappings (normalize keys for consistent lookup)
    rows = conn.execute("""
        SELECT canonical_name, discogs_artist_id, musicbrainz_artist_id, entity_id
        FROM artist
    """).fetchall()
    for name, discogs_id, mb_id, entity_id in rows:
        norm = normalize_name(name)
        if discogs_id is not None:
            data.name_to_discogs[norm] = discogs_id
        if mb_id is not None:
            data.name_to_mb[norm] = mb_id
        if entity_id is not None:
            data.name_to_entity[norm] = entity_id

    # Entity groups (entities with multiple artists)
    entity_artists: dict[int, list[str]] = defaultdict(list)
    for name, entity_id in data.name_to_entity.items():
        entity_artists[entity_id].append(name)
    data.entity_groups = {eid: names for eid, names in entity_artists.items() if len(names) > 1}

    # Member-of relationships (normalize names)
    member_rows = conn.execute("""
        SELECT g.canonical_name, m.canonical_name
        FROM member_of mo
        JOIN artist g ON mo.group_id = g.id
        JOIN artist m ON mo.member_id = m.id
    """).fetchall()
    data.member_of = {
        (normalize_name(group), normalize_name(member)) for group, member in member_rows
    }

    conn.close()
    logger.info(
        "Loaded graph DB: %d discogs IDs, %d MB IDs, %d entity groups, %d member_of pairs",
        len(data.name_to_discogs),
        len(data.name_to_mb),
        len(data.entity_groups),
        len(data.member_of),
    )
    return data


# -- Discogs CSV --


def load_discogs_aliases(csv_dir: str, relevant_ids: set[int]) -> dict[int, set[str]]:
    """Load artist_alias.csv from Discogs converter output, filtered to relevant IDs.

    Returns {discogs_artist_id: set(normalized_alias_names)}.
    """
    alias_path = Path(csv_dir) / "artist_alias.csv"
    if not alias_path.exists():
        logger.warning("Discogs artist_alias.csv not found at %s", alias_path)
        return {}

    result: dict[int, set[str]] = defaultdict(set)
    with open(alias_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                artist_id = int(row["artist_id"])
            except (ValueError, KeyError):
                continue
            if artist_id in relevant_ids:
                alias_name = normalize_name(row.get("alias_name", ""))
                if alias_name:
                    result[artist_id].add(alias_name)

    logger.info(
        "Loaded %d Discogs aliases for %d artists",
        sum(len(v) for v in result.values()),
        len(result),
    )
    return dict(result)


def load_discogs_members(csv_dir: str, relevant_ids: set[int]) -> dict[int, set[tuple[int, str]]]:
    """Load artist_member.csv from Discogs converter output, filtered to relevant IDs.

    Returns {group_discogs_id: set((member_discogs_id, normalized_member_name))}.
    """
    member_path = Path(csv_dir) / "artist_member.csv"
    if not member_path.exists():
        logger.warning("Discogs artist_member.csv not found at %s", member_path)
        return {}

    result: dict[int, set[tuple[int, str]]] = defaultdict(set)
    with open(member_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                group_id = int(row["group_artist_id"])
            except (ValueError, KeyError):
                continue
            if group_id in relevant_ids:
                try:
                    member_id = int(row["member_artist_id"])
                except (ValueError, KeyError):
                    member_id = 0
                member_name = normalize_name(row.get("member_name", ""))
                if member_name:
                    result[group_id].add((member_id, member_name))

    logger.info(
        "Loaded %d Discogs member entries for %d groups",
        sum(len(v) for v in result.values()),
        len(result),
    )
    return dict(result)


# -- MusicBrainz TSV --


def load_mb_aliases(
    alias_tsv_path: str,
    artist_tsv_path: str,
    relevant_uuids: set[str],
) -> dict[str, set[str]]:
    """Load MusicBrainz artist aliases, filtered to relevant UUIDs.

    The MB artist_alias TSV uses integer artist IDs, but the graph DB stores UUIDs.
    We first build a {int_id: uuid} map from the artist TSV, then filter aliases.

    Returns {mb_uuid: set(normalized_alias_names)}.
    """
    if not Path(artist_tsv_path).exists() or not Path(alias_tsv_path).exists():
        logger.warning("MusicBrainz TSV files not found")
        return {}

    # Step 1: Build int_id -> uuid map, filtered to relevant UUIDs
    int_to_uuid: dict[int, str] = {}
    with open(artist_tsv_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split("\t", 3)
            if len(parts) < 3:
                continue
            try:
                int_id = int(parts[0])
                uuid = parts[1]
                if uuid in relevant_uuids:
                    int_to_uuid[int_id] = uuid
            except ValueError:
                continue

    logger.info("Mapped %d MB integer IDs to UUIDs", len(int_to_uuid))
    relevant_int_ids = set(int_to_uuid.keys())

    # Step 2: Load aliases filtered by relevant integer IDs
    result: dict[str, set[str]] = defaultdict(set)
    with open(alias_tsv_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split("\t", 4)
            if len(parts) < 3:
                continue
            try:
                artist_int_id = int(parts[1])
            except ValueError:
                continue
            if artist_int_id in relevant_int_ids:
                alias_name = normalize_name(parts[2])
                if alias_name:
                    uuid = int_to_uuid[artist_int_id]
                    result[uuid].add(alias_name)

    logger.info(
        "Loaded %d MB aliases for %d artists", sum(len(v) for v in result.values()), len(result)
    )
    return dict(result)
