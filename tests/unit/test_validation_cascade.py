"""Unit tests for the Step-3b policy cascade (LML#750).

``apply_track_validation_cascade`` sequences the tiers that used to live inline
in ``lookup/orchestrator.py``'s ``_step_validate_tracks``: per-result Discogs
track validation, the LML#629 A4 cached-track promotion, the LML#717
song-as-album-title promotion, and the compilation artist-fallback merge.
These tests exercise the cascade directly (mocking its Discogs-facing
collaborators), independent of ``perform_lookup``.
"""

from unittest.mock import AsyncMock, patch

import pytest

from lookup.validation import Step3bResult, apply_track_validation_cascade
from tests.factories import make_library_item


@pytest.mark.asyncio
class TestApplyTrackValidationCascade:
    async def test_per_result_validation_confirms_a_match(self):
        """A confirmed per-result match wins outright; nothing else runs."""
        item = make_library_item(id=1, title="A Night at the Opera")
        with (
            patch(
                "lookup.validation.filter_results_by_track_validation",
                new_callable=AsyncMock,
                return_value=[item],
            ),
            patch(
                "lookup.validation.find_library_albums_with_cached_track",
                new_callable=AsyncMock,
            ) as mock_cached_track,
        ):
            result = await apply_track_validation_cascade(
                real_results=[item],
                library_results=[item],
                found_on_compilation=False,
                song_not_found=True,
                discogs_titles={},
                artist_fallback_results=[],
                song="Bohemian Rhapsody",
                artist="Queen",
                match_artist="Queen",
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        assert result == Step3bResult([item], False, {})
        mock_cached_track.assert_not_called()

    async def test_no_validation_confirmation_and_song_already_found_is_a_no_op(self):
        """If validation confirms nothing but song_not_found is already False, don't cascade further."""
        item = make_library_item(id=1)
        with (
            patch(
                "lookup.validation.filter_results_by_track_validation",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "lookup.validation.find_library_albums_with_cached_track",
                new_callable=AsyncMock,
            ) as mock_cached_track,
        ):
            result = await apply_track_validation_cascade(
                real_results=[item],
                library_results=[item],
                found_on_compilation=False,
                song_not_found=False,
                discogs_titles={"9": "unused"},
                artist_fallback_results=[],
                song="Some Song",
                artist="Some Artist",
                match_artist="Some Artist",
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        assert result == Step3bResult([item], False, {"9": "unused"})
        mock_cached_track.assert_not_called()

    async def test_a4_cached_track_promotion_wins_after_validation_miss(self):
        """LML#629: a cache-confirmed promotion supersedes the unvalidated fallback."""
        fallback_item = make_library_item(id=1, title="The Game")
        promoted_item = make_library_item(id=2, title="A Night at the Opera")
        with (
            patch(
                "lookup.validation.filter_results_by_track_validation",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "lookup.validation.find_library_albums_with_cached_track",
                new_callable=AsyncMock,
                return_value=([promoted_item], {2: "resolved-release"}),
            ),
        ):
            result = await apply_track_validation_cascade(
                real_results=[fallback_item],
                library_results=[fallback_item],
                found_on_compilation=False,
                song_not_found=True,
                discogs_titles={1: "existing"},
                artist_fallback_results=[],
                song="Bohemian Rhapsody",
                artist="Queen",
                match_artist="Queen",
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        assert result.library_results == [promoted_item]
        assert result.song_not_found is False
        assert result.discogs_titles == {1: "existing", 2: "resolved-release"}

    async def test_song_as_album_title_promotion_after_every_other_tier_misses(self):
        """LML#717: a surviving row whose title matches the typed song is promoted."""
        item = make_library_item(id=1, title="On Patrol")
        with (
            patch(
                "lookup.validation.filter_results_by_track_validation",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "lookup.validation.find_library_albums_with_cached_track",
                new_callable=AsyncMock,
                return_value=([], {}),
            ),
        ):
            result = await apply_track_validation_cascade(
                real_results=[item],
                library_results=[item],
                found_on_compilation=False,
                song_not_found=True,
                discogs_titles={},
                artist_fallback_results=[],
                song="On Patrol",
                artist="Sun Araw",
                match_artist="Sun Araw",
                db=object(),
                discogs_service=None,
                allow_release_resolution_fallback=True,
            )

        assert result == Step3bResult([item], False, {})

    async def test_every_tier_misses_preserves_song_not_found(self):
        """No tier confirms anything: state passes through unchanged."""
        item = make_library_item(id=1, title="Unrelated Album")
        with (
            patch(
                "lookup.validation.filter_results_by_track_validation",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "lookup.validation.find_library_albums_with_cached_track",
                new_callable=AsyncMock,
                return_value=([], {}),
            ),
        ):
            result = await apply_track_validation_cascade(
                real_results=[item],
                library_results=[item],
                found_on_compilation=False,
                song_not_found=True,
                discogs_titles={},
                artist_fallback_results=[],
                song="Totally Different Song",
                artist="Sun Araw",
                match_artist="Sun Araw",
                db=object(),
                discogs_service=None,
                allow_release_resolution_fallback=True,
            )

        assert result == Step3bResult([item], True, {})

    async def test_compilation_branch_merges_confirmed_artist_fallback_ahead_of_compilation(self):
        """On a compilation hit, a confirmed artist-fallback row is prepended."""
        compilation_item = make_library_item(id=1, title="Some Compilation")
        fallback_item = make_library_item(id=2, title="The Artist's Own Album")
        with patch(
            "lookup.validation.filter_results_by_track_validation",
            new_callable=AsyncMock,
            return_value=[fallback_item],
        ) as mock_validate:
            result = await apply_track_validation_cascade(
                real_results=[compilation_item],
                library_results=[compilation_item],
                found_on_compilation=True,
                song_not_found=False,
                discogs_titles={},
                artist_fallback_results=[fallback_item],
                song="Some Song",
                artist="Some Artist",
                match_artist="Some Artist",
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        mock_validate.assert_awaited_once()
        assert mock_validate.call_args.args[:3] == ([fallback_item], "Some Song", "Some Artist")
        assert result.library_results == [fallback_item, compilation_item]
        assert result.song_not_found is False

    async def test_compilation_branch_no_confirmation_keeps_compilation_results(self):
        """On a compilation hit with no confirmed artist-fallback match, results are unchanged."""
        compilation_item = make_library_item(id=1, title="Some Compilation")
        fallback_item = make_library_item(id=2, title="Unrelated Album")
        with patch(
            "lookup.validation.filter_results_by_track_validation",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await apply_track_validation_cascade(
                real_results=[compilation_item],
                library_results=[compilation_item],
                found_on_compilation=True,
                song_not_found=False,
                discogs_titles={},
                artist_fallback_results=[fallback_item],
                song="Some Song",
                artist="Some Artist",
                match_artist="Some Artist",
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        assert result == Step3bResult([compilation_item], False, {})
