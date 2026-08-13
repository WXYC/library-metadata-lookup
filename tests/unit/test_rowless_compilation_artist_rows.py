"""LML#1184: a row-less compilation hit must not erase the artist's shelved rows.

When TRACK_ON_COMPILATION resolves the requested track to a compilation WXYC does not
shelve, the strategy surfaces it in the row-less shape (``LibraryItem(id=0)``,
LML#628 / LML#631) and ``_apply`` replaces the artist-only fallback rows with it,
stashing those rows in ``artist_fallback_results``. Step 3b is where that stash is
meant to be redeemed — but its gate keyed on ``library_results`` holding a real row,
which the row-less shape guarantees it does not, so the artist's own shelved albums
were dropped from the response entirely.

Reproduction (prod, 2026-08-13): ``Strange Life by Arabian Prince`` returned exactly
one row-less row (Chrome Children Vol. 2, which WXYC does not shelve) and dropped both
Arabian Prince albums the station does hold — library ids 99 and 54515.

Two layers are covered here: the cascade tier that performs the merge
(``apply_track_validation_cascade``) and the orchestrator gate that must let it run
(``_step_validate_tracks``).
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.search import SEARCH_TYPE_FALLBACK
from lookup.matching import MAX_SEARCH_RESULTS
from lookup.orchestrator import LookupState, _step_validate_tracks
from lookup.validation import apply_track_validation_cascade
from tests.factories import make_library_item

ROWLESS_COMP = "Chrome Children Vol. 2"
SONG = "Strange Life"
ARTIST = "Arabian Prince"


def _rowless_comp_item():
    """The synthetic id=0 carry-through for a compilation WXYC does not shelve."""
    return make_library_item(id=0, artist=ARTIST, title=ROWLESS_COMP)


def _shelved_artist_rows():
    """The two Arabian Prince releases WXYC actually holds."""
    return [
        make_library_item(id=99, artist=ARTIST, title="Brother Arab", call_letters="Ar"),
        make_library_item(
            id=54515,
            artist=ARTIST,
            title="Innovative Life: The Anthology 1984-1989",
            call_letters="Ar",
        ),
    ]


def _parsed_stub(song=SONG, artist=ARTIST):
    parsed = MagicMock()
    parsed.song = song
    parsed.artist = artist
    parsed.album = None
    return parsed


def _services_stub(**overrides):
    """Minimal LookupServices stand-in for the ``_step_*`` wiring tests."""
    services = MagicMock()
    services.telemetry.track_step = MagicMock(return_value=contextlib.nullcontext())
    services.db = object()
    services.discogs_service = object()
    services.allow_release_resolution_fallback = True
    for key, value in overrides.items():
        setattr(services, key, value)
    return services


@pytest.mark.asyncio
class TestCascadeRowlessCompilationTier:
    """``apply_track_validation_cascade``'s compilation tier, row-less variant."""

    async def test_unconfirmed_artist_rows_are_appended_not_dropped(self):
        """Validation confirms nothing: keep the row-less comp, append the shelved rows.

        Discogs cannot confirm the track on either Arabian Prince album (and with a
        misspelled song title it never will), but "we could not confirm the track on
        these" is not a reason to tell a caller WXYC holds nothing by an artist it
        demonstrably stocks.
        """
        rowless = _rowless_comp_item()
        shelved = _shelved_artist_rows()

        with patch(
            "lookup.validation.filter_results_by_track_validation",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await apply_track_validation_cascade(
                real_results=[],
                library_results=[rowless],
                found_on_compilation=True,
                song_not_found=False,
                discogs_titles={},
                artist_fallback_results=shelved,
                song=SONG,
                artist=ARTIST,
                match_artist=ARTIST,
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        assert result.library_results == [rowless, *shelved]
        # The shelf-facing reading of "Found <song> on:" is false once the only row
        # carrying the track is one nobody can pull, so the response adopts the
        # framing this same request already gets when Discogs finds nothing at all.
        assert result.song_not_found is True
        assert result.found_on_compilation is False

    async def test_confirmed_artist_row_still_prepends_ahead_of_the_rowless_comp(self):
        """A confirmed shelved album outranks the unshelved comp and keeps the comp framing."""
        rowless = _rowless_comp_item()
        shelved = _shelved_artist_rows()
        anthology = shelved[1]

        with patch(
            "lookup.validation.filter_results_by_track_validation",
            new_callable=AsyncMock,
            return_value=[anthology],
        ):
            result = await apply_track_validation_cascade(
                real_results=[],
                library_results=[rowless],
                found_on_compilation=True,
                song_not_found=False,
                discogs_titles={},
                artist_fallback_results=shelved,
                song=SONG,
                artist=ARTIST,
                match_artist=ARTIST,
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        assert result.library_results == [anthology, rowless]
        assert result.song_not_found is False
        # A shelved row carries the track, so the compilation framing stays honest.
        assert result.found_on_compilation is None

    async def test_appended_rows_respect_the_result_cap(self):
        """A prolific artist must not blow the response open.

        ``artist_fallback_results`` is un-truncated — ``search_library_with_fallback``
        fetches at ``_FETCH_LIMIT`` (10x ``MAX_SEARCH_RESULTS``) — and each surviving
        row costs a per-item artwork fetch, streaming probe and identity resolve in
        steps 3c/4/6.
        """
        rowless = _rowless_comp_item()
        stash = [
            make_library_item(id=1000 + n, artist=ARTIST, title=f"Album {n}") for n in range(40)
        ]

        with patch(
            "lookup.validation.filter_results_by_track_validation",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await apply_track_validation_cascade(
                real_results=[],
                library_results=[rowless],
                found_on_compilation=True,
                song_not_found=False,
                discogs_titles={},
                artist_fallback_results=stash,
                song=SONG,
                artist=ARTIST,
                match_artist=ARTIST,
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        assert len(result.library_results) == MAX_SEARCH_RESULTS
        # The comp keeps position 0 — top-1 artwork binds to the release that
        # actually carries the track.
        assert result.library_results[0] is rowless
        assert result.library_results[1:] == stash[: MAX_SEARCH_RESULTS - 1]

    async def test_rowless_lane_bounds_the_discogs_probe_input(self):
        """The new lane must not fan out a live probe over the whole un-truncated stash.

        This shape reached step 3b never at all before LML#1184, so every probe here
        is new load on a hot path — and the branch that consumes the verdict is by
        construction the "nothing confirmed" one, so the fan-out is paid on every
        firing. The pre-existing shelved-comp lane keeps the full list.
        """
        stash = [
            make_library_item(id=1000 + n, artist=ARTIST, title=f"Album {n}") for n in range(40)
        ]

        with patch(
            "lookup.validation.filter_results_by_track_validation",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_validate:
            await apply_track_validation_cascade(
                real_results=[],
                library_results=[_rowless_comp_item()],
                found_on_compilation=True,
                song_not_found=False,
                discogs_titles={},
                artist_fallback_results=stash,
                song=SONG,
                artist=ARTIST,
                match_artist=ARTIST,
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        assert mock_validate.await_args.args[0] == stash[:MAX_SEARCH_RESULTS]

    async def test_shelved_lane_still_validates_the_whole_stash(self):
        """The pre-existing lane's probe budget is unchanged — narrowing it here
        would be an unrelated behavior change riding along with a bug fix."""
        shelved_comp = make_library_item(id=50018, artist="Various Artists - Hiphop", title="Comp")
        stash = [
            make_library_item(id=1000 + n, artist=ARTIST, title=f"Album {n}") for n in range(40)
        ]

        with patch(
            "lookup.validation.filter_results_by_track_validation",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_validate:
            await apply_track_validation_cascade(
                real_results=[shelved_comp],
                library_results=[shelved_comp],
                found_on_compilation=True,
                song_not_found=False,
                discogs_titles={},
                artist_fallback_results=stash,
                song=SONG,
                artist=ARTIST,
                match_artist=ARTIST,
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        assert mock_validate.await_args.args[0] == stash

    async def test_shelved_compilation_with_unconfirmed_fallback_is_untouched(self):
        """Regression guard: the append is scoped to the row-less case.

        When the compilation itself is shelved the DJ can pull it, so an unconfirmed
        artist fallback stays suppressed exactly as before (LML#750 behavior).
        """
        shelved_comp = make_library_item(id=50018, artist="Various Artists - Hiphop", title="Comp")
        shelved = _shelved_artist_rows()

        with patch(
            "lookup.validation.filter_results_by_track_validation",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await apply_track_validation_cascade(
                real_results=[shelved_comp],
                library_results=[shelved_comp],
                found_on_compilation=True,
                song_not_found=False,
                discogs_titles={},
                artist_fallback_results=shelved,
                song=SONG,
                artist=ARTIST,
                match_artist=ARTIST,
                db=object(),
                discogs_service=object(),
                allow_release_resolution_fallback=True,
            )

        assert result.library_results == [shelved_comp]
        assert result.song_not_found is False
        assert result.found_on_compilation is None


@pytest.mark.asyncio
class TestStepValidateTracksGate:
    """``_step_validate_tracks``' gate — the veto that stranded the stash."""

    async def test_rowless_compilation_hit_still_runs_the_cascade(self):
        """``real_results`` is empty by construction here; the stash must still be redeemed."""
        rowless = _rowless_comp_item()
        shelved = _shelved_artist_rows()
        state = LookupState(
            library_results=[rowless],
            found_on_compilation=True,
            song_not_found=False,
            artist_fallback_results=shelved,
        )

        with patch(
            "lookup.orchestrator.apply_track_validation_cascade",
            new_callable=AsyncMock,
        ) as mock_cascade:
            mock_cascade.return_value = MagicMock(
                library_results=[rowless, *shelved],
                song_not_found=True,
                discogs_titles={},
                found_on_compilation=False,
            )
            await _step_validate_tracks(_parsed_stub(), state, _services_stub())

        mock_cascade.assert_awaited_once()
        assert mock_cascade.await_args.kwargs["artist_fallback_results"] == shelved
        assert state.library_results == [rowless, *shelved]
        assert state.song_not_found is True
        assert state.found_on_compilation is False
        # search_type must move with the verdict. It is what consumers actually
        # gate on — Backend-Service's `isTrustedLmlTrackContextMatch` keys on
        # `search_type in {direct, compilation}` and reads neither of the two
        # flags above — so leaving it at "compilation" would tell BS to trust,
        # as a confirmed track-context match, rows we just failed to confirm.
        assert state.search_type == SEARCH_TYPE_FALLBACK

    async def test_rowless_hit_without_a_stash_still_skips(self):
        """Nothing to redeem: no cascade, no Discogs probes for an empty candidate list."""
        state = LookupState(
            library_results=[_rowless_comp_item()],
            found_on_compilation=True,
            song_not_found=False,
            artist_fallback_results=[],
        )

        with patch(
            "lookup.orchestrator.apply_track_validation_cascade",
            new_callable=AsyncMock,
        ) as mock_cascade:
            await _step_validate_tracks(_parsed_stub(), state, _services_stub())

        mock_cascade.assert_not_awaited()

    async def test_cascade_leaving_found_on_compilation_unset_does_not_rebind_it(self):
        """``found_on_compilation=None`` means "leave alone" — the flag must survive."""
        shelved = _shelved_artist_rows()
        state = LookupState(
            library_results=shelved,
            found_on_compilation=True,
            song_not_found=False,
            artist_fallback_results=shelved,
            search_type="compilation",
        )

        with patch(
            "lookup.orchestrator.apply_track_validation_cascade",
            new_callable=AsyncMock,
        ) as mock_cascade:
            mock_cascade.return_value = MagicMock(
                library_results=shelved,
                song_not_found=False,
                discogs_titles={},
                found_on_compilation=None,
            )
            await _step_validate_tracks(_parsed_stub(), state, _services_stub())

        assert state.found_on_compilation is True
        # Not overturned, so search_type is left exactly as the pipeline stamped it.
        assert state.search_type == "compilation"
