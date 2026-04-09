"""Tests for pipeline input construction — ensuring Deezer-matched names flow to Spotify."""

import pytest
import pytest_asyncio

from scripts.streaming_availability.dedup import DeduplicatedAlbum
from scripts.streaming_availability.results_db import ResultsDB


@pytest_asyncio.fixture
async def db_with_deezer_hit():
    """A results DB with one album: Discogs enriched, Deezer found, Spotify pending."""
    db = ResultsDB(":memory:")
    await db.connect()

    album = DeduplicatedAlbum(
        normalized_artist="de la soul",
        normalized_title="buhloone mind state",
        display_artist="De La Soul",
        display_title="Buhloone Mind State",
        library_ids=[1],
        formats=["cd"],
        genre="Hiphop",
        label=None,
        is_compilation=False,
        is_single=False,
    )
    await db.insert_albums([album])

    # Simulate Discogs enrichment
    rows = await db.get_pending_discogs(limit=1)
    await db.update_discogs_result(
        rows[0]["id"],
        "found",
        artist="De La Soul",
        title="Buhloone Mindstate",
        release_id=12345,
    )

    # Simulate Deezer found with a different title
    rows = await db.get_pending("deezer", limit=1)
    await db.update_result(
        rows[0]["id"],
        "deezer",
        "found",
        url="https://www.deezer.com/album/123",
        confidence=95.0,
        matched_artist="De La Soul",
        matched_title="Buhloone Mindstate",
    )

    yield db
    await db.close()


class TestPipelineInputConstruction:
    @pytest.mark.asyncio
    async def test_deezer_matched_names_available_for_spotify(self, db_with_deezer_hit):
        """When building pipeline input for Spotify-pending albums,
        Deezer-matched names should be included in the dict."""
        db = db_with_deezer_hit
        rows = await db.get_deezer_hits_pending_spotify(limit=10)

        assert len(rows) == 1
        row = rows[0]

        # These should be available from the DB row
        assert row["deezer_matched_artist"] == "De La Soul"
        assert row["deezer_matched_title"] == "Buhloone Mindstate"
        assert row["discogs_artist"] == "De La Soul"
        assert row["discogs_title"] == "Buhloone Mindstate"
