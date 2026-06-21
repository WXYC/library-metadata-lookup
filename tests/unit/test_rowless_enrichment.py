"""Regression guard for the row-less enrichment split (LML#630).

A *row-less* result is a ``LibraryItem(id=0)`` (no WXYC catalog row) paired with
a real Discogs identity in ``artwork`` (``release_id > 0``). LML#628's A1
carry-through surfaces these for non-library releases that resolve + validate
from the inputs.

LML#630 asks that a row-less release carry the **full resolved-release
enrichment** — artwork, bio/wiki, and the entire ``extended``-mode Discogs
payload (``tracklist`` / ``genres`` / ``styles`` / ``label`` /
``full_release_date`` / ``artist_image_url`` / ``discogs_artist_id`` /
``profile_tokens``) — while curated, genuinely library-only signals (the
librarian ``streaming_links`` override, call number, filing) stay gated on a
real row.

That behavior is **already delivered**, as a side effect of LML#628: ``enrich_one``
gates the extended payload on ``library_row_acceptable``, which is
``artwork is not None and row_title_matches_requested_album``; LML#628 added a
title-gate bypass clause —

    or (item.id == ROWLESS_LIBRARY_ID and artwork is not None and artwork.release_id > 0)

— so a row-less carry-through item clears the gate even when its (track-resolved)
release title differs from the typed album. Flipping ``library_row_acceptable``
to ``True`` opens ``is_album_derived_eligible`` / ``is_artist_derived_eligible``
and skips the synthesis branch, so the extended payload + bio/wiki populate.

Nothing PINS that today: ``test_rowless_result_contract.py`` deliberately stubs
``get_release -> None`` (so the enrichment pass is a no-op and only the carried
``release_id``/``release_url`` are asserted), and the ``extended``-payload tests
in ``test_enrichment.py`` all use row-*backed* items. A future refactor of the
title-gate clause or the eligibility gates could silently regress the row-less
payload with no failing test. This file is that guard — a characterization test,
not a red-then-green TDD driver: it passes against current code; the point is it
must KEEP passing.

Companion to ``test_rowless_result_contract.py`` (the wire-contract of the
emitted item) and ``test_enrichment.py`` (the row-backed extended path).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.models import ArtistDetails, ReleaseMetadataResponse, TrackItem
from lookup.orchestrator import ROWLESS_LIBRARY_ID, enrich_artwork_results
from tests.factories import make_discogs_result, make_library_item

# A non-library release that resolves on Discogs. The request album is
# deliberately a *different string* from the release title in each test below,
# so the LML#487 title gate would suppress the payload were it not for LML#628's
# row-less bypass — that is the exact condition this file guards.
RELEASE_ID = 10154369
RELEASE_TITLE = "On Your Own Love Again"
ARTIST = "Jessica Pratt"
SONG = "Wrong Hand"
# A typed album that does NOT fuzzy-match the release title (score_match < 80),
# so ``row_title_matches_requested_album`` is False on its own merits.
MISMATCHED_ALBUM = "Quiet Signs"


def _resolved_release() -> ReleaseMetadataResponse:
    return ReleaseMetadataResponse(
        release_id=RELEASE_ID,
        title=RELEASE_TITLE,
        artist=ARTIST,
        year=2015,
        label="Drag City",
        artist_id=42,
        genres=["Rock"],
        styles=["Folk", "Acoustic"],
        tracklist=[TrackItem(position="1", title=SONG, duration="3:00", artists=[])],
        released="2015-01-27",
        release_url=f"https://discogs.com/release/{RELEASE_ID}",
    )


def _artist_details() -> ArtistDetails:
    return ArtistDetails(
        artist_id=42,
        name=ARTIST,
        profile="American singer-songwriter from Los Angeles.",
        image_url="https://img.discogs.com/jp.jpg",
        urls=["https://en.wikipedia.org/wiki/Jessica_Pratt"],
    )


class TestRowlessEnrichmentSplit:
    """A row-less release carries the resolved-release enrichment; library-only
    signals stay gated on a real row."""

    @pytest.mark.asyncio
    async def test_rowless_carries_full_extended_payload_despite_album_mismatch(self):
        """id==0 + real release_id + extended → full payload populates from the
        resolved release, even when the typed album mismatches the release title.

        Pins LML#630's desired end state: artwork, bio/wiki, and every
        ``extended`` field land on the row-less result, and the carried Discogs
        identity is NOT collapsed to the BS#1185 ``release_id=0`` sentinel.
        """
        item = make_library_item(id=ROWLESS_LIBRARY_ID, artist=ARTIST, title=RELEASE_TITLE)
        artwork = make_discogs_result(release_id=RELEASE_ID, artist=ARTIST, album=RELEASE_TITLE)

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = _resolved_release()
        discogs_service.get_artist_details.return_value = _artist_details()

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song=SONG,
            album=MISMATCHED_ALBUM,  # != RELEASE_TITLE — would fail the LML#487 gate on a row
            artist=ARTIST,
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None

        # The carried Discogs identity survives — not the BS#1185 sentinel.
        assert enriched.release_id == RELEASE_ID
        assert enriched.release_url != ""

        # Album-derived extended payload, read from the resolved release.
        assert enriched.genres == ["Rock"]
        assert enriched.styles == ["Folk", "Acoustic"]
        assert enriched.label == "Drag City"
        assert enriched.full_release_date == "2015-01-27"
        assert enriched.tracklist is not None
        assert len(enriched.tracklist) == 1
        assert enriched.tracklist[0].title == SONG
        assert enriched.release_year == 2015

        # Artist-derived enrichment, on the same row-less result.
        assert enriched.artist_image_url == "https://img.discogs.com/jp.jpg"
        assert enriched.discogs_artist_id == 42
        assert enriched.artist_bio is not None
        assert "singer-songwriter" in enriched.artist_bio
        assert enriched.wikipedia_url == "https://en.wikipedia.org/wiki/Jessica_Pratt"
        assert enriched.profile_tokens is not None

    @pytest.mark.asyncio
    async def test_rowless_does_not_consult_curated_streaming_links(self):
        """Curated ``streaming_links`` stay library-only: the override block
        hard-requires ``item.id`` truthy, so a row-less item (id==0) never even
        queries the DB for them — they remain a real-row affordance."""
        item = make_library_item(id=ROWLESS_LIBRARY_ID, artist=ARTIST, title=RELEASE_TITLE)
        artwork = make_discogs_result(release_id=RELEASE_ID, artist=ARTIST, album=RELEASE_TITLE)

        library_db = AsyncMock()
        library_db._has_streaming_links = True
        library_db.get_streaming_links = AsyncMock(
            return_value={"spotify_url": "https://open.spotify.com/album/curated"}
        )

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = _resolved_release()
        discogs_service.get_artist_details.return_value = _artist_details()

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song=SONG,
            album=MISMATCHED_ALBUM,
            artist=ARTIST,
            library_db=library_db,
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None
        # Never queried the curated override — the id==0 gate short-circuits.
        library_db.get_streaming_links.assert_not_awaited()
        # And the curated URL did not leak onto the result.
        assert enriched.spotify_url != "https://open.spotify.com/album/curated"

    @pytest.mark.asyncio
    async def test_rowbacked_album_mismatch_still_suppresses_album_payload(self):
        """No-regression control: a row-*backed* item whose title fails the
        LML#487/#604 gate is STILL collapsed to the streaming-only sentinel and
        loses its *album-derived* payload.

        The row-less bypass keys strictly on ``item.id == ROWLESS_LIBRARY_ID``,
        so it must not loosen the fuzzy-library-mismatch guard for real rows —
        identical inputs to the row-less case, only ``id`` differs.

        Scope note: the *artist-derived* fields (``artist_bio`` / ``wikipedia_url``
        / ``discogs_artist_id`` / ``profile_tokens``) are NOT asserted here — under
        the LML#504 split-gate they ride a verified artist identity, independent
        of album-row acceptability, so they legitimately survive an album-title
        mismatch. This control pins only the album-derived suppression + sentinel
        collapse, which is the #604/#487 guard the row-less bypass must not erode.
        """
        item = make_library_item(id=999, artist=ARTIST, title=RELEASE_TITLE)
        artwork = make_discogs_result(release_id=RELEASE_ID, artist=ARTIST, album=RELEASE_TITLE)

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = _resolved_release()
        discogs_service.get_artist_details.return_value = _artist_details()

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song=SONG,
            album=MISMATCHED_ALBUM,
            artist=ARTIST,
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None
        # Collapsed to the BS#1185 streaming-only sentinel — the wrong-album
        # row's Discogs *release* identity is stripped.
        assert enriched.release_id == 0
        assert enriched.release_url == ""
        # Album-derived payload (incl. ``artist_image_url``, which rides the
        # album-derived gate) is suppressed.
        assert enriched.genres is None
        assert enriched.styles is None
        assert enriched.label is None
        assert enriched.full_release_date is None
        assert enriched.tracklist is None
        assert enriched.artist_image_url is None
