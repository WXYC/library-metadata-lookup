"""Unit tests for scripts/variation_audit/phases.py."""

from scripts.variation_audit.classifier import (
    ALIAS,
    MEMBER_OF_GROUP,
    SEPARATE_ARTIST,
    UNRESOLVED,
    RelationshipClassifier,
)
from scripts.variation_audit.loaders import GraphData
from scripts.variation_audit.phases import (
    phase_1_cross_references,
    phase_2_alt_names,
    phase_3_duplicates,
    phase_4_missing_links,
)


def _make_classifier(**kwargs):
    graph = GraphData(
        name_to_discogs=kwargs.get("name_to_discogs", {}),
        name_to_mb=kwargs.get("name_to_mb", {}),
        name_to_entity=kwargs.get("name_to_entity", {}),
        entity_groups=kwargs.get("entity_groups", {}),
        member_of=kwargs.get("member_of", set()),
    )
    return RelationshipClassifier(
        graph=graph,
        discogs_aliases=kwargs.get("discogs_aliases", {}),
        discogs_members=kwargs.get("discogs_members", {}),
        mb_aliases=kwargs.get("mb_aliases", {}),
    )


# -- Phase 1: Cross-references --


class TestPhase1CrossReferences:
    def test_classifies_cross_references(self):
        library_codes = {100: "Autechre", 200: "Gescom"}
        xrefs = [(1, 100, 200, "same artist")]
        classifier = _make_classifier(
            name_to_entity={"autechre": 10, "gescom": 10},
            entity_groups={10: ["autechre", "gescom"]},
        )
        results = phase_1_cross_references(library_codes, xrefs, classifier)
        assert len(results) == 1
        assert results[0]["artist_a"] == "Autechre"
        assert results[0]["artist_b"] == "Gescom"
        assert results[0]["classification"] == ALIAS

    def test_handles_missing_library_code(self):
        library_codes = {100: "Autechre"}
        xrefs = [(1, 100, 999, "unknown target")]
        classifier = _make_classifier()
        results = phase_1_cross_references(library_codes, xrefs, classifier)
        assert len(results) == 1
        assert results[0]["artist_b"] == "[unknown code 999]"
        assert results[0]["classification"] == UNRESOLVED

    def test_preserves_comment(self):
        library_codes = {100: "Why?", 200: "Clouddead"}
        xrefs = [(8, 100, 200, "Why? a member of Clouddead")]
        classifier = _make_classifier(
            member_of={("clouddead", "why?")},
        )
        results = phase_1_cross_references(library_codes, xrefs, classifier)
        assert results[0]["comment"] == "Why? a member of Clouddead"
        assert results[0]["classification"] == MEMBER_OF_GROUP


# -- Phase 2: Alternate names --


class TestPhase2AltNames:
    def test_classifies_alt_name_pairs(self):
        alt_pairs = [
            ("Sneaker Pimps", "IamX", "Hiphop", "SN", 5, 1),
        ]
        classifier = _make_classifier(
            name_to_discogs={"sneaker pimps": 100, "iamx": 200},
        )
        results = phase_2_alt_names(alt_pairs, classifier)
        assert len(results) == 1
        assert results[0]["library_artist"] == "Sneaker Pimps"
        assert results[0]["alternate_name"] == "IamX"
        assert results[0]["classification"] == SEPARATE_ARTIST

    def test_includes_library_code_info(self):
        alt_pairs = [
            ("Cat Power", "Chan Marshall", "Rock", "CA", 37, 2),
        ]
        classifier = _make_classifier(
            name_to_entity={"cat power": 5, "chan marshall": 5},
            entity_groups={5: ["cat power", "chan marshall"]},
        )
        results = phase_2_alt_names(alt_pairs, classifier)
        assert results[0]["genre"] == "Rock"
        assert results[0]["call_letters"] == "CA"
        assert results[0]["call_number"] == 37
        assert results[0]["release_count"] == 2
        assert results[0]["classification"] == ALIAS

    def test_filters_split_releases(self):
        alt_pairs = [
            ("Band A", "Band A <split> Band B", "Rock", "BA", 1, 1),
        ]
        classifier = _make_classifier()
        results = phase_2_alt_names(alt_pairs, classifier)
        # Split releases should still appear but classified as SPLIT_RELEASE
        assert results[0]["classification"] == "SPLIT_RELEASE"


# -- Phase 3: Duplicates --


class TestPhase3Duplicates:
    def test_finds_entity_duplicates(self):
        graph = GraphData(
            name_to_entity={"aphex twin": 10, "polygon window": 10, "caustic window": 10},
            entity_groups={10: ["aphex twin", "polygon window", "caustic window"]},
            name_to_discogs={"aphex twin": 45, "polygon window": 45, "caustic window": 45},
        )
        artist_to_codes = {
            "Aphex Twin": [("Electronic", "AP", 1)],
            "Polygon Window": [("Electronic", "PO", 50)],
            # Caustic Window not in library
        }
        results = phase_3_duplicates(graph, artist_to_codes)
        assert len(results) >= 1
        # At least one result should include both Aphex Twin and Polygon Window
        found = False
        for r in results:
            names = r["artist_names"]
            if "Aphex Twin" in names and "Polygon Window" in names:
                found = True
                assert "Caustic Window" not in names  # not in library
        assert found

    def test_skips_single_library_artist(self):
        graph = GraphData(
            name_to_entity={"aphex twin": 10, "polygon window": 10},
            entity_groups={10: ["aphex twin", "polygon window"]},
        )
        artist_to_codes = {
            "Aphex Twin": [("Electronic", "AP", 1)],
            # Polygon Window not in library
        }
        results = phase_3_duplicates(graph, artist_to_codes)
        assert len(results) == 0  # Need 2+ library artists to be a duplicate


# -- Phase 4: Missing links --


class TestPhase4MissingLinks:
    def test_finds_missing_group_member_links(self):
        graph = GraphData(
            member_of={("the jackson 5", "michael jackson")},
        )
        artist_to_codes = {
            "The Jackson 5": [("Rock", "JA", 1)],
            "Michael Jackson": [("Rock", "JA", 50)],
        }
        existing_xref_pairs = set()
        results = phase_4_missing_links(graph, artist_to_codes, existing_xref_pairs)
        assert len(results) == 1
        assert results[0]["group_name"] == "The Jackson 5"
        assert results[0]["member_name"] == "Michael Jackson"

    def test_skips_already_linked(self):
        graph = GraphData(
            member_of={("the mothers", "frank zappa")},
        )
        artist_to_codes = {
            "The Mothers": [("Rock", "MO", 1)],
            "Frank Zappa": [("Rock", "ZA", 2)],
        }
        existing_xref_pairs = {("frank zappa", "the mothers")}
        results = phase_4_missing_links(graph, artist_to_codes, existing_xref_pairs)
        assert len(results) == 0

    def test_skips_when_member_not_in_library(self):
        graph = GraphData(
            member_of={("the mothers", "ray collins")},
        )
        artist_to_codes = {
            "The Mothers": [("Rock", "MO", 1)],
            # Ray Collins not in library
        }
        existing_xref_pairs = set()
        results = phase_4_missing_links(graph, artist_to_codes, existing_xref_pairs)
        assert len(results) == 0
