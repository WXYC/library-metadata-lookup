"""Relationship classification for artist name variation audit."""

import re

from scripts.variation_audit.loaders import GraphData, normalize_name

# Classification categories
ALIAS = "ALIAS"
MEMBER_OF_GROUP = "MEMBER_OF_GROUP"
COLLABORATION = "COLLABORATION"
SEPARATE_ARTIST = "SEPARATE_ARTIST"
SPELLING_VARIANT = "SPELLING_VARIANT"
SPLIT_RELEASE = "SPLIT_RELEASE"
UNRESOLVED = "UNRESOLVED"

# Patterns that indicate collaboration credits
_COLLAB_PATTERNS = re.compile(
    r"\s+(?:&|feat\.?|featuring|with|meets|vs\.?)\s+|"
    r"\s*/\s*",  # slash separator
    re.IGNORECASE,
)


class RelationshipClassifier:
    """Classifies the relationship between two artist names using external data."""

    def __init__(
        self,
        graph: GraphData,
        discogs_aliases: dict[int, set[str]],
        discogs_members: dict[int, set[tuple[int, str]]],
        mb_aliases: dict[str, set[str]],
    ):
        self._graph = graph
        self._discogs_aliases = discogs_aliases
        self._discogs_members = discogs_members
        self._mb_aliases = mb_aliases

        # Build reverse Discogs member index: {member_discogs_id: set(group_discogs_id)}
        self._member_to_groups: dict[int, set[int]] = {}
        for group_id, members in discogs_members.items():
            for member_id, _ in members:
                self._member_to_groups.setdefault(member_id, set()).add(group_id)

    def classify(self, name_a: str, name_b: str) -> tuple[str, str]:
        """Classify the relationship between two artist names.

        Returns (classification, evidence_string).
        """
        norm_a = normalize_name(name_a)
        norm_b = normalize_name(name_b)

        # 1. Spelling variant (normalized names match)
        if norm_a == norm_b:
            return SPELLING_VARIANT, f"Normalized forms match: '{norm_a}'"

        # 2. Split release
        if "<split>" in name_b.lower() or "<split>" in name_a.lower():
            return SPLIT_RELEASE, "Contains <split> marker"

        # 3. Collaboration (one name contains the other plus a collab separator)
        collab = self._check_collaboration(name_a, name_b, norm_a, norm_b)
        if collab:
            return COLLABORATION, collab

        # 4. Alias: same entity_id
        entity_a = self._graph.name_to_entity.get(norm_a)
        entity_b = self._graph.name_to_entity.get(norm_b)
        if entity_a is not None and entity_b is not None and entity_a == entity_b:
            return ALIAS, f"Same entity ID {entity_a} in semantic index"

        # 5. Alias: same Discogs ID
        discogs_a = self._graph.name_to_discogs.get(norm_a)
        discogs_b = self._graph.name_to_discogs.get(norm_b)
        if discogs_a is not None and discogs_b is not None and discogs_a == discogs_b:
            return ALIAS, f"Same Discogs artist ID {discogs_a}"

        # 6. Alias: Discogs alias table
        if discogs_a is not None and norm_b in self._discogs_aliases.get(discogs_a, set()):
            return ALIAS, f"'{name_b}' is Discogs alias of '{name_a}' (ID {discogs_a})"
        if discogs_b is not None and norm_a in self._discogs_aliases.get(discogs_b, set()):
            return ALIAS, f"'{name_a}' is Discogs alias of '{name_b}' (ID {discogs_b})"

        # 7. Alias: MusicBrainz alias table
        mb_a = self._graph.name_to_mb.get(norm_a)
        mb_b = self._graph.name_to_mb.get(norm_b)
        if mb_a is not None and norm_b in self._mb_aliases.get(mb_a, set()):
            return ALIAS, f"'{name_b}' is MusicBrainz alias of '{name_a}'"
        if mb_b is not None and norm_a in self._mb_aliases.get(mb_b, set()):
            return ALIAS, f"'{name_a}' is MusicBrainz alias of '{name_b}'"

        # 8. Member of group: semantic-index member_of
        if (norm_b, norm_a) in self._graph.member_of:
            return MEMBER_OF_GROUP, f"'{name_a}' is member of '{name_b}' per semantic index"
        if (norm_a, norm_b) in self._graph.member_of:
            return MEMBER_OF_GROUP, f"'{name_b}' is member of '{name_a}' per semantic index"

        # 9. Member of group: Discogs artist_member
        if discogs_a is not None and discogs_b is not None:
            # Check if A is member of B
            if discogs_b in self._discogs_members:
                for member_id, _ in self._discogs_members[discogs_b]:
                    if member_id == discogs_a:
                        return (
                            MEMBER_OF_GROUP,
                            f"'{name_a}' is Discogs member of '{name_b}' (group ID {discogs_b})",
                        )
            # Check if B is member of A
            if discogs_a in self._discogs_members:
                for member_id, _ in self._discogs_members[discogs_a]:
                    if member_id == discogs_b:
                        return (
                            MEMBER_OF_GROUP,
                            f"'{name_b}' is Discogs member of '{name_a}' (group ID {discogs_a})",
                        )

        # 10. Separate artist: both have external IDs but no relationship
        has_id_a = discogs_a is not None or mb_a is not None
        has_id_b = discogs_b is not None or mb_b is not None
        if has_id_a and has_id_b:
            ids_a = f"Discogs={discogs_a}" if discogs_a else f"MB={mb_a}"
            ids_b = f"Discogs={discogs_b}" if discogs_b else f"MB={mb_b}"
            return SEPARATE_ARTIST, f"No relationship found: {name_a}({ids_a}), {name_b}({ids_b})"

        # 11. Unresolved
        return UNRESOLVED, "Insufficient external data to classify"

    def _check_collaboration(
        self, name_a: str, name_b: str, norm_a: str, norm_b: str
    ) -> str | None:
        """Check if one name is a collaboration credit involving the other."""
        # Check if name_b contains name_a plus a collab separator
        parts = _COLLAB_PATTERNS.split(name_b)
        if len(parts) > 1:
            normalized_parts = {normalize_name(p) for p in parts}
            if norm_a in normalized_parts:
                return f"'{name_b}' is collaboration involving '{name_a}'"

        # Check reverse
        parts = _COLLAB_PATTERNS.split(name_a)
        if len(parts) > 1:
            normalized_parts = {normalize_name(p) for p in parts}
            if norm_b in normalized_parts:
                return f"'{name_a}' is collaboration involving '{name_b}'"

        return None
