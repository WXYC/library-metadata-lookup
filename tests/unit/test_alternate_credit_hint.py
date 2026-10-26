"""Tests for ``lookup.alternate_credit_hint.alternate_credit_hint`` (LML#1444)."""

from unittest.mock import patch

import pytest

from discogs.models import DiscogsSearchResponse
from generated.api_models import ArtistMatchHint, ArtistSearchAliasSource, DegradedReason
from lookup.alternate_credit_hint import alternate_credit_hint
from lookup.artist_shelf import artist_spellings
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from lookup.strategies.artist_plus_album import search_library_with_fallback
from services.parser import ParsedRequest
from tests.conftest import make_lml_telemetry
from tests.factories import library_catalog, make_library_item

CREDIT = "Afel Bocoum, Damon Albarn, Toumani Diabate and friends"
HINT = [ArtistMatchHint(matched_variant=CREDIT, source=ArtistSearchAliasSource.wxyc_library_alt)]


@pytest.mark.parametrize(
    ("row_artist", "credit", "artist", "expected"),
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
def test_alternate_credit_hint(row_artist, credit, artist, expected):
    item = make_library_item(artist=row_artist, alternate_artist_name=credit)
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
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        request = LookupRequest(artist=typed, album="Mali Music", raw_message=f"{typed} - Mali")
        async with library_catalog(
            tmp_path, [("Afel Bocoum", "Mali Music"), ("Damon Albarn", "Mali Music", CREDIT)]
        ) as db:
            with patch("lookup.orchestrator.should_shed_tail", return_value=shed):
                response = await perform_lookup(
                    request, db, mock_discogs_service, make_lml_telemetry()
                )

        assert response.degraded is (shed is not None)
        assert [(i.library_item.artist, i.matched_via_alias) for i in response.results] == [
            ("Afel Bocoum", None),
            ("Damon Albarn", HINT),
        ]


@pytest.mark.parametrize(
    ("row_artist", "credit", "artist", "spellings", "labelled"),
    [
        # A shelved artist: "filed under" is membership in artist_spellings, so a
        # tolerant-rung neighbour that is another act is credited, not filed under.
        ("The Girls", "Girls & Friends", "Girls", ["Girls"], True),
        ("Mark-Almond", "Mark Almond & Friends", "Mark Almond", ["Mark Almond"], True),
        # The same-call-letters punctuation variant is in spellings: no label.
        ("Alaska!", "Alaska & Friends", "Alaska", ["Alaska", "Alaska!"], False),
        ("Alaska", "Alaska & Friends", "Alaska", ["Alaska", "Alaska!"], False),
        ("Damon Albarn", CREDIT, "Afel Bocoum", ["Afel Bocoum"], True),
        # An unshelved artist (no spellings) keeps the rung test (LML#1490).
        ("The Girls", "Girls & Friends", "Girls", [], False),
        ("Damon Albarn", CREDIT, "Afel Bocoum", [], True),
    ],
)
def test_alternate_credit_hint_by_spellings(row_artist, credit, artist, spellings, labelled):
    item = make_library_item(artist=row_artist, alternate_artist_name=credit)
    hint = alternate_credit_hint(item, artist, spellings)
    assert (hint is not None) is labelled


class TestPerformLookupLabelsTheCreditedRowAcrossActs:
    """Through ``perform_lookup`` over a real catalog: the credited row is
    labelled when it is filed under another act, whatever its punctuation, and
    carries none when it is filed under the typed artist's own spelling, even
    when it credits the artist too (the label would fire under rung equality
    alone for the neighbour, and for the own row if "filed under" were wrong)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shed", [None, DegradedReason.deadline_exceeded])
    @pytest.mark.parametrize(
        ("typed", "own", "other"),
        [
            ("Girls", "Girls", "The Girls"),
            ("Mark Almond", "Mark Almond", "Mark-Almond"),
        ],
    )
    async def test_credited_row_under_another_act_is_labelled(
        self, tmp_path, mock_discogs_service, typed, own, other, shed
    ):
        credit = f"{typed} & Friends"
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        request = LookupRequest(artist=typed, album="Album One", raw_message=f"{typed} - Album")
        async with library_catalog(
            tmp_path,
            [(own, "Album One", credit), (other, "Album One", credit)],
            call_letters={own: "MA", other: "AL"},
        ) as db:
            with patch("lookup.orchestrator.should_shed_tail", return_value=shed):
                response = await perform_lookup(
                    request, db, mock_discogs_service, make_lml_telemetry()
                )

        labels = {i.library_item.artist: i.matched_via_alias for i in response.results}
        assert labels[own] is None
        assert labels[other] is not None


class TestPerformLookupReusesTheSearchsSpellings:
    """The label reads the spellings the artist+album lane already picked
    (LML#1490): the response build, on the full path and the shed one, adds no
    library read, so a library error there cannot turn a degraded response
    into an error."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shed", [None, DegradedReason.deadline_exceeded])
    async def test_labelling_adds_no_spellings_read(self, tmp_path, mock_discogs_service, shed):
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        request = LookupRequest(
            artist="Afel Bocoum", album="Mali Music", raw_message="Afel Bocoum - Mali"
        )
        async with library_catalog(
            tmp_path, [("Afel Bocoum", "Mali Music"), ("Damon Albarn", "Mali Music", CREDIT)]
        ) as db:
            reads = []
            read = db.artist_names_matching

            async def counted(phrases):
                reads.append(phrases)
                return await read(phrases)

            with (
                patch.object(db, "artist_names_matching", counted),
                patch("lookup.orchestrator.should_shed_tail", return_value=shed),
            ):
                response = await perform_lookup(
                    request, db, mock_discogs_service, make_lml_telemetry()
                )

        assert len(reads) == 1
        assert [i.matched_via_alias for i in response.results] == [None, HINT]


class TestPerformLookupPunctuationVariants:
    """Same call letters means one act (LML#1449), so a credited row filed
    under the typed artist's punctuation variant carries no label."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shed", [None, DegradedReason.deadline_exceeded])
    async def test_credited_variant_on_the_same_call_letters_carries_no_label(
        self, tmp_path, mock_discogs_service, shed
    ):
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        request = LookupRequest(artist="Alaska", album="Emotions", raw_message="Alaska - Emotions")
        async with library_catalog(
            tmp_path,
            [("Alaska", "Emotions"), ("Alaska!", "Emotions", "Alaska & Friends")],
            call_letters={"Alaska": "AL", "Alaska!": "AL"},
        ) as db:
            with patch("lookup.orchestrator.should_shed_tail", return_value=shed):
                response = await perform_lookup(
                    request, db, mock_discogs_service, make_lml_telemetry()
                )

        assert [(i.library_item.artist, i.matched_via_alias) for i in response.results] == [
            ("Alaska", None),
            ("Alaska!", None),
        ]

    @pytest.mark.asyncio
    async def test_spelling_fts_never_reaches_is_not_the_artist_so_its_credited_row_is_labelled(
        self, tmp_path, mock_discogs_service
    ):
        """Accepted consequence of deciding "filed under" by ``artist_spellings`` (LML#1490).

        The index tokenizes "Beak>" as "beak", yet ``artist_names_matching``
        never reaches it for a typed "Beak", so the spellings are ["Beak"] and
        a credited row filed under "Beak>" is labelled, as the row readers,
        which also read only "Beak", treat it as another artist's row.
        """
        rows = [("Beak", "Album One"), ("Beak>", "Album One", "Beak & Friends")]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        request = LookupRequest(artist="Beak", album="Album One", raw_message="Beak - Album One")
        async with library_catalog(
            tmp_path, rows, call_letters={"Beak": "BE", "Beak>": "BE"}
        ) as db:
            spellings = await artist_spellings(db, "Beak")
            response = await perform_lookup(request, db, mock_discogs_service, make_lml_telemetry())

        assert spellings == ["Beak"]
        labels = {i.library_item.artist: i.matched_via_alias for i in response.results}
        assert labels["Beak"] is None
        assert labels["Beak>"] is not None


class TestSearchReportsItsSpellings:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "expected"),
        [("Girls", ["Girls"]), ("Cat Power", []), ("The Girls", ["The Girls"])],
    )
    async def test_on_spellings_receives_the_artists_spellings(self, tmp_path, typed, expected):
        got: list[list[str]] = []
        parsed = ParsedRequest(artist=typed, album="Album One", song=None, raw_message=typed)
        async with library_catalog(
            tmp_path, [("Girls", "Album One"), ("The Girls", "Other")]
        ) as db:
            await search_library_with_fallback(db, parsed, ["Album One"], on_spellings=got.append)

        assert got == [expected]
