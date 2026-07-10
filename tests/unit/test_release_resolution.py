"""Unit tests for ``lookup.release_resolution`` (compilation cross-reference).

The module owns "find and validate the Discogs release a track sits on" — the
probe set (Wave A/B ``search_releases_by_track`` + the conditional V/A-merge)
and per-candidate ``validate_track_on_release`` — and ranks validated releases
by title match to the requested album. It does NOT do library-row matching.

The spine case throughout: A Guy Called Gerald — "Message to Black Youth" on the
Various-Artists compilation "When There Is No Sun" (discogs release 36907527).
"""

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import ReleaseInfo, TrackReleasesResponse
from lookup.release_resolution import _log_track_validation


def _track_response(*releases: ReleaseInfo) -> TrackReleasesResponse:
    return TrackReleasesResponse(
        track="Message to Black Youth",
        artist="A Guy Called Gerald",
        releases=list(releases),
        total=len(releases),
    )


def _va_comp(
    release_id: int = 36907527,
    album: str = "When There Is No Sun",
) -> ReleaseInfo:
    return ReleaseInfo(
        album=album,
        artist="Various",
        release_id=release_id,
        release_url=f"https://www.discogs.com/release/{release_id}",
        is_compilation=True,
    )


class TestResolveReleaseForTrack:
    @pytest.mark.asyncio
    async def test_returns_validated_release_as_resolved_release(self):
        """A track that probes to a V/A comp and validates → one ResolvedRelease."""
        from lookup.release_resolution import ResolvedRelease, resolve_release_for_track

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(_va_comp()))
        svc.validate_track_on_release = AsyncMock(return_value=True)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
        )

        assert resolved == [
            ResolvedRelease(
                release_id=36907527,
                release_url="https://www.discogs.com/release/36907527",
                is_compilation=True,
                album_title="When There Is No Sun",
            )
        ]

    @pytest.mark.asyncio
    async def test_open_breaker_sheds_before_probing(self):
        """LML#755 FIX 1: when the saturation breaker is OPEN, the resolver
        raises ``DiscogsBreakerOpenError`` before probing rather than returning
        ``[]`` — so its L1 ``@async_cached`` wrapper never memoizes an empty
        (which would pin a 7-day miss sourced from a shed), and the row-less
        binder never writes a null-pin. All probes would shed anyway, so the
        early raise is also strictly cheaper."""
        from unittest.mock import MagicMock

        from discogs.breaker import BreakerState, DiscogsBreakerOpenError
        from lookup.release_resolution import resolve_release_for_track

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(_va_comp()))
        svc.validate_track_on_release = AsyncMock(return_value=True)

        open_breaker = MagicMock()
        open_breaker.state = BreakerState.OPEN

        with patch("lookup.release_resolution.get_discogs_breaker", return_value=open_breaker):
            with pytest.raises(DiscogsBreakerOpenError):
                await resolve_release_for_track(
                    song="Message to Black Youth",
                    artist="A Guy Called Gerald",
                    album="When There Is No Sun",
                    discogs_service=svc,
                )

        # It shed before probing.
        svc.search_releases_by_track.assert_not_called()

    @pytest.mark.asyncio
    async def test_drops_releases_that_fail_track_credit_validation(self):
        """A probed release whose track-credit doesn't validate is excluded."""
        from lookup.release_resolution import resolve_release_for_track

        wrong = _va_comp(release_id=111, album="Some Other Comp")
        right = _va_comp(release_id=36907527, album="When There Is No Sun")
        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(wrong, right))
        # Only the right release validates the per-track credit.
        svc.validate_track_on_release = AsyncMock(
            side_effect=lambda rid, song, artist: rid == 36907527
        )

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
        )

        assert [r.release_id for r in resolved] == [36907527]

    @pytest.mark.asyncio
    async def test_skips_candidate_with_falsy_release_id(self):
        """A probed release with release_id=0 (malformed Discogs row) is skipped
        before validation — never validated, never resolved."""
        from lookup.release_resolution import resolve_release_for_track

        zero = ReleaseInfo(
            album="When There Is No Sun",
            artist="Various",
            release_id=0,
            release_url="",
            is_compilation=True,
        )
        good = _va_comp(release_id=36907527, album="When There Is No Sun")
        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(zero, good))
        svc.validate_track_on_release = AsyncMock(return_value=True)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
        )

        assert [r.release_id for r in resolved] == [36907527]
        # The zero-id row was skipped before the validate probe ran.
        assert svc.validate_track_on_release.await_count == 1

    @pytest.mark.asyncio
    async def test_wave_b_keyword_probe_surfaces_va_comp_when_wave_a_has_none(self):
        """End-to-end wave routing: Wave A (artist-field probe) returns a non-V/A
        release; only Wave B (artist_as_keyword=True) surfaces the V/A comp, which
        the merge pulls in and validates. Catches a wave-swap or dropped-keyword-probe
        wiring bug that the isolated merge unit test cannot see."""
        from lookup.release_resolution import resolve_release_for_track

        def _route(track, artist, artist_as_keyword=False):
            if artist_as_keyword:
                return _track_response(_va_comp(release_id=36907527, album="When There Is No Sun"))
            return _track_response(_single_artist(1, "An Unrelated Solo Album"))

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(side_effect=_route)
        # Only the V/A comp validates the per-track credit.
        svc.validate_track_on_release = AsyncMock(
            side_effect=lambda rid, song, artist: rid == 36907527
        )

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
        )

        assert [r.release_id for r in resolved] == [36907527]

    @pytest.mark.asyncio
    async def test_album_title_probe_failure_does_not_drop_track_candidates(self):
        """A transient failure of the album-title probe must NOT discard the
        validated track-wave candidates. The album-title probe is isolated (like
        the strategy's ``_album_title_probe_safe``), so only the track waves
        gate the empty-result path."""
        from lookup.release_resolution import resolve_release_for_track

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(_va_comp()))
        svc.search_releases_by_album_title = AsyncMock(side_effect=RuntimeError("discogs 503"))
        svc.validate_track_on_release = AsyncMock(return_value=True)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
            also_probe_album_title=True,
        )

        assert [r.release_id for r in resolved] == [36907527]

    @pytest.mark.asyncio
    async def test_album_wave_distinct_release_survives_track_wave_twin_validation_failure(self):
        """The album-title wave's candidates are deduped by release_id, not title:
        a track-wave release that FAILS validation must not suppress a DISTINCT
        same-titled album-wave release that WOULD validate."""
        from lookup.release_resolution import resolve_release_for_track

        track_twin = _va_comp(release_id=111, album="When There Is No Sun")  # fails validation
        album_twin = _va_comp(release_id=222, album="When There Is No Sun")  # validates

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(track_twin))
        svc.search_releases_by_album_title = AsyncMock(return_value=_track_response(album_twin))
        svc.validate_track_on_release = AsyncMock(side_effect=lambda rid, song, artist: rid == 222)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
            also_probe_album_title=True,
        )

        assert [r.release_id for r in resolved] == [222]


def _single_artist(release_id: int, album: str, *, is_compilation: bool = False) -> ReleaseInfo:
    return ReleaseInfo(
        album=album,
        artist="A Guy Called Gerald",
        release_id=release_id,
        release_url=f"https://www.discogs.com/release/{release_id}",
        is_compilation=is_compilation,
    )


class TestMergeWaveBCompilations:
    """The WXYC#527 V/A-merge, extracted so the comp strategy can delegate to it."""

    def test_merges_wave_b_va_comp_when_wave_a_has_no_va_hit(self):
        from lookup.release_resolution import merge_wave_b_compilations

        wave_a = [_single_artist(1, "A Guy Called Gerald Album")]
        wave_b = [_va_comp(release_id=2, album="When There Is No Sun")]

        merged = merge_wave_b_compilations(wave_a, wave_b)

        assert [r.release_id for r in merged] == [1, 2]

    def test_does_not_merge_wave_b_when_wave_a_already_has_va_comp(self):
        from lookup.release_resolution import merge_wave_b_compilations

        wave_a = [_va_comp(release_id=1, album="Wave A Comp")]
        wave_b = [_va_comp(release_id=2, album="Wave B Comp")]

        merged = merge_wave_b_compilations(wave_a, wave_b)

        assert [r.release_id for r in merged] == [1]

    def test_skips_wave_b_non_compilation_rows(self):
        from lookup.release_resolution import merge_wave_b_compilations

        wave_a = [_single_artist(1, "Solo Album")]
        # Per-row Wave B inclusion gates on ``is_compilation`` only (faithful to
        # the original): a non-compilation row is skipped, a Compilation-flagged
        # row is merged even when its artist is single-artist.
        wave_b = [
            _single_artist(2, "Plain Single"),
            _single_artist(3, "Retrospective", is_compilation=True),
        ]

        merged = merge_wave_b_compilations(wave_a, wave_b)

        assert [r.release_id for r in merged] == [1, 3]

    def test_dedups_wave_b_against_wave_a_by_album_title(self):
        from lookup.release_resolution import merge_wave_b_compilations

        wave_a = [_single_artist(1, "Shared Title")]
        wave_b = [_va_comp(release_id=2, album="Shared Title")]

        merged = merge_wave_b_compilations(wave_a, wave_b)

        assert [r.release_id for r in merged] == [1]


class TestRanking:
    @pytest.mark.asyncio
    async def test_prefers_title_matched_release_over_lower_id_mismatch(self):
        """The divergent-pressing fix: rank by title match, not Discogs's top-1.

        A different-titled release with a *lower* release_id (the kind of thing
        Discogs's keyword re-search surfaces at the top) must not outrank the
        release whose title matches the requested album — release_id is only the
        stable tie-break.
        """
        from lookup.release_resolution import resolve_release_for_track

        mismatch = _va_comp(release_id=10, album="Acid House Classics")
        original = _va_comp(release_id=99, album="When There Is No Sun")
        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(mismatch, original))
        svc.validate_track_on_release = AsyncMock(return_value=True)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
        )

        assert [r.release_id for r in resolved] == [99, 10]

    @pytest.mark.asyncio
    async def test_equal_title_match_resolves_by_stable_release_id(self):
        """Two pressings whose titles normalize equally (original + deluxe
        reissue) resolve by stable release_id — deterministic across runs."""
        from lookup.release_resolution import resolve_release_for_track

        reissue = _va_comp(release_id=10, album="When There Is No Sun (Deluxe Reissue Edition)")
        original = _va_comp(release_id=99, album="When There Is No Sun")
        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(reissue, original))
        svc.validate_track_on_release = AsyncMock(return_value=True)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
        )

        assert [r.release_id for r in resolved] == [10, 99]

    @pytest.mark.asyncio
    async def test_stable_release_id_tie_break_when_no_album(self):
        """With no album to rank against, ordering is stable by release_id."""
        from lookup.release_resolution import resolve_release_for_track

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(
            return_value=_track_response(
                _va_comp(release_id=50, album="Comp B"),
                _va_comp(release_id=20, album="Comp A"),
            )
        )
        svc.validate_track_on_release = AsyncMock(return_value=True)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album=None,
            discogs_service=svc,
        )

        assert [r.release_id for r in resolved] == [20, 50]


def _eight_candidates() -> list[ReleaseInfo]:
    """Eight V/A comps whose titles deliberately do NOT pre-rank in input order.

    Input order is by ``release_id`` (10..80); the exact title match
    "When There Is No Sun" sits at id 80 (last in input) so a test that asserts
    "the title-top validates first" genuinely proves the pre-rank moved it to the
    front, not that input order happened to coincide.
    """
    return [
        _va_comp(release_id=10, album="Acid House Classics"),
        _va_comp(release_id=20, album="Some Other Comp"),
        _va_comp(release_id=30, album="Totally Different"),
        _va_comp(release_id=40, album="Comp A"),
        _va_comp(release_id=50, album="Comp B"),
        _va_comp(release_id=60, album="An Unrelated Solo Album"),
        _va_comp(release_id=70, album="Yet Another Comp"),
        _va_comp(release_id=80, album="When There Is No Sun"),  # exact title match
    ]


class TestBoundedEarlyExit:
    """Opt-in bounded sequential-early-exit (LML#633).

    ``max_validations`` defaults to ``None`` (today's validate-all, rank-after —
    pins the #604 lazy-bind callers byte-for-byte). When set to an int the
    resolver pre-ranks candidates by title score, validates in rank order,
    returns the first that validates, and stops after that many attempts (the
    consumer #628 passes 5).
    """

    @pytest.mark.asyncio
    async def test_title_top_validates_first_single_call(self):
        """8 candidates, the title-top-ranked one validates → exactly 1 validate
        call, and that release is returned."""
        from lookup.release_resolution import resolve_release_for_track

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(*_eight_candidates()))
        svc.validate_track_on_release = AsyncMock(return_value=True)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
            max_validations=5,
        )

        assert [r.release_id for r in resolved] == [80]
        assert svc.validate_track_on_release.await_count == 1
        # The single validate probe was the title-top (id 80), not input-order #1.
        assert svc.validate_track_on_release.await_args.args[0] == 80

    @pytest.mark.asyncio
    async def test_title_top_fails_second_validates_two_calls(self):
        """Title-top fails validation, the #2-ranked validates → exactly 2 calls,
        returns the #2-ranked release."""
        from lookup.release_resolution import resolve_release_for_track

        # Pre-rank: id 80 (exact, 100) > id 20 ("Some Other Comp", 45.7) > rest.
        # 80 fails the per-track credit; 20 passes.
        candidates = _eight_candidates()
        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(*candidates))
        svc.validate_track_on_release = AsyncMock(side_effect=lambda rid, song, artist: rid == 20)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
            max_validations=5,
        )

        assert [r.release_id for r in resolved] == [20]
        assert svc.validate_track_on_release.await_count == 2
        # Probed in rank order: title-top (80) first, then the runner-up (20).
        assert [c.args[0] for c in svc.validate_track_on_release.await_args_list] == [80, 20]

    @pytest.mark.asyncio
    async def test_none_validate_stops_at_backstop_cap(self):
        """No candidate validates → stops after exactly 5 attempts (the N=5
        backstop), returns empty — even though 8 candidates were available."""
        from lookup.release_resolution import resolve_release_for_track

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(*_eight_candidates()))
        svc.validate_track_on_release = AsyncMock(return_value=False)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
            max_validations=5,
        )

        assert resolved == []
        assert svc.validate_track_on_release.await_count == 5

    @pytest.mark.asyncio
    async def test_default_mode_validates_all_candidates(self):
        """Default mode (no ``max_validations`` arg) validates ALL 8 candidates —
        pins the #604 lazy-bind callers unchanged (validate-all, rank-after)."""
        from lookup.release_resolution import resolve_release_for_track

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(*_eight_candidates()))
        svc.validate_track_on_release = AsyncMock(return_value=False)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
        )

        assert resolved == []
        assert svc.validate_track_on_release.await_count == 8

    @pytest.mark.asyncio
    async def test_no_album_orders_by_own_release_then_id_early_exits(self):
        """No-album case: nothing to title-rank, so order by prefer
        ``is_compilation=False`` (own release) then stable ``release_id``,
        early-exit on first validation, capped at 5. The own-release with the
        lowest id is probed first and, validating, ends the sweep at 1 call."""
        from lookup.release_resolution import resolve_release_for_track

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(
            return_value=_track_response(
                _va_comp(release_id=20, album="A V/A Comp"),  # is_compilation=True
                _single_artist(50, "Own Album B"),  # is_compilation=False
                _single_artist(40, "Own Album A"),  # is_compilation=False, lower id
            )
        )
        svc.validate_track_on_release = AsyncMock(return_value=True)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album=None,
            discogs_service=svc,
            max_validations=5,
        )

        # Own releases first (40 before 50 by id), comp last → 40 probed first,
        # validates, early-exit.
        assert [r.release_id for r in resolved] == [40]
        assert svc.validate_track_on_release.await_count == 1
        assert svc.validate_track_on_release.await_args.args[0] == 40

    @pytest.mark.asyncio
    async def test_no_album_single_artist_retrospective_outranks_remix_single(self):
        """A single-artist retrospective — Discogs flags it ``Compilation`` but it
        is credited to a real artist, not "Various" — is the canonical home of an
        artist's original track, so it must rank as an own-release. It must NOT be
        deprioritized below a later non-compilation single that only carries a
        remix of the same track.

        Regression for the "Me & Mr Jones by Plug" case: the original lives on the
        retrospective "Drum 'n' Bass for Papa" (lower id, ``is_compilation=True``),
        while the remix lives on a plain single (higher id, ``is_compilation=
        False``). The old ``bool(is_compilation)`` prerank sorted the remix single
        first and surfaced it; the true-V/A gate ranks the retrospective first.
        """
        from lookup.release_resolution import resolve_release_for_track

        retrospective = _single_artist(30, "Drum 'n' Bass For Papa", is_compilation=True)
        remix_single = _single_artist(70, "Me & Mr. Sutton", is_compilation=False)
        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(
            return_value=_track_response(remix_single, retrospective)
        )
        svc.validate_track_on_release = AsyncMock(return_value=True)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album=None,
            discogs_service=svc,
            max_validations=5,
        )

        # The retrospective (id 30) ranks as an own-release and, being the lower
        # id, is probed first and validates → early-exit on it, not the remix.
        assert [r.release_id for r in resolved] == [30]
        assert svc.validate_track_on_release.await_args.args[0] == 30


class TestValidateReleaseForTrack:
    """The shared validate+telemetry primitive both strategies delegate to."""

    @pytest.mark.asyncio
    async def test_returns_verdict_and_records_telemetry(self, monkeypatch):
        from lookup import release_resolution as rr

        logged: list[dict] = []
        monkeypatch.setattr(rr, "_log_track_validation", lambda **kw: logged.append(kw))

        svc = AsyncMock()
        svc.validate_track_on_release = AsyncMock(return_value=True)

        verdict = await rr.validate_release_for_track(
            svc,
            36907527,
            "Message to Black Youth",
            "A Guy Called Gerald",
            source="unit",
        )

        assert verdict is True
        svc.validate_track_on_release.assert_awaited_once_with(
            36907527, "Message to Black Youth", "A Guy Called Gerald"
        )
        assert logged and logged[0]["source"] == "unit"
        assert logged[0]["release_id"] == 36907527
        assert logged[0]["verdict"] is True
        assert "latency_ms" in logged[0]


class TestEmptyCases:
    @pytest.mark.asyncio
    async def test_returns_empty_without_discogs_service(self):
        from lookup.release_resolution import resolve_release_for_track

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=None,
        )

        assert resolved == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_nothing_validates(self):
        from lookup.release_resolution import resolve_release_for_track

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(_va_comp()))
        svc.validate_track_on_release = AsyncMock(return_value=False)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
        )

        assert resolved == []

    @pytest.mark.asyncio
    async def test_returns_empty_for_blank_song_or_artist_without_probing(self):
        """The song/artist guard short-circuits before any Discogs probe."""
        from lookup.release_resolution import resolve_release_for_track

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock()

        assert await resolve_release_for_track("", "A Guy Called Gerald", "X", svc) == []
        assert await resolve_release_for_track("Message to Black Youth", "", "X", svc) == []

        svc.search_releases_by_track.assert_not_awaited()


class TestErrorHandling:
    """Release resolution degrades gracefully on Discogs failures (matches the
    resilience of the search_compilations_for_track path it was lifted from)."""

    @pytest.mark.asyncio
    async def test_probe_failure_returns_empty(self):
        from lookup.release_resolution import resolve_release_for_track

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(side_effect=RuntimeError("discogs 503"))
        svc.validate_track_on_release = AsyncMock(return_value=True)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
        )

        assert resolved == []

    @pytest.mark.asyncio
    async def test_breaker_shed_is_not_memoized_by_the_l1_wrapper(self):
        """LML#755 FIX 1: a shed must not be laundered into a memoized ``[]`` by
        ``resolve_release_for_track_cached``'s ``@async_cached``. Because the
        uncached resolver RAISES on an open breaker (not returns ``[]``), the L1
        decorator never writes an entry — a later call with a healthy breaker
        re-probes and resolves normally instead of serving a stale empty."""
        from unittest.mock import MagicMock

        from discogs.breaker import BreakerState, DiscogsBreakerOpenError
        from discogs.memory_cache import get_release_resolution_cache
        from lookup.release_resolution import resolve_release_for_track_cached

        get_release_resolution_cache().clear()

        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(_va_comp()))
        svc.validate_track_on_release = AsyncMock(return_value=True)

        open_breaker = MagicMock(state=BreakerState.OPEN)
        closed_breaker = MagicMock(state=BreakerState.CLOSED)

        # First call: breaker OPEN → shed → raises, nothing memoized.
        with patch("lookup.release_resolution.get_discogs_breaker", return_value=open_breaker):
            with pytest.raises(DiscogsBreakerOpenError):
                await resolve_release_for_track_cached(
                    svc, "Message to Black Youth", "A Guy Called Gerald", "When There Is No Sun"
                )

        # Second call with the breaker CLOSED must re-probe (no memoized empty).
        with patch("lookup.release_resolution.get_discogs_breaker", return_value=closed_breaker):
            resolved = await resolve_release_for_track_cached(
                svc, "Message to Black Youth", "A Guy Called Gerald", "When There Is No Sun"
            )

        assert [r.release_id for r in resolved] == [36907527]
        svc.search_releases_by_track.assert_awaited()

    @pytest.mark.asyncio
    async def test_single_validation_failure_preserves_other_releases(self):
        """One candidate's validation raising must not discard already-validated
        releases — the divergence the lift could otherwise introduce."""
        from lookup.release_resolution import resolve_release_for_track

        good = _va_comp(release_id=36907527, album="When There Is No Sun")
        boom = _va_comp(release_id=111, album="When There Is No Sun")
        svc = AsyncMock()
        svc.search_releases_by_track = AsyncMock(return_value=_track_response(good, boom))

        async def _validate(rid, song, artist):
            if rid == 111:
                raise RuntimeError("discogs timeout")
            return True

        svc.validate_track_on_release = AsyncMock(side_effect=_validate)

        resolved = await resolve_release_for_track(
            song="Message to Black Youth",
            artist="A Guy Called Gerald",
            album="When There Is No Sun",
            discogs_service=svc,
        )

        assert [r.release_id for r in resolved] == [36907527]


# ---------------------------------------------------------------------------
# Tests: _log_track_validation (A7 / LML#344 audit instrumentation)
# Relocated from test_orchestrator_helpers.py when the helper moved into this
# leaf module; patch targets point at lookup.release_resolution accordingly.
# ---------------------------------------------------------------------------


class TestLogTrackValidation:
    """Audit instrumentation for the two validate_track_on_release call sites.

    LML#344 wants to know how often the step-3b validation runs after
    TRACK_ON_COMPILATION's inline validation already vetted the same release,
    and how often the two verdicts diverge. The helper records each
    validation call with a source label so downstream Sentry analysis can
    split (release_id, verdict) pairs by source.

    Two surfaces:
      - Sentry breadcrumb every call (always-on; trace explorer queries against it).
      - Structured INFO log on a 1% sample (cheap Railway-log grep target).

    Sample rate is parameterizable so tests can force the deterministic branch.
    """

    def test_records_breadcrumb_on_every_call(self):
        """Every call must produce a Sentry breadcrumb (always-on for trace explorer)."""
        with patch("lookup.release_resolution.sentry_sdk.add_breadcrumb") as mock_breadcrumb:
            _log_track_validation(
                source="compilation_inline",
                release_id=12345,
                song="VI Scose Poise",
                artist="Autechre",
                verdict=True,
                latency_ms=42.5,
                sample_rate=0.0,  # disable log sampling; only breadcrumb path
            )
        mock_breadcrumb.assert_called_once()
        call = mock_breadcrumb.call_args
        assert call.kwargs["category"] == "track_validation"
        data = call.kwargs["data"]
        assert data["source"] == "compilation_inline"
        assert data["release_id"] == 12345
        assert data["song"] == "VI Scose Poise"
        assert data["artist"] == "Autechre"
        assert data["verdict"] is True
        assert data["latency_ms"] == 42.5

    def test_samples_info_log_at_configured_rate(self):
        """When the random sample fires, an INFO log is emitted alongside the breadcrumb."""
        with (
            patch("lookup.release_resolution.random.random", return_value=0.005),  # 0.5% < 1%
            patch("lookup.release_resolution.logger") as mock_logger,
            patch("lookup.release_resolution.sentry_sdk.add_breadcrumb"),
        ):
            _log_track_validation(
                source="step_3b",
                release_id=1,
                song="x",
                artist="y",
                verdict=False,
                latency_ms=1.0,
                sample_rate=0.01,
            )
        # logger.info called with the structured payload
        info_calls = [c for c in mock_logger.info.call_args_list if "track_validation" in str(c)]
        assert len(info_calls) == 1

    def test_does_not_sample_info_log_when_rate_misses(self):
        """When the random draw exceeds the sample rate, no INFO log fires.

        Even at 1% sampling we still need the *deterministic* off-path so
        Railway logs aren't flooded by every /lookup. The breadcrumb path
        runs unconditionally; only the structured log is gated.
        """
        with (
            patch("lookup.release_resolution.random.random", return_value=0.99),  # 99% > 1%
            patch("lookup.release_resolution.logger") as mock_logger,
            patch("lookup.release_resolution.sentry_sdk.add_breadcrumb"),
        ):
            _log_track_validation(
                source="step_3b",
                release_id=1,
                song="x",
                artist="y",
                verdict=False,
                latency_ms=1.0,
                sample_rate=0.01,
            )
        info_calls = [c for c in mock_logger.info.call_args_list if "track_validation" in str(c)]
        assert len(info_calls) == 0

    def test_handles_sentry_sdk_failure(self):
        """A Sentry SDK exception must not break /lookup.

        Mirrors the swallow-and-log pattern in _log_album_title_fallback /
        _log_resolver_pre_pass / _log_search_budget_exceeded. The audit
        layer is non-essential — it cannot be allowed to take down the
        request path.
        """
        with patch(
            "lookup.release_resolution.sentry_sdk.add_breadcrumb",
            side_effect=RuntimeError("sentry exploded"),
        ):
            # Must not raise.
            _log_track_validation(
                source="compilation_inline",
                release_id=1,
                song="x",
                artist="y",
                verdict=True,
                latency_ms=1.0,
                sample_rate=0.0,
            )

    def test_handles_null_artist(self):
        """Some call paths pass artist=None; the helper must accept it."""
        with patch("lookup.release_resolution.sentry_sdk.add_breadcrumb") as mock_breadcrumb:
            _log_track_validation(
                source="step_3b",
                release_id=1,
                song="x",
                artist=None,
                verdict=False,
                latency_ms=1.0,
                sample_rate=0.0,
            )
        data = mock_breadcrumb.call_args.kwargs["data"]
        assert data["artist"] is None
