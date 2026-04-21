"""Unit tests for scripts/variation_audit/loaders.py."""

import sqlite3
from pathlib import Path

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

# -- normalize_name --


class TestNormalizeName:
    def test_lowercases(self):
        assert normalize_name("Autechre") == "autechre"

    def test_strips_whitespace(self):
        assert normalize_name("  Stereolab  ") == "stereolab"

    def test_strips_diacritics(self):
        assert normalize_name("Björk") == "bjork"

    def test_nfkd_decomposition(self):
        assert normalize_name("café") == "cafe"

    def test_preserves_numbers(self):
        assert normalize_name("808 State") == "808 state"


# -- load_library_artists --


def _create_test_library(tmp_path: Path) -> str:
    db_path = str(tmp_path / "library.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE library (
            id INTEGER PRIMARY KEY,
            title TEXT,
            artist TEXT,
            call_letters TEXT,
            artist_call_number INTEGER,
            release_call_number INTEGER,
            genre TEXT,
            format TEXT,
            alternate_artist_name TEXT,
            label TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                1,
                "Aluminum Tunes",
                "Stereolab",
                "ST",
                105,
                1,
                "Rock",
                "cd",
                "Stereolab",
                "Duophonic",
            ),
            (
                2,
                "Dots and Loops",
                "Stereolab",
                "ST",
                105,
                2,
                "Rock",
                "cd",
                "Stereolab",
                "Duophonic",
            ),
            (3, "Confield", "Autechre", "AU", 40, 1, "Electronic", "cd", "Autechre", "Warp"),
            (4, "Moon Pix", "Cat Power", "CA", 37, 1, "Rock", "cd", "Chan Marshall", "Matador"),
            (5, "Fillmore East", "Frank Zappa", "ZA", 2, 1, "Rock", "cd", "The Mothers", "Ryko"),
            (6, "Kiss & Swallow", "Sneaker Pimps", "SN", 5, 1, "Hiphop", "cd", "IamX", "Virgin"),
            (7, "V/A Comp", "Various Artists", "Z-A", 0, 1, "Rock", "cd", "Various Artists", None),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


class TestLoadLibraryArtists:
    def test_returns_artist_to_codes_mapping(self, tmp_path):
        db_path = _create_test_library(tmp_path)
        artists, _ = load_library_artists(db_path)
        assert "Stereolab" in artists
        assert artists["Stereolab"] == [("Rock", "ST", 105)]

    def test_deduplicates_library_codes(self, tmp_path):
        db_path = _create_test_library(tmp_path)
        artists, _ = load_library_artists(db_path)
        # Stereolab has 2 releases but 1 library code
        assert len(artists["Stereolab"]) == 1

    def test_returns_alt_name_pairs(self, tmp_path):
        db_path = _create_test_library(tmp_path)
        _, alt_pairs = load_library_artists(db_path)
        # Only pairs where alternate differs from artist
        alt_dict = {(a, alt): count for a, alt, g, cl, cn, count in alt_pairs}
        assert ("Cat Power", "Chan Marshall") in alt_dict
        assert ("Frank Zappa", "The Mothers") in alt_dict
        assert ("Sneaker Pimps", "IamX") in alt_dict
        # Stereolab's alt matches artist, should be excluded
        assert ("Stereolab", "Stereolab") not in alt_dict

    def test_excludes_various_artists(self, tmp_path):
        db_path = _create_test_library(tmp_path)
        artists, _ = load_library_artists(db_path)
        assert "Various Artists" not in artists

    def test_alt_pairs_include_release_count(self, tmp_path):
        db_path = _create_test_library(tmp_path)
        _, alt_pairs = load_library_artists(db_path)
        for artist, alt, genre, cl, cn, count in alt_pairs:
            if artist == "Cat Power" and alt == "Chan Marshall":
                assert count == 1
                assert genre == "Rock"
                assert cl == "CA"
                assert cn == 37


# -- load_library_codes_from_dump --


SAMPLE_LIBRARY_CODE_SQL = """
INSERT INTO `LIBRARY_CODE` VALUES (100,1,'AU',40,100,1000,1000,'Autechre','Autechre'),(200,1,'ST',105,200,1000,1000,'Stereolab','Stereolab'),(300,2,'ZA',2,300,1000,1000,'Frank Zappa','Zappa, Frank');
"""


class TestLoadLibraryCodesFromDump:
    def test_parses_library_codes(self, tmp_path):
        dump_path = tmp_path / "dump.sql"
        dump_path.write_text(SAMPLE_LIBRARY_CODE_SQL)
        codes = load_library_codes_from_dump(str(dump_path))
        assert codes[100] == "Autechre"
        assert codes[200] == "Stereolab"
        assert codes[300] == "Frank Zappa"

    def test_handles_escaped_quotes(self, tmp_path):
        sql = "INSERT INTO `LIBRARY_CODE` VALUES (400,1,'ER',7,400,1000,1000,'Eric\\'s Trip','Eric\\'s Trip');\n"
        dump_path = tmp_path / "dump.sql"
        dump_path.write_text(sql)
        codes = load_library_codes_from_dump(str(dump_path))
        assert codes[400] == "Eric's Trip"


# -- load_cross_references_from_dump --


SAMPLE_XREF_SQL = """
INSERT INTO `LIBRARY_CODE_CROSS_REFERENCE` VALUES (2,100,200,'shared member Adrian Finch',1000,1000),(4,300,200,'Grace\\'s solo work is filed w/ DQE',1000,1000);
"""


class TestLoadCrossReferencesFromDump:
    def test_parses_cross_references(self, tmp_path):
        dump_path = tmp_path / "dump.sql"
        dump_path.write_text(SAMPLE_XREF_SQL)
        xrefs = load_cross_references_from_dump(str(dump_path))
        assert len(xrefs) == 2
        assert xrefs[0] == (2, 100, 200, "shared member Adrian Finch")
        assert xrefs[1] == (4, 300, 200, "Grace's solo work is filed w/ DQE")

    def test_handles_empty_comments(self, tmp_path):
        sql = "INSERT INTO `LIBRARY_CODE_CROSS_REFERENCE` VALUES (56,100,200,'',1000,1000);\n"
        dump_path = tmp_path / "dump.sql"
        dump_path.write_text(sql)
        xrefs = load_cross_references_from_dump(str(dump_path))
        assert xrefs[0] == (56, 100, 200, "")


# -- load_graph_db --


def _create_test_graph_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE artist (
            id INTEGER PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            discogs_artist_id INTEGER,
            musicbrainz_artist_id TEXT,
            entity_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE entity (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE member_of (
            group_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            PRIMARY KEY (group_id, member_id)
        )
    """)
    conn.executemany(
        "INSERT INTO artist VALUES (?, ?, ?, ?, ?)",
        [
            (1, "autechre", 12, "mb-uuid-1", None),
            (2, "stereolab", 34, "mb-uuid-2", None),
            (3, "aphex twin", 45, None, 10),
            (4, "polygon window", 45, None, 10),
            (5, "frank zappa", 56, None, None),
            (6, "the mothers", 78, None, None),
        ],
    )
    conn.execute("INSERT INTO entity VALUES (10, 'Richard D. James')")
    conn.executemany(
        "INSERT INTO member_of VALUES (?, ?)",
        [
            (6, 5),  # Frank Zappa is member of The Mothers
        ],
    )
    conn.commit()
    conn.close()
    return db_path


class TestLoadGraphDb:
    def test_returns_name_to_discogs(self, tmp_path):
        db_path = _create_test_graph_db(tmp_path)
        result = load_graph_db(db_path)
        assert result.name_to_discogs["autechre"] == 12
        assert result.name_to_discogs["stereolab"] == 34

    def test_returns_name_to_mb(self, tmp_path):
        db_path = _create_test_graph_db(tmp_path)
        result = load_graph_db(db_path)
        assert result.name_to_mb["autechre"] == "mb-uuid-1"

    def test_returns_entity_groups(self, tmp_path):
        db_path = _create_test_graph_db(tmp_path)
        result = load_graph_db(db_path)
        # Entity 10 has aphex twin and polygon window
        assert any(
            {"aphex twin", "polygon window"} == set(group)
            for group in result.entity_groups.values()
        )

    def test_returns_member_of_pairs(self, tmp_path):
        db_path = _create_test_graph_db(tmp_path)
        result = load_graph_db(db_path)
        assert ("the mothers", "frank zappa") in result.member_of

    def test_returns_name_to_entity(self, tmp_path):
        db_path = _create_test_graph_db(tmp_path)
        result = load_graph_db(db_path)
        assert result.name_to_entity["aphex twin"] == 10
        assert result.name_to_entity["polygon window"] == 10

    def test_shared_discogs_id_detected(self, tmp_path):
        db_path = _create_test_graph_db(tmp_path)
        result = load_graph_db(db_path)
        # aphex twin and polygon window both have discogs_artist_id=45
        assert result.name_to_discogs["aphex twin"] == 45
        assert result.name_to_discogs["polygon window"] == 45


# -- load_discogs_aliases --


class TestLoadDiscogsAliases:
    def test_loads_and_filters_by_id(self, tmp_path):
        csv_path = tmp_path / "artist_alias.csv"
        csv_path.write_text(
            "artist_id,artist_name,alias_name\n"
            "12,Autechre,Gescom\n"
            "12,Autechre,Lego Feet\n"
            "999,Nobody,Irrelevant\n"
        )
        result = load_discogs_aliases(str(tmp_path), relevant_ids={12})
        assert "gescom" in result[12]
        assert "lego feet" in result[12]
        assert 999 not in result

    def test_empty_when_no_csv(self, tmp_path):
        result = load_discogs_aliases(str(tmp_path / "nonexistent"), relevant_ids={12})
        assert result == {}


# -- load_discogs_members --


class TestLoadDiscogsMembers:
    def test_loads_and_filters_by_id(self, tmp_path):
        csv_path = tmp_path / "artist_member.csv"
        csv_path.write_text(
            "group_artist_id,group_name,member_artist_id,member_name\n"
            "78,The Mothers,56,Frank Zappa\n"
            "78,The Mothers,90,Ray Collins\n"
            "999,Nobody,888,Irrelevant\n"
        )
        result = load_discogs_members(str(tmp_path), relevant_ids={78, 56})
        assert (56, "frank zappa") in result[78]
        assert (90, "ray collins") in result[78]
        assert 999 not in result

    def test_empty_when_no_csv(self, tmp_path):
        result = load_discogs_members(str(tmp_path / "nonexistent"), relevant_ids={78})
        assert result == {}


# -- load_mb_aliases --


class TestLoadMbAliases:
    def test_loads_and_filters_by_uuid(self, tmp_path):
        # MB artist TSV: columns 0=id, 1=gid, 2=name (tab-separated, no header)
        artist_tsv = tmp_path / "artist"
        artist_tsv.write_text(
            "100\tmb-uuid-1\tAutechre\tAutechre\t2\t\t\t\t\t\t\t\t\t\n"
            "200\tmb-uuid-2\tStereolab\tStereolab\t2\t\t\t\t\t\t\t\t\t\n"
            "300\tmb-uuid-999\tNobody\tNobody\t2\t\t\t\t\t\t\t\t\t\n"
        )
        # MB artist_alias TSV: columns 0=id, 1=artist_id(int), 2=name
        alias_tsv = tmp_path / "artist_alias"
        alias_tsv.write_text(
            "1\t100\tGescom\t\t\t\t\t\t\t\t\t\t\t\t\t\n"
            "2\t100\tLego Feet\t\t\t\t\t\t\t\t\t\t\t\t\t\n"
            "3\t300\tIrrelevant\t\t\t\t\t\t\t\t\t\t\t\t\t\n"
        )
        result = load_mb_aliases(
            str(alias_tsv),
            str(artist_tsv),
            relevant_uuids={"mb-uuid-1", "mb-uuid-2"},
        )
        assert "gescom" in result["mb-uuid-1"]
        assert "lego feet" in result["mb-uuid-1"]
        assert "mb-uuid-999" not in result

    def test_empty_when_no_file(self, tmp_path):
        result = load_mb_aliases(
            str(tmp_path / "nonexistent"),
            str(tmp_path / "nonexistent2"),
            relevant_uuids={"mb-uuid-1"},
        )
        assert result == {}
