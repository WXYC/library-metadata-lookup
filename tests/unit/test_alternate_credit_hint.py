"""Tests for ``lookup.alternate_credit_hint.alternate_credit_hint`` (LML#1444)."""

import pytest

from generated.api_models import ArtistMatchHint, ArtistSearchAliasSource
from lookup.alternate_credit_hint import alternate_credit_hint
from tests.factories import make_library_item

CREDIT = "Afel Bocoum, Damon Albarn, Toumani Diabate and friends"
HINT = [ArtistMatchHint(matched_variant=CREDIT, source=ArtistSearchAliasSource.wxyc_library_alt)]


@pytest.mark.parametrize(
    ("filed_under", "credit", "artist", "expected"),
    [
        ("Damon Albarn", CREDIT, "Afel Bocoum", HINT),
        (
            "Damon Albarn",
            "Afel Bocoum",
            "Afel Bocoum",
            [ArtistMatchHint(matched_variant="Afel Bocoum", source="wxyc_library_alt")],
        ),
        *[
            (
                "Damon Albarn",
                f"Afel Bocoum{sep}Damon Albarn",
                "Afel Bocoum",
                [
                    ArtistMatchHint(
                        matched_variant=f"Afel Bocoum{sep}Damon Albarn", source="wxyc_library_alt"
                    )
                ],
            )
            for sep in (", ", " & ", " and ", " / ")
        ],
        ("Damon Albarn", "Agnes Obel & Friends", "Agnes", None),
        ("Afel Bocoum", CREDIT, "Afel Bocoum", None),
        ("Afel Bocoum", "Afel Bocoum, Damon Albarn", "afel bocoum", None),
        ("The Clientele", "The Clientele, Damon Albarn", "Clientele", None),
        ("Melt-Banana", "Melt Banana & Friends", "Melt Banana", None),
        ("Damon Albarn", "", "Afel Bocoum", None),
        ("Damon Albarn", None, "Afel Bocoum", None),
    ],
)
def test_alternate_credit_hint(filed_under, credit, artist, expected):
    item = make_library_item(artist=filed_under, alternate_artist_name=credit)
    assert alternate_credit_hint(item, artist) == expected
