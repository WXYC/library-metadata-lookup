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

from entity.sources import PgSource
from lookup.release_resolution import ResolvedRelease
from lookup.rowless import ROWLESS_LIBRARY_ID, _make_rowless_item
from lookup.validation import (
    Step3bResult,
    _rebind_rowless_release_via_override,
    apply_track_validation_cascade,
)
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

    async def test_shelved_row_named_by_the_song_beats_the_a4_rowless_release(self):
        """A row-less external release must not outrank a shelved row the request named.

        Prod trace, 2026-09-14 19:43:37 PT — "Minimoonstar by Ricardo Villalobos".
        WXYC shelves the CD as *Minimoonstar* (VI 10/2); Discogs titles the same
        record — release 1350337 — *Vasco EP Part 1*, carrying "Minimoonstar" as a
        track. Per-result validation can't confirm the track, so A4 asks the PG
        cache, which confirms it on 1350337 and then matches back by **album
        title** (``search_album_fuzzy(db, release.album)``). Nothing in the catalog
        is titled "Vasco EP Part 1", so A4 concludes the release is not in the
        library and surfaces it row-less — stepping over the shelved row for the
        same physical record, which is also the row the listener named.

        A4 returning first denies the LML#717 tier its say: "Minimoonstar" scores
        100 against the row's title, well over ``_SONG_AS_ALBUM_TITLE_FLOOR``.

        The consequence is user-visible, not cosmetic. request-o-matic strips
        ``id=0`` rows from the request channel (``routers/request.py``, ROM#256)
        because a DJ can't pull a non-shelved album, then re-derives
        ``song_not_found`` from what survives — so the row-less answer reaches the
        DJ as '"Minimoonstar" by Ricardo Villalobos not found in library' about a
        CD sitting at VI 10/2.

        Boundary: ``test_a4_cached_track_promotion_wins_after_validation_miss``
        above pins A4 still winning when no surviving row's title answers the
        request. This test narrows A4, it does not disable it.
        """
        shelved = make_library_item(
            id=43063,
            artist="Ricardo Villalobos",
            title="Minimoonstar",
            call_letters="VI",
            artist_call_number=10,
            release_call_number=2,
        )
        rowless = _make_rowless_item(artist="Ricardo Villalobos", title="Vasco EP Part 1")
        with (
            patch(
                "lookup.validation.filter_results_by_track_validation",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "lookup.validation.find_library_albums_with_cached_track",
                new_callable=AsyncMock,
                return_value=([rowless], {ROWLESS_LIBRARY_ID: "resolved-1350337"}),
            ),
        ):
            result = await apply_track_validation_cascade(
                real_results=[shelved],
                library_results=[shelved],
                found_on_compilation=False,
                song_not_found=True,
                discogs_titles={},
                artist_fallback_results=[],
                song="Minimoonstar",
                artist="Ricardo Villalobos",
                match_artist="Ricardo Villalobos",
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        assert result.library_results == [shelved]
        assert result.song_not_found is False


# ---------------------------------------------------------------------------
# The LML#850-override row-less reverse probe.
#
# The A4 carry-through's own match-back (``search_album_fuzzy``) — and the
# LML#1318 typed-pair floor behind ``resolve_typed_album_level_match`` — both
# fail on the Broadcast shape by construction: WXYC shelves the split LP
# abbreviated "Broadcast & the Focus Group Investigate...", Discogs titles the
# release itself just "Investigate Witch Cults Of The Radio Age" (a 51-point
# token_sort_ratio, verified against the real Discogs API — no title floor
# bridges that). What DOES bridge it, verified against prod 2026-09-19: the
# shelf row already carries an LML#850 hand-verified override
# (``lml_cache.library_release_override``) pinning it to the exact release the
# track leg independently resolves. The reverse probe below asks that
# question directly — an id-equality check, no title similarity at all.
# ---------------------------------------------------------------------------

BROADCAST_ITEM = make_library_item(
    id=55651,
    artist="Broadcast",
    title="Broadcast & the Focus Group Investigate...",
    call_letters="BR",
    artist_call_number=120,
    release_call_number=9,
)
BROADCAST_RELEASE_ID = 1944554

# The control: a Broadcast shelf row whose title needs no reverse probe at
# all, since per-result validation confirms it directly.
BROADCAST_TENDER_BUTTONS_ITEM = make_library_item(
    id=55448,
    artist="Broadcast",
    title="Tender Buttons",
    call_letters="BR",
    artist_call_number=120,
    release_call_number=7,
)

# The Hiding Places / High Places guard: a same-artist row that must NOT be
# readmitted just because it shares the artist with a row-less High Places hit.
HIDING_PLACES_ITEM = make_library_item(
    id=8001, artist="Hiding Places Artist", title="Hiding Places"
)
HIGH_PLACES_RELEASE_ID = 1471882
HIDING_PLACES_OWN_RELEASE_ID = 6001234


@pytest.mark.asyncio
class TestRebindRowlessReleaseViaOverride:
    """Direct unit tests for ``_rebind_rowless_release_via_override``."""

    async def test_no_pg_returns_none_without_querying(self):
        result = await _rebind_rowless_release_via_override(
            None, release_id=BROADCAST_RELEASE_ID, shelf_rows=[BROADCAST_ITEM]
        )
        assert result is None

    async def test_no_shelf_rows_returns_none(self):
        pg = AsyncMock(spec=PgSource)
        result = await _rebind_rowless_release_via_override(
            pg, release_id=BROADCAST_RELEASE_ID, shelf_rows=[]
        )
        assert result is None
        pg.fetchall.assert_not_awaited()

    async def test_matching_override_returns_the_shelf_row(self):
        """The Broadcast shape: the row's own override matches the target release."""
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[{"library_id": 55651, "discogs_release_id": BROADCAST_RELEASE_ID}]
        )
        result = await _rebind_rowless_release_via_override(
            pg, release_id=BROADCAST_RELEASE_ID, shelf_rows=[BROADCAST_ITEM]
        )
        assert result == BROADCAST_ITEM

    async def test_override_pinned_to_a_different_release_is_rejected(self):
        """Hiding Places / High Places: the row's OWN override points elsewhere."""
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[{"library_id": 8001, "discogs_release_id": HIDING_PLACES_OWN_RELEASE_ID}]
        )
        result = await _rebind_rowless_release_via_override(
            pg, release_id=HIGH_PLACES_RELEASE_ID, shelf_rows=[HIDING_PLACES_ITEM]
        )
        assert result is None

    async def test_no_override_present_for_the_row_returns_none(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])
        result = await _rebind_rowless_release_via_override(
            pg, release_id=HIGH_PLACES_RELEASE_ID, shelf_rows=[HIDING_PLACES_ITEM]
        )
        assert result is None

    async def test_pg_failure_degrades_to_none_not_a_crash(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(side_effect=RuntimeError("pool exhausted"))
        result = await _rebind_rowless_release_via_override(
            pg, release_id=BROADCAST_RELEASE_ID, shelf_rows=[BROADCAST_ITEM]
        )
        assert result is None

    async def test_bounded_to_the_probe_limit(self):
        """Only the first 20 shelf rows are queried, in one call."""
        rows = [
            make_library_item(id=n, artist="Prolific Artist", title=f"Album {n}")
            for n in range(1, 26)
        ]
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])
        await _rebind_rowless_release_via_override(pg, release_id=999, shelf_rows=rows)
        assert pg.fetchall.await_count == 1
        queried_ids = pg.fetchall.await_args.args[1]
        assert queried_ids == list(range(1, 21))

    async def test_skips_nonpositive_ids_defensively(self):
        rowless_lookalike = make_library_item(id=0, artist="Broadcast", title="row-less")
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])
        result = await _rebind_rowless_release_via_override(
            pg, release_id=BROADCAST_RELEASE_ID, shelf_rows=[rowless_lookalike]
        )
        assert result is None
        pg.fetchall.assert_not_awaited()


@pytest.mark.asyncio
class TestCascadeRowLessReverseProbe:
    """``apply_track_validation_cascade``'s use of the LML#850 reverse probe."""

    async def _run(self, *, real_results, promoted_release_id, overrides, song="The Be Colony"):
        rowless = _make_rowless_item(
            artist="Broadcast", title="Investigate Witch Cults Of The Radio Age"
        )
        resolved = ResolvedRelease(
            release_id=promoted_release_id,
            release_url=f"https://www.discogs.com/release/{promoted_release_id}",
            is_compilation=False,
            album_title="Investigate Witch Cults Of The Radio Age",
            confidence=0.8,
            track_confirmed=True,
        )
        with (
            patch(
                "lookup.validation.filter_results_by_track_validation",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "lookup.validation.find_library_albums_with_cached_track",
                new_callable=AsyncMock,
                return_value=([rowless], {ROWLESS_LIBRARY_ID: resolved}),
            ),
            patch(
                "lookup.validation.get_library_release_overrides",
                new_callable=AsyncMock,
                return_value=overrides,
            ) as mock_overrides,
        ):
            result = await apply_track_validation_cascade(
                real_results=real_results,
                library_results=real_results,
                found_on_compilation=False,
                song_not_found=True,
                discogs_titles={},
                artist_fallback_results=[],
                song=song,
                artist="Broadcast",
                match_artist="Broadcast",
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
                pg=AsyncMock(spec=PgSource),
            )
        return result, mock_overrides

    async def test_broadcast_shaped_release_rebinds_to_the_overridden_shelf_row(self):
        """The flagship reproduction: an id-equality hit rebinds the row-less
        release to the shelf row instead of surfacing it as "(external)"."""
        result, _ = await self._run(
            real_results=[BROADCAST_ITEM],
            promoted_release_id=BROADCAST_RELEASE_ID,
            overrides={55651: BROADCAST_RELEASE_ID},
        )
        assert result.library_results == [BROADCAST_ITEM]
        assert result.song_not_found is False

    async def test_no_override_present_falls_through_to_rowless_as_before(self):
        """Degrades to the pre-existing row-less behaviour when nothing is pinned
        yet — the stopgap changes nothing for a row the override walk hasn't
        reached."""
        result, mock_overrides = await self._run(
            real_results=[BROADCAST_ITEM],
            promoted_release_id=BROADCAST_RELEASE_ID,
            overrides={},
        )
        mock_overrides.assert_awaited_once()
        assert result.library_results[0].id == ROWLESS_LIBRARY_ID
        assert result.song_not_found is False

    async def test_hiding_places_guard_rejects_a_same_artist_different_album(self):
        """Regression guard: a same-artist row whose OWN override points to a
        DIFFERENT release must not be readmitted for a High Places query."""
        result, _ = await self._run(
            real_results=[HIDING_PLACES_ITEM],
            promoted_release_id=HIGH_PLACES_RELEASE_ID,
            overrides={8001: HIDING_PLACES_OWN_RELEASE_ID},
            song="High Places",
        )
        assert result.library_results[0].id == ROWLESS_LIBRARY_ID
        assert result.library_results[0].id != HIDING_PLACES_ITEM.id

    async def test_clean_title_control_never_consults_overrides(self):
        """Control: a track that per-result validation confirms directly (the
        Broadcast "Tender Buttons" shape) short-circuits before the row-less
        tier — and therefore before the reverse probe — runs at all."""
        with (
            patch(
                "lookup.validation.filter_results_by_track_validation",
                new_callable=AsyncMock,
                return_value=[BROADCAST_TENDER_BUTTONS_ITEM],
            ),
            patch(
                "lookup.validation.find_library_albums_with_cached_track",
                new_callable=AsyncMock,
            ) as mock_cached_track,
            patch(
                "lookup.validation.get_library_release_overrides", new_callable=AsyncMock
            ) as mock_overrides,
        ):
            result = await apply_track_validation_cascade(
                real_results=[BROADCAST_TENDER_BUTTONS_ITEM],
                library_results=[BROADCAST_TENDER_BUTTONS_ITEM],
                found_on_compilation=False,
                song_not_found=True,
                discogs_titles={},
                artist_fallback_results=[],
                song="Tender Buttons",
                artist="Broadcast",
                match_artist="Broadcast",
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
                pg=AsyncMock(spec=PgSource),
            )

        assert result.library_results == [BROADCAST_TENDER_BUTTONS_ITEM]
        assert result.song_not_found is False
        mock_cached_track.assert_not_called()
        mock_overrides.assert_not_awaited()

    async def test_no_pg_degrades_to_rowless_without_crashing(self):
        """``pg=None`` (the cascade's default) must not attempt the probe at all —
        it degrades to exactly today's row-less behaviour."""
        rowless = _make_rowless_item(
            artist="Broadcast", title="Investigate Witch Cults Of The Radio Age"
        )
        resolved = ResolvedRelease(
            release_id=BROADCAST_RELEASE_ID,
            release_url=f"https://www.discogs.com/release/{BROADCAST_RELEASE_ID}",
            is_compilation=False,
            album_title="Investigate Witch Cults Of The Radio Age",
            confidence=0.8,
        )
        with (
            patch(
                "lookup.validation.filter_results_by_track_validation",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "lookup.validation.find_library_albums_with_cached_track",
                new_callable=AsyncMock,
                return_value=([rowless], {ROWLESS_LIBRARY_ID: resolved}),
            ),
        ):
            result = await apply_track_validation_cascade(
                real_results=[BROADCAST_ITEM],
                library_results=[BROADCAST_ITEM],
                found_on_compilation=False,
                song_not_found=True,
                discogs_titles={},
                artist_fallback_results=[],
                song="The Be Colony",
                artist="Broadcast",
                match_artist="Broadcast",
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        assert result.library_results[0].id == ROWLESS_LIBRARY_ID
