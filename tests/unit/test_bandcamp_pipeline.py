"""Unit tests for scripts/bandcamp_pipeline.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from scripts.streaming_availability.dedup import DeduplicatedAlbum
from scripts.streaming_availability.results_db import ResultsDB


def _make_album(
    normalized_artist: str = "stereolab",
    normalized_title: str = "aluminum tunes",
    display_artist: str = "Stereolab",
    display_title: str = "Aluminum Tunes",
    library_ids: list[int] | None = None,
    formats: list[str] | None = None,
    genre: str | None = "Rock",
    label: str | None = "Duophonic",
    is_compilation: bool = False,
) -> DeduplicatedAlbum:
    return DeduplicatedAlbum(
        normalized_artist=normalized_artist,
        normalized_title=normalized_title,
        display_artist=display_artist,
        display_title=display_title,
        library_ids=library_ids if library_ids is not None else [1],
        formats=formats if formats is not None else ["cd"],
        genre=genre,
        label=label,
        is_compilation=is_compilation,
    )


@pytest_asyncio.fixture
async def db():
    """In-memory results database with bandcamp_slug migration applied."""
    results_db = ResultsDB(":memory:")
    await results_db.connect()
    yield results_db
    await results_db.close()


class TestMigrateExistingBandcampUrls:
    @pytest.mark.asyncio
    async def test_album_level_url_extracts_slug_keeps_url(self, db):
        from scripts.bandcamp_pipeline import migrate_existing_bandcamp_urls

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_bandcamp_url(
            album_id, "https://stereolab.bandcamp.com/album/aluminum-tunes"
        )

        await migrate_existing_bandcamp_urls(db)

        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_slug"] == "stereolab"
        assert rows[0]["bandcamp_url"] == "https://stereolab.bandcamp.com/album/aluminum-tunes"

    @pytest.mark.asyncio
    async def test_artist_level_url_extracts_slug_clears_url(self, db):
        from scripts.bandcamp_pipeline import migrate_existing_bandcamp_urls

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_bandcamp_url(album_id, "https://stereolab.bandcamp.com")

        await migrate_existing_bandcamp_urls(db)

        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_slug"] == "stereolab"
        assert rows[0]["bandcamp_url"] is None

    @pytest.mark.asyncio
    async def test_artist_level_url_resets_status_to_pending(self, db):
        from scripts.bandcamp_pipeline import migrate_existing_bandcamp_urls

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        # An artist-level URL was previously written (which marks status 'found').
        await db.update_bandcamp_url(album_id, "https://stereolab.bandcamp.com")

        await migrate_existing_bandcamp_urls(db)

        # Clearing the URL for re-matching must re-enable Phase 2: the album
        # is pending again, not stuck at 'found' (#661).
        pending = await db.get_pending_bandcamp_lookup()
        assert len(pending) == 1
        assert pending[0]["id"] == album_id
        assert pending[0]["bandcamp_status"] == "pending"

    @pytest.mark.asyncio
    async def test_idempotent_on_rerun(self, db):
        from scripts.bandcamp_pipeline import migrate_existing_bandcamp_urls

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_bandcamp_url(
            album_id, "https://stereolab.bandcamp.com/album/aluminum-tunes"
        )

        await migrate_existing_bandcamp_urls(db)
        await migrate_existing_bandcamp_urls(db)  # second run

        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_slug"] == "stereolab"
        assert rows[0]["bandcamp_url"] == "https://stereolab.bandcamp.com/album/aluminum-tunes"

    @pytest.mark.asyncio
    async def test_skips_albums_without_url(self, db):
        from scripts.bandcamp_pipeline import migrate_existing_bandcamp_urls

        await db.insert_albums([_make_album()])

        await migrate_existing_bandcamp_urls(db)

        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_slug"] is None
        assert rows[0]["bandcamp_url"] is None


class TestPhaseSearch:
    @pytest.mark.asyncio
    async def test_writes_slug_on_match(self, db):
        from scripts.bandcamp_pipeline import phase_search

        await db.insert_albums([_make_album()])
        # Mark as not found on spotify so it shows up in not-on-streaming query
        rows = await db.get_pending("spotify", limit=10)
        await db.update_result(rows[0]["id"], "spotify", "not_found")

        mock_client = AsyncMock()
        mock_client.search_artist = AsyncMock(
            return_value=[
                {"name": "Stereolab", "url": "https://stereolab.bandcamp.com", "slug": "stereolab"}
            ]
        )

        slugs = await phase_search(mock_client, db)

        assert slugs["Stereolab"] == "stereolab"
        pending = await db.get_pending_bandcamp_lookup()
        assert len(pending) == 1
        assert pending[0]["bandcamp_slug"] == "stereolab"

    @pytest.mark.asyncio
    async def test_sets_empty_slug_on_no_match(self, db):
        from scripts.bandcamp_pipeline import phase_search

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_result(rows[0]["id"], "spotify", "not_found")

        mock_client = AsyncMock()
        mock_client.search_artist = AsyncMock(return_value=[])

        slugs = await phase_search(mock_client, db)

        assert len(slugs) == 0
        # Should be marked with empty string so we don't re-search
        pending = await db.get_pending_bandcamp_lookup()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_skips_artists_with_existing_slug(self, db):
        from scripts.bandcamp_pipeline import phase_search

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_result(rows[0]["id"], "spotify", "not_found")
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")

        mock_client = AsyncMock()
        mock_client.search_artist = AsyncMock(return_value=[])

        await phase_search(mock_client, db)

        mock_client.search_artist.assert_not_called()

    @pytest.mark.asyncio
    async def test_include_streaming_expands_scope(self, db):
        from scripts.bandcamp_pipeline import phase_search

        await db.insert_albums([_make_album()])
        # Leave spotify_status as 'pending' -- wouldn't show up with not_on_streaming_only

        mock_client = AsyncMock()
        mock_client.search_artist = AsyncMock(
            return_value=[
                {"name": "Stereolab", "url": "https://stereolab.bandcamp.com", "slug": "stereolab"}
            ]
        )

        slugs = await phase_search(mock_client, db, include_streaming=True)

        assert slugs["Stereolab"] == "stereolab"

    @pytest.mark.asyncio
    async def test_respects_limit(self, db):
        from scripts.bandcamp_pipeline import phase_search

        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="autechre",
                    normalized_title="confield",
                    display_artist="Autechre",
                    display_title="Confield",
                ),
            ]
        )
        all_rows = await db.get_pending("spotify", limit=10)
        for r in all_rows:
            await db.update_result(r["id"], "spotify", "not_found")

        mock_client = AsyncMock()
        mock_client.search_artist = AsyncMock(return_value=[])

        await phase_search(mock_client, db, limit=1)

        assert mock_client.search_artist.call_count == 1

    @pytest.mark.asyncio
    async def test_fuzzy_match_threshold(self, db):
        from scripts.bandcamp_pipeline import phase_search

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_result(rows[0]["id"], "spotify", "not_found")

        # Return a band with a name that won't meet the 75 threshold
        # "Stereolab" vs "Stereo Total" -> token_sort_ratio ~67
        mock_client = AsyncMock()
        mock_client.search_artist = AsyncMock(
            return_value=[
                {
                    "name": "Stereo Total",
                    "url": "https://stereototal.bandcamp.com",
                    "slug": "stereototal",
                }
            ]
        )

        slugs = await phase_search(mock_client, db)

        assert len(slugs) == 0

    @pytest.mark.asyncio
    async def test_sets_slug_for_all_albums_by_artist(self, db):
        from scripts.bandcamp_pipeline import phase_search

        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="stereolab",
                    normalized_title="dots and loops",
                    display_artist="Stereolab",
                    display_title="Dots and Loops",
                ),
            ]
        )
        all_rows = await db.get_pending("spotify", limit=10)
        for r in all_rows:
            await db.update_result(r["id"], "spotify", "not_found")

        mock_client = AsyncMock()
        mock_client.search_artist = AsyncMock(
            return_value=[
                {"name": "Stereolab", "url": "https://stereolab.bandcamp.com", "slug": "stereolab"}
            ]
        )

        await phase_search(mock_client, db)

        pending = await db.get_pending_bandcamp_lookup()
        assert len(pending) == 2
        assert all(r["bandcamp_slug"] == "stereolab" for r in pending)


class TestPhaseLookup:
    @pytest.mark.asyncio
    async def test_writes_album_url_on_match(self, db):
        from scripts.bandcamp_pipeline import phase_lookup

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")

        mock_client = AsyncMock()
        mock_client.fetch_artist_catalog = AsyncMock(
            return_value=[
                {
                    "url": "https://stereolab.bandcamp.com/album/aluminum-tunes",
                    "title": "Aluminum Tunes",
                },
            ]
        )

        results = await phase_lookup(mock_client, db)

        assert len(results) == 1
        assert results[0]["bandcamp_url"] == "https://stereolab.bandcamp.com/album/aluminum-tunes"

        # Verify DB was updated
        pending = await db.get_pending_bandcamp_lookup()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_no_match_marks_not_found_and_excludes_from_pending(self, db):
        from scripts.bandcamp_pipeline import phase_lookup

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")

        mock_client = AsyncMock()
        mock_client.fetch_artist_catalog = AsyncMock(
            return_value=[
                {
                    "url": "https://stereolab.bandcamp.com/album/something-else",
                    "title": "Something Else",
                },
            ]
        )

        results = await phase_lookup(mock_client, db)

        assert len(results) == 0
        # URL stays null, but the album is durably marked attempted so a re-run
        # skips it instead of re-scraping forever (resumability, #661).
        all_rows = await db.get_all_results()
        assert all_rows[0]["bandcamp_url"] is None
        assert all_rows[0]["bandcamp_status"] == "not_found"
        assert len(await db.get_pending_bandcamp_lookup()) == 0

    @pytest.mark.asyncio
    async def test_empty_catalog_marks_not_found(self, db):
        from scripts.bandcamp_pipeline import phase_lookup

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")

        mock_client = AsyncMock()
        # [] is a successful fetch of a genuinely empty catalog.
        mock_client.fetch_artist_catalog = AsyncMock(return_value=[])

        await phase_lookup(mock_client, db)

        all_rows = await db.get_all_results()
        assert all_rows[0]["bandcamp_status"] == "not_found"
        assert len(await db.get_pending_bandcamp_lookup()) == 0

    @pytest.mark.asyncio
    async def test_fetch_failure_leaves_pending(self, db):
        from scripts.bandcamp_pipeline import phase_lookup

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")

        mock_client = AsyncMock()
        # None signals a transient fetch failure (timeout / 5xx / rate-limit
        # exhausted), NOT a definitively-empty catalog. Marking it not_found
        # would permanently drop the slug on a network blip during the drain.
        mock_client.fetch_artist_catalog = AsyncMock(return_value=None)

        await phase_lookup(mock_client, db)

        all_rows = await db.get_all_results()
        assert all_rows[0]["bandcamp_status"] == "pending"
        assert all_rows[0]["bandcamp_url"] is None
        # Still pending -> a re-run will retry it.
        assert len(await db.get_pending_bandcamp_lookup()) == 1

    @pytest.mark.asyncio
    async def test_rerun_skips_attempted_no_match(self, db):
        from scripts.bandcamp_pipeline import phase_lookup

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")

        mock_client = AsyncMock()
        mock_client.fetch_artist_catalog = AsyncMock(
            return_value=[
                {
                    "url": "https://stereolab.bandcamp.com/album/something-else",
                    "title": "Something Else",
                },
            ]
        )

        await phase_lookup(mock_client, db)  # first attempt: no match -> not_found
        mock_client.fetch_artist_catalog.reset_mock()

        await phase_lookup(mock_client, db)  # re-run must skip the attempted slug
        mock_client.fetch_artist_catalog.assert_not_called()

    @pytest.mark.asyncio
    async def test_artist_fallback_writes_artist_url(self, db):
        from scripts.bandcamp_pipeline import phase_lookup

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")

        mock_client = AsyncMock()
        mock_client.fetch_artist_catalog = AsyncMock(
            return_value=[
                {
                    "url": "https://stereolab.bandcamp.com/album/totally-different",
                    "title": "Totally Different",
                },
            ]
        )

        await phase_lookup(mock_client, db, artist_fallback=True)

        # Should write fallback artist-level URL
        pending = await db.get_pending_bandcamp_lookup()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_groups_by_slug_avoids_duplicate_fetches(self, db):
        from scripts.bandcamp_pipeline import phase_lookup

        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="stereolab",
                    normalized_title="dots and loops",
                    display_artist="Stereolab",
                    display_title="Dots and Loops",
                ),
            ]
        )
        all_rows = await db.get_pending("spotify", limit=10)
        for r in all_rows:
            await db.update_bandcamp_slug(r["id"], "stereolab")

        mock_client = AsyncMock()
        mock_client.fetch_artist_catalog = AsyncMock(
            return_value=[
                {
                    "url": "https://stereolab.bandcamp.com/album/aluminum-tunes",
                    "title": "Aluminum Tunes",
                },
                {
                    "url": "https://stereolab.bandcamp.com/album/dots-and-loops",
                    "title": "Dots and Loops",
                },
            ]
        )

        results = await phase_lookup(mock_client, db)

        # Should only fetch once for "stereolab" slug
        mock_client.fetch_artist_catalog.assert_called_once_with("stereolab")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_uses_score_match_for_fuzzy_matching(self, db):
        from scripts.bandcamp_pipeline import phase_lookup

        await db.insert_albums(
            [
                _make_album(
                    normalized_artist="stereolab",
                    normalized_title="aluminum tunes",
                    display_artist="Stereolab",
                    display_title='Aluminum Tunes 12"',
                ),
            ]
        )
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")

        mock_client = AsyncMock()
        mock_client.fetch_artist_catalog = AsyncMock(
            return_value=[
                {
                    "url": "https://stereolab.bandcamp.com/album/aluminum-tunes",
                    "title": "Aluminum Tunes",
                },
            ]
        )

        results = await phase_lookup(mock_client, db)

        # Should match despite format suffix in library title
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_respects_limit(self, db):
        from scripts.bandcamp_pipeline import phase_lookup

        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="autechre",
                    normalized_title="confield",
                    display_artist="Autechre",
                    display_title="Confield",
                ),
            ]
        )
        all_rows = await db.get_pending("spotify", limit=10)
        for r in all_rows:
            await db.update_bandcamp_slug(r["id"], r["display_artist"].lower())

        mock_client = AsyncMock()
        mock_client.fetch_artist_catalog = AsyncMock(return_value=[])

        await phase_lookup(mock_client, db, limit=1)

        assert mock_client.fetch_artist_catalog.call_count == 1

    @pytest.mark.asyncio
    async def test_empty_catalog_no_crash(self, db):
        from scripts.bandcamp_pipeline import phase_lookup

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")

        mock_client = AsyncMock()
        mock_client.fetch_artist_catalog = AsyncMock(return_value=[])

        results = await phase_lookup(mock_client, db)

        assert len(results) == 0


class TestConcurrentPipeline:
    @pytest.mark.asyncio
    async def test_search_feeds_lookup_via_queue(self, db):
        from scripts.bandcamp_pipeline import run_concurrent

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_result(rows[0]["id"], "spotify", "not_found")

        mock_client = AsyncMock()
        mock_client.search_artist = AsyncMock(
            return_value=[
                {"name": "Stereolab", "url": "https://stereolab.bandcamp.com", "slug": "stereolab"}
            ]
        )
        mock_client.fetch_artist_catalog = AsyncMock(
            return_value=[
                {
                    "url": "https://stereolab.bandcamp.com/album/aluminum-tunes",
                    "title": "Aluminum Tunes",
                },
            ]
        )

        results = await run_concurrent(mock_client, db)

        assert len(results) == 1
        assert results[0]["bandcamp_url"] == "https://stereolab.bandcamp.com/album/aluminum-tunes"
        # Verify the lookup was triggered by search discovering the slug
        mock_client.fetch_artist_catalog.assert_called_once_with("stereolab")

    @pytest.mark.asyncio
    async def test_lookup_processes_existing_slugs_and_new_discoveries(self, db):
        from scripts.bandcamp_pipeline import run_concurrent

        # Album 1: already has a slug (e.g. from Wikidata)
        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="autechre",
                    normalized_title="confield",
                    display_artist="Autechre",
                    display_title="Confield",
                ),
            ]
        )
        all_rows = await db.get_pending("spotify", limit=10)
        stereolab_id = next(r["id"] for r in all_rows if r["display_artist"] == "Stereolab")
        autechre_id = next(r["id"] for r in all_rows if r["display_artist"] == "Autechre")

        # Stereolab already has slug, Autechre does not
        await db.update_bandcamp_slug(stereolab_id, "stereolab")
        await db.update_result(autechre_id, "spotify", "not_found")

        mock_client = AsyncMock()
        mock_client.search_artist = AsyncMock(
            return_value=[
                {"name": "Autechre", "url": "https://autechre.bandcamp.com", "slug": "autechre"}
            ]
        )

        async def fake_catalog(slug):
            if slug == "stereolab":
                return [
                    {
                        "url": "https://stereolab.bandcamp.com/album/aluminum-tunes",
                        "title": "Aluminum Tunes",
                    }
                ]
            if slug == "autechre":
                return [
                    {"url": "https://autechre.bandcamp.com/album/confield", "title": "Confield"}
                ]
            return []

        mock_client.fetch_artist_catalog = AsyncMock(side_effect=fake_catalog)

        results = await run_concurrent(mock_client, db)

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_error_in_search_does_not_crash_lookup(self, db):
        from scripts.bandcamp_pipeline import run_concurrent

        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="autechre",
                    normalized_title="confield",
                    display_artist="Autechre",
                    display_title="Confield",
                ),
            ]
        )
        all_rows = await db.get_pending("spotify", limit=10)
        # Give Stereolab an existing slug so lookup can process it
        stereolab_id = next(r["id"] for r in all_rows if r["display_artist"] == "Stereolab")
        await db.update_bandcamp_slug(stereolab_id, "stereolab")

        autechre_id = next(r["id"] for r in all_rows if r["display_artist"] == "Autechre")
        await db.update_result(autechre_id, "spotify", "not_found")

        mock_client = AsyncMock()
        # Search raises an error
        mock_client.search_artist = AsyncMock(side_effect=Exception("API down"))
        mock_client.fetch_artist_catalog = AsyncMock(
            return_value=[
                {
                    "url": "https://stereolab.bandcamp.com/album/aluminum-tunes",
                    "title": "Aluminum Tunes",
                },
            ]
        )

        # Should still process the existing slug despite search failure
        results = await run_concurrent(mock_client, db)

        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_queue_event_only_fetches_new_slug(self, db):
        """A queue event must fetch only the newly discovered slug's catalog.

        Regression for #125: the consumer used to call phase_lookup() with no
        filter on every queue event, re-scanning the entire pending set. Any
        album that never matched stayed pending forever, so each newly
        discovered slug re-fetched every still-pending catalog -- O(slugs x
        catalogs) instead of one fetch per slug.
        """
        from scripts.bandcamp_pipeline import run_concurrent

        # Stereolab already has a slug but its catalog will NOT match its
        # album, so it stays pending (bandcamp_url NULL) for the whole run.
        # Autechre is discovered by search and arrives via the queue.
        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="autechre",
                    normalized_title="confield",
                    display_artist="Autechre",
                    display_title="Confield",
                ),
            ]
        )
        all_rows = await db.get_pending("spotify", limit=10)
        stereolab_id = next(r["id"] for r in all_rows if r["display_artist"] == "Stereolab")
        autechre_id = next(r["id"] for r in all_rows if r["display_artist"] == "Autechre")

        await db.update_bandcamp_slug(stereolab_id, "stereolab")
        await db.update_result(autechre_id, "spotify", "not_found")

        mock_client = AsyncMock()
        mock_client.search_artist = AsyncMock(
            return_value=[
                {"name": "Autechre", "url": "https://autechre.bandcamp.com", "slug": "autechre"}
            ]
        )

        async def fake_catalog(slug):
            if slug == "autechre":
                return [
                    {"url": "https://autechre.bandcamp.com/album/confield", "title": "Confield"}
                ]
            # Stereolab's catalog never matches "Aluminum Tunes"
            return [
                {
                    "url": "https://stereolab.bandcamp.com/album/something-else",
                    "title": "Something Else",
                }
            ]

        mock_client.fetch_artist_catalog = AsyncMock(side_effect=fake_catalog)

        await run_concurrent(mock_client, db)

        fetched = [call.args[0] for call in mock_client.fetch_artist_catalog.call_args_list]
        # Stereolab is fetched exactly once (the initial pre-existing pass);
        # the autechre queue event must NOT re-scrape it.
        assert fetched.count("stereolab") == 1, fetched
        assert fetched.count("autechre") == 1, fetched


class TestProcessBatched:
    @pytest.mark.asyncio
    async def test_processes_all_items(self):
        from scripts.bandcamp_pipeline import process_batched

        items = [1, 2, 3, 4, 5]
        results = await process_batched(
            items, lambda x: asyncio.sleep(0, result=x * 2), batch_size=2
        )
        assert sorted(results) == [2, 4, 6, 8, 10]

    @pytest.mark.asyncio
    async def test_exception_in_one_does_not_fail_batch(self):
        from scripts.bandcamp_pipeline import process_batched

        async def failing_fn(x):
            if x == 3:
                raise ValueError("boom")
            return x

        results = await process_batched([1, 2, 3, 4], failing_fn, batch_size=2)
        # Should have 3 successes and 1 exception
        successes = [r for r in results if not isinstance(r, Exception)]
        assert sorted(successes) == [1, 2, 4]

    @pytest.mark.asyncio
    async def test_empty_input(self):
        from scripts.bandcamp_pipeline import process_batched

        results = await process_batched([], lambda x: asyncio.sleep(0, result=x), batch_size=3)
        assert results == []
