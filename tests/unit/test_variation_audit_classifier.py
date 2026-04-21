"""Unit tests for scripts/variation_audit/classifier.py."""

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
from scripts.variation_audit.loaders import GraphData


def _make_classifier(
    name_to_discogs=None,
    name_to_mb=None,
    name_to_entity=None,
    entity_groups=None,
    member_of=None,
    discogs_aliases=None,
    discogs_members=None,
    mb_aliases=None,
):
    graph = GraphData(
        name_to_discogs=name_to_discogs or {},
        name_to_mb=name_to_mb or {},
        name_to_entity=name_to_entity or {},
        entity_groups=entity_groups or {},
        member_of=member_of or set(),
    )
    return RelationshipClassifier(
        graph=graph,
        discogs_aliases=discogs_aliases or {},
        discogs_members=discogs_members or {},
        mb_aliases=mb_aliases or {},
    )


class TestSpellingVariant:
    def test_case_difference(self):
        c = _make_classifier()
        cls, _ = c.classify("Autechre", "AUTECHRE")
        assert cls == SPELLING_VARIANT

    def test_diacritic_difference(self):
        c = _make_classifier()
        cls, _ = c.classify("Björk", "Bjork")
        assert cls == SPELLING_VARIANT

    def test_whitespace_difference(self):
        c = _make_classifier()
        cls, _ = c.classify("808 State", "808 State ")
        assert cls == SPELLING_VARIANT

    def test_different_names_not_variant(self):
        c = _make_classifier()
        cls, _ = c.classify("Autechre", "Aphex Twin")
        assert cls != SPELLING_VARIANT


class TestSplitRelease:
    def test_split_marker(self):
        c = _make_classifier()
        cls, _ = c.classify("Band A", "Band A <split> Band B")
        assert cls == SPLIT_RELEASE

    def test_no_split_marker(self):
        c = _make_classifier()
        cls, _ = c.classify("Band A", "Band B")
        assert cls != SPLIT_RELEASE


class TestCollaboration:
    def test_ampersand(self):
        c = _make_classifier()
        cls, _ = c.classify("DJ Krush", "DJ Krush & Coldcut")
        assert cls == COLLABORATION

    def test_feat(self):
        c = _make_classifier()
        cls, _ = c.classify("Artist A", "Artist A feat Artist B")
        assert cls == COLLABORATION

    def test_featuring(self):
        c = _make_classifier()
        cls, _ = c.classify("Artist A", "Artist A featuring Artist B")
        assert cls == COLLABORATION

    def test_with(self):
        c = _make_classifier()
        cls, _ = c.classify("Duke Ellington", "Duke Ellington with John Coltrane")
        assert cls == COLLABORATION

    def test_slash_separator(self):
        c = _make_classifier()
        cls, _ = c.classify("Band A", "Band A / Band B")
        assert cls == COLLABORATION


class TestAlias:
    def test_same_entity_id(self):
        c = _make_classifier(
            name_to_entity={"aphex twin": 10, "polygon window": 10},
            entity_groups={10: ["aphex twin", "polygon window"]},
        )
        cls, evidence = c.classify("Aphex Twin", "Polygon Window")
        assert cls == ALIAS
        assert "entity" in evidence.lower()

    def test_same_discogs_id(self):
        c = _make_classifier(
            name_to_discogs={"j dilla": 45, "jay dee": 45},
        )
        cls, evidence = c.classify("J Dilla", "Jay Dee")
        assert cls == ALIAS
        assert "discogs" in evidence.lower()

    def test_discogs_alias_table(self):
        c = _make_classifier(
            name_to_discogs={"autechre": 12},
            discogs_aliases={12: {"gescom", "lego feet"}},
        )
        cls, evidence = c.classify("Autechre", "Gescom")
        assert cls == ALIAS
        assert "alias" in evidence.lower()

    def test_mb_alias_table(self):
        c = _make_classifier(
            name_to_mb={"autechre": "mb-uuid-1"},
            mb_aliases={"mb-uuid-1": {"gescom", "ae"}},
        )
        cls, evidence = c.classify("Autechre", "Gescom")
        assert cls == ALIAS
        assert "musicbrainz" in evidence.lower()


class TestMemberOfGroup:
    def test_graph_member_of(self):
        c = _make_classifier(
            member_of={("the mothers", "frank zappa")},
        )
        cls, evidence = c.classify("Frank Zappa", "The Mothers")
        assert cls == MEMBER_OF_GROUP
        assert "member" in evidence.lower()

    def test_discogs_member(self):
        c = _make_classifier(
            name_to_discogs={"frank zappa": 56, "the mothers": 78},
            discogs_members={78: {(56, "frank zappa")}},
        )
        cls, evidence = c.classify("Frank Zappa", "The Mothers")
        assert cls == MEMBER_OF_GROUP
        assert "discogs" in evidence.lower()

    def test_reverse_direction(self):
        """Member-of should be detected regardless of argument order."""
        c = _make_classifier(
            member_of={("the mothers", "frank zappa")},
        )
        cls, _ = c.classify("The Mothers", "Frank Zappa")
        assert cls == MEMBER_OF_GROUP


class TestSeparateArtist:
    def test_both_have_discogs_no_relationship(self):
        c = _make_classifier(
            name_to_discogs={"sneaker pimps": 100, "iamx": 200},
        )
        cls, evidence = c.classify("Sneaker Pimps", "IamX")
        assert cls == SEPARATE_ARTIST
        assert "100" in evidence and "200" in evidence

    def test_both_have_mb_no_relationship(self):
        c = _make_classifier(
            name_to_mb={"sneaker pimps": "mb-1", "iamx": "mb-2"},
        )
        cls, evidence = c.classify("Sneaker Pimps", "IamX")
        assert cls == SEPARATE_ARTIST


class TestUnresolved:
    def test_no_external_ids(self):
        c = _make_classifier()
        cls, _ = c.classify("Unknown Artist A", "Unknown Artist B")
        assert cls == UNRESOLVED

    def test_one_has_id_other_does_not(self):
        c = _make_classifier(
            name_to_discogs={"known artist": 123},
        )
        cls, _ = c.classify("Known Artist", "Unknown Artist")
        assert cls == UNRESOLVED


class TestPrecedence:
    def test_spelling_variant_beats_alias(self):
        """If normalized names match, it's a spelling variant even if same entity."""
        c = _make_classifier(
            name_to_entity={"bjork": 10},
        )
        cls, _ = c.classify("Björk", "Bjork")
        assert cls == SPELLING_VARIANT

    def test_alias_beats_member_of(self):
        """If same entity, classify as alias even if also in member_of."""
        c = _make_classifier(
            name_to_entity={"aphex twin": 10, "afx": 10},
            entity_groups={10: ["aphex twin", "afx"]},
            member_of={("aphex twin", "afx")},
        )
        cls, _ = c.classify("Aphex Twin", "AFX")
        assert cls == ALIAS

    def test_split_beats_alias(self):
        c = _make_classifier(
            name_to_entity={"band a": 10, "band a <split> band b": 10},
        )
        cls, _ = c.classify("Band A", "Band A <split> Band B")
        assert cls == SPLIT_RELEASE
