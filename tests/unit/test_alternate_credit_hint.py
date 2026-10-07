"""Tests for ``lookup.alternate_credit_hint.alternate_credit_hint`` (LML#1444)."""

from unittest.mock import patch

import pytest

from discogs.models import DiscogsSearchResponse
from generated.api_models import ArtistMatchHint, ArtistSearchAliasSource, DegradedReason
from lookup.alternate_credit_hint import alternate_credit_hint
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from tests.conftest import make_lml_telemetry
from tests.factories import make_library_catalog, make_library_item

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
        # Credited, but filed under the typed artist on one equality rung only:
        # each row fails when its rung is dropped from the "filed under" test.
        ("The Clientele", "Clientele & Friends", "Clientele", None),  # article (2 and 4)
        ("!!!", "The !!! & Friends", "The !!!", None),  # article only (2; 4 is empty)
        ("A-Bones", "A Bones & Friends", "A Bones", None),  # punctuation only (3)
        ("Melt-Banana", "The Melt Banana & Friends", "The Melt Banana", None),  # both (4)
        ("Melt-Banana", "Melt Banana & Friends", "Melt Banana", None),
        # A rung key that comes out empty matches nothing, as in artist_shelf.
        (
            "...",
            "!!! & Friends",
            "!!!",
            [ArtistMatchHint(matched_variant="!!! & Friends", source="wxyc_library_alt")],
        ),
        (
            "The",
            "A & Friends",
            "A",
            [ArtistMatchHint(matched_variant="A & Friends", source="wxyc_library_alt")],
        ),
        ("Damon Albarn", "", "Afel Bocoum", None),
        ("Damon Albarn", None, "Afel Bocoum", None),
    ],
)
def test_alternate_credit_hint(filed_under, credit, artist, expected):
    item = make_library_item(artist=filed_under, alternate_artist_name=credit)
    assert alternate_credit_hint(item, artist) == expected


class TestPerformLookupLabelsTheCreditedRow:
    """The response-level case through ``perform_lookup`` over a real catalog:
    the Mali Music row filed under Damon Albarn carries the label and Afel
    Bocoum's own row does not, on the full path and the degraded one. The
    misspelled "Afel Bokoum" is fuzzy-corrected on the library channel, so it
    pins that the label reads ``library_artist_for``, not the typed artist."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("typed", ["Afel Bocoum", "Afel Bokoum"])
    @pytest.mark.parametrize("shed", [None, DegradedReason.deadline_exceeded])
    async def test_only_the_credited_row_is_labelled(
        self, tmp_path, mock_discogs_service, typed, shed
    ):
        db = await make_library_catalog(
            tmp_path, [("Afel Bocoum", "Mali Music"), ("Damon Albarn", "Mali Music", CREDIT)]
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        request = LookupRequest(artist=typed, album="Mali Music", raw_message=f"{typed} - Mali")
        try:
            with patch("lookup.orchestrator.should_shed_tail", return_value=shed):
                response = await perform_lookup(
                    request, db, mock_discogs_service, make_lml_telemetry()
                )
        finally:
            await db.close()

        assert response.degraded is (shed is not None)
        assert [(i.library_item.artist, i.matched_via_alias) for i in response.results] == [
            ("Afel Bocoum", None),
            ("Damon Albarn", HINT),
        ]
