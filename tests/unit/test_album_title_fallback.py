"""Unit tests for the album-title text-search fallback in search_compilations_for_track.

See WXYC/library-metadata-lookup#319 (sibling of #318, closes #237).

When the two parallel Discogs probes in ``search_compilations_for_track``
return no usable releases AND the request supplies an album title AND the
resolver pre-pass did not produce a high-confidence canonical swap, the
orchestrator retries with a title-only compilation search and routes the
candidate releases through the existing post-process loop. The trio-style
case from #237 (``Orcutt Shelley Miller`` / ``Orcutt Shelley Miller`` /
``A Star Is Born``) needs the existing ``album == artist`` skip in
``process_release`` to NOT fire for this code path, since the trio
intentionally has matching artist and album strings.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from core.thresholds import CANONICAL_ARTIST_SIMILARITY_FLOOR
from discogs.models import ReleaseInfo, TrackReleasesResponse
from lookup.orchestrator import search_compilations_for_track
from services.parser import ParsedRequest
from tests.factories import make_library_item


def _empty_response() -> TrackReleasesResponse:
    return TrackReleasesResponse(track="", artist=None, releases=[], total=0)


def _trio_response() -> TrackReleasesResponse:
    """Album-title fallback returns the Orcutt/Shelley/Miller release."""
    return TrackReleasesResponse(
        track="",
        artist=None,
        releases=[
            ReleaseInfo(
                album="Orcutt Shelley Miller",
                artist="Bill Orcutt, Corsano, Miller",
                release_id=34993109,
                release_url="https://www.discogs.com/release/34993109",
                is_compilation=False,
            )
        ],
        total=1,
    )


class TestAlbumTitleFallback:
    @pytest.mark.asyncio
    async def test_fallback_fires_when_results_empty_album_present_no_swap(self):
        """All three guard conditions met → ``search_releases_by_album_title`` runs."""
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(return_value=_empty_response())
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Orcutt Shelley Miller",
            album="Orcutt Shelley Miller",
            song="A Star Is Born",
            raw_message="Orcutt Shelley Miller - A Star Is Born",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            await search_compilations_for_track(db, parsed, discogs_service=service)

        service.search_releases_by_album_title.assert_awaited_once()
        called_album = service.search_releases_by_album_title.await_args.args[0]
        assert called_album == "Orcutt Shelley Miller"

    @pytest.mark.asyncio
    async def test_fallback_skipped_when_album_missing(self):
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(return_value=_empty_response())

        parsed = ParsedRequest(
            artist="Orcutt Shelley Miller",
            song="A Star Is Born",
            raw_message="Orcutt Shelley Miller - A Star Is Born",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            await search_compilations_for_track(db, parsed, discogs_service=service)

        service.search_releases_by_album_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_skipped_when_resolver_swapped(self, monkeypatch):
        """If the resolver found a confident canonical, the existing probes
        already used it; the fallback adds no new information."""
        monkeypatch.setenv("LML_RESOLVE_ARTIST_CANONICAL", "true")
        from config.settings import get_settings

        get_settings.cache_clear()

        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "name": "Some Canonical Artist",
                    "score": CANONICAL_ARTIST_SIMILARITY_FLOOR + 0.10,
                }
            ]
        )
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(return_value=_empty_response())

        parsed = ParsedRequest(
            artist="Some Typoed Artist",
            album="An Album",
            song="A Song",
            raw_message="Some Typoed Artist - A Song",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            await search_compilations_for_track(db, parsed, discogs_service=service)

        service.search_releases_by_album_title.assert_not_called()
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_fallback_fires_after_initial_pass_filters_everything(self):
        """Staging regression from 2026-05-13 (#237 follow-up).

        Discogs has since added a canonical "Orcutt Shelley Miller" artist
        entity, so the artist-scoped probe in search_releases_by_track now
        DOES return the trio's 5 pressings. raw_releases is non-empty —
        which silenced the original `not raw_releases` fallback gate — and
        process_release rejects all 5 via the album==artist self-named
        guard (the library row's title 'Orcutt-Shelley-Miller' has an
        artist of 'Bill Orcutt', not the trio). End state: zero results.

        After the fix the gate fires on "no library results produced" so
        the fallback runs as a second pass with skip_self_named_album=False
        and skip_artist_match_filter=True, finds the library row, and
        validate_track_on_release confirms it.
        """
        # Library returns the trio album (matched by title — note the
        # hyphens in the actual library title) for any of these queries.
        library_item = make_library_item(
            id=72142,
            artist="Bill Orcutt",
            title="Orcutt-Shelley-Miller",
        )
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[library_item])

        # Five pressings of the trio release returned by both probes.
        # is_compilation=False (Discogs lists this under format=Vinyl) and
        # album == parsed.artist, which trips the self-named-album guard
        # in the default process_release path.
        trio_pressings_response = TrackReleasesResponse(
            track="A Star Is Born",
            artist="Orcutt Shelley Miller",
            releases=[
                ReleaseInfo(
                    album="Orcutt Shelley Miller",
                    artist="Orcutt Shelley Miller",
                    release_id=rid,
                    release_url=f"https://www.discogs.com/release/{rid}",
                    is_compilation=False,
                )
                for rid in (35017901, 35220253, 34993109, 34998866, 34993607)
            ],
            total=5,
        )

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=trio_pressings_response)
        service.search_releases_by_album_title = AsyncMock(return_value=_trio_response())
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Orcutt Shelley Miller",
            album="Orcutt Shelley Miller",
            song="A Star Is Born",
            raw_message="Orcutt Shelley Miller - A Star Is Born",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        service.search_releases_by_album_title.assert_awaited_once()
        assert any(r.id == 72142 for r in results), (
            "Expected library row 72142 to surface via the album-title fallback "
            "after the artist-scoped probes' candidates were all filtered out by "
            "process_release's default guards. Got: "
            f"{[(r.id, r.title) for r in results]}"
        )

    @pytest.mark.asyncio
    async def test_fallback_not_consumed_when_library_match_already_found(self):
        """The consume-gate fires on ``not results`` (no library matches were
        produced from the artist-scoped probes). Since #339 the album-title
        probe runs *speculatively* in the same `asyncio.gather` as the
        artist-scoped probes — paying its API cost up front to save the
        sequential-wait latency. The library match here is found by Wave A,
        so the fallback's *results* must not surface, even though its API
        call was made."""
        library_item = make_library_item(id=42, artist="A Real Artist", title="An Album")
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[library_item])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="A Song",
                artist="A Real Artist",
                releases=[
                    ReleaseInfo(
                        album="An Album",
                        artist="A Real Artist",
                        release_id=42,
                        release_url="https://www.discogs.com/release/42",
                    )
                ],
                total=1,
            )
        )
        service.search_releases_by_album_title = AsyncMock(return_value=_empty_response())
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="A Real Artist",
            album="An Album",
            song="A Song",
            raw_message="A Real Artist - A Song",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _ = await search_compilations_for_track(db, parsed, discogs_service=service)

        assert any(r.id == 42 for r in results)
        # The probe IS called speculatively (#339); the consume-gate, not the
        # fire-gate, is what prevents its (empty) result from surfacing.
        service.search_releases_by_album_title.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trio_release_surfaces_through_fallback_with_mismatching_library_artist(
        self,
    ):
        """The realistic trio case: the WXYC library row's artist string lists
        the trio members in catalog form (``"Orcutt, Bill / Shelley, Chris / Miller, Mette"``),
        which does NOT prefix-match the user-typed ``"Orcutt Shelley Miller"``.
        With ``skip_artist_match_filter=True`` on the fallback path, the library
        match should still reach ``validate_track_on_release`` (PR #236's fuzzy
        token-set-ratio validator), and the release should surface.
        """
        item = make_library_item(
            id=34993109,
            artist="Orcutt, Bill / Shelley, Chris / Miller, Mette",
            title="Orcutt Shelley Miller",
        )
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[item])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(return_value=_trio_response())
        # validate_track_on_release stands in for PR #236's fuzzy validator —
        # the realistic trio credit ('Bill Orcutt, Corsano, Miller') passes via
        # rapidfuzz.token_set_ratio against parsed.artist.
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Orcutt Shelley Miller",
            album="Orcutt Shelley Miller",
            song="A Star Is Born",
            raw_message="Orcutt Shelley Miller - A Star Is Born",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        assert any(r.id == 34993109 for r in results), (
            "Expected release 34993109 to surface via the fallback even when "
            "library artist string ('Orcutt, Bill / Shelley, Chris / Miller, "
            "Mette') does not prefix-match parsed.artist ('Orcutt Shelley Miller'). "
            "skip_artist_match_filter=True defers artist gating to "
            "validate_track_on_release."
        )
        service.validate_track_on_release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fallback_drops_wrong_artist_row_matched_only_by_album_title(self):
        """Regression: a wrong-*artist* library row matched purely on album-title
        fuzz must NOT surface through the fallback.

        Repro (Lone / "Galaxy Garden" / "Crystal Caverns 1991"): "Galaxy Garden"
        isn't in the WXYC library, so Wave A is empty and the album-title
        fallback fires. Its Discogs candidate is Lone's real "Galaxy Garden"
        release, but ``search_album_fuzzy(db, "Galaxy Garden")`` fuzz-matches the
        library row "Galaxy to Galaxy" by *Galaxy 2 Galaxy*
        (``album_title_acceptable`` → True, token_set_ratio 80). Because the
        fallback runs ``process_release(..., skip_artist_match_filter=True)`` and
        ``validate_track_on_release`` validates the (correct) Discogs release, the
        wrong-artist row surfaces — and, being a non-empty result, structurally
        preempts the ``_resolve_nonlibrary_release`` carry-through (gated on
        ``not results``) that would have resolved the correct row-less release.

        The artist ("Galaxy 2 Galaxy") shares essentially no fuzzy overlap with
        the request ("Lone") — token_set_ratio ~17, far below the reordered-
        collaborator cases the flag protects (65-70). The fallback must reject it.
        """
        wrong_artist_item = make_library_item(
            id=70229,
            artist="Galaxy 2 Galaxy",
            title="Galaxy to Galaxy",
        )
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[wrong_artist_item])

        # The album-title fallback probe returns Lone's actual "Galaxy Garden"
        # release; validate_track_on_release confirms the *release* (as in prod).
        lone_galaxy_garden = TrackReleasesResponse(
            track="Crystal Caverns 1991",
            artist="Lone",
            releases=[
                ReleaseInfo(
                    album="Galaxy Garden",
                    artist="Lone",
                    release_id=4030652,
                    release_url="https://www.discogs.com/release/4030652",
                    is_compilation=False,
                )
            ],
            total=1,
        )

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(return_value=lone_galaxy_garden)
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Lone",
            album="Galaxy Garden",
            song="Crystal Caverns 1991",
            raw_message="Lone - Crystal Caverns 1991",
        )

        with (
            patch(
                "lookup.orchestrator.lookup_releases_by_track",
                new_callable=AsyncMock,
                return_value=[],
            ),
            # Isolate the fallback's artist gating from the downstream
            # non-library carry-through (not under test here).
            patch(
                "lookup.orchestrator._resolve_nonlibrary_release",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        assert not any(r.id == 70229 for r in results), (
            "Wrong-artist library row 70229 ('Galaxy 2 Galaxy' / 'Galaxy to "
            "Galaxy') must not surface for request artist 'Lone' — it matched "
            "only on album-title fuzz, and its artist has ~zero overlap with the "
            f"request. Got: {[(r.id, r.title) for r in results]}"
        )

    @pytest.mark.asyncio
    async def test_fallback_keeps_various_artists_comp_row_despite_zero_artist_overlap(self):
        """A genuine Various-Artists compilation row must survive the fallback's
        lenient artist backstop even though its artist string ("Various Artists")
        shares no tokens with the typed solo artist.

        This is the case ``search_compilations_for_track`` exists to serve: the
        WXYC library files a compilation under "Various Artists", the user types
        the *track* artist ("Sun Ra"), and Wave A finds nothing. The album-title
        fallback fires and ``search_album_fuzzy`` surfaces the VA row on a title
        match. ``token_set_ratio("Sun Ra", "Various Artists") ≈ 29`` is far below
        ``_FALLBACK_ARTIST_SIMILARITY_FLOOR`` (40), so a naive floor would drop
        it — but the Discogs release is a compilation and the row is a
        compilation artist whose title matches, so the fallback must keep it
        (mirroring the strict branch's compilation carve-out). ``is_compilation_
        artist`` is False for the "Galaxy 2 Galaxy" collision, so this carve-out
        does not re-open the #717 wrong-artist bug.
        """
        va_comp_item = make_library_item(
            id=88123,
            artist="Various Artists",
            title="Freedom Jazz Dance",
        )
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[va_comp_item])

        comp_release = TrackReleasesResponse(
            track="Space Is the Place",
            artist=None,
            releases=[
                ReleaseInfo(
                    album="Freedom Jazz Dance",
                    artist="Various",
                    release_id=5551234,
                    release_url="https://www.discogs.com/release/5551234",
                    is_compilation=True,
                )
            ],
            total=1,
        )

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(return_value=comp_release)
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Sun Ra",
            album="Freedom Jazz Dance",
            song="Space Is the Place",
            raw_message="Sun Ra - Space Is the Place",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        assert any(r.id == 88123 for r in results), (
            "Expected the Various-Artists compilation row 88123 ('Various "
            "Artists' / 'Freedom Jazz Dance') to surface for track artist 'Sun "
            "Ra' via the album-title fallback. It is a legitimate compilation "
            "match (is_compilation_artist + title match), so the lenient artist "
            f"floor must not drop it. Got: {[(r.id, r.title) for r in results]}"
        )

    @pytest.mark.asyncio
    async def test_fallback_release_still_rejected_when_validate_track_returns_false(self):
        """Even with skip_artist_match_filter=True, validate_track_on_release
        is the last line of defense — if the Discogs-side fuzzy validator says
        the track or artist doesn't match, the release does NOT surface."""
        item = make_library_item(
            id=34993109,
            artist="Orcutt, Bill / Shelley, Chris / Miller, Mette",
            title="Orcutt Shelley Miller",
        )
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[item])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(return_value=_empty_response())
        service.search_releases_by_album_title = AsyncMock(return_value=_trio_response())
        service.validate_track_on_release = AsyncMock(return_value=False)

        parsed = ParsedRequest(
            artist="Orcutt Shelley Miller",
            album="Orcutt Shelley Miller",
            song="A Star Is Born",
            raw_message="Orcutt Shelley Miller - A Star Is Born",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        assert not any(r.id == 34993109 for r in results)


class TestAlbumTitleProbeConcurrency:
    """Acceptance check for WXYC/library-metadata-lookup#339 (A2).

    The album-title probe must fire in the same ``asyncio.gather`` as the
    two artist-scoped probes when its preconditions (``parsed.album`` set
    and resolver did not swap) are met — otherwise cold-cache wall time
    is ``A + B + C`` instead of ``max(A, B, C)``. The trade-off is one
    speculative API call when Wave A succeeds; that call warms LML's
    cache and is counted as cheap.
    """

    @pytest.mark.asyncio
    async def test_album_title_probe_runs_in_parallel_with_track_probes(self):
        """All three Discogs probes are in-flight concurrently."""
        active = {"count": 0, "peak": 0}

        def make_probe_mock(response: TrackReleasesResponse):
            async def _probe(*_args, **_kwargs):
                active["count"] += 1
                active["peak"] = max(active["peak"], active["count"])
                # Yield twice so the other gathered coroutines get scheduled
                # before this one returns. One ``sleep(0)`` is enough for the
                # second sibling to enter; the second ``sleep(0)`` covers the
                # third (the album-title probe).
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                active["count"] -= 1
                return response

            return _probe

        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(side_effect=make_probe_mock(_empty_response()))
        service.search_releases_by_album_title = AsyncMock(
            side_effect=make_probe_mock(_empty_response())
        )
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Obscure Artist",
            album="An Album",
            song="A Song",
            raw_message="Obscure Artist - A Song",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            await search_compilations_for_track(db, parsed, discogs_service=service)

        assert active["peak"] == 3, (
            "Expected all three probes (two artist-scoped + album-title fallback) "
            f"to be in-flight concurrently, but peak concurrency was {active['peak']}. "
            "If this is 2, the album-title probe is still sequential and the "
            "A2 optimization didn't land."
        )

    @pytest.mark.asyncio
    async def test_speculative_probe_error_logged_even_when_wave_a_succeeds(self):
        """New behavior introduced by #339: because the album-title probe now
        fires speculatively in the same `asyncio.gather` as the artist-scoped
        probes, a Discogs failure on the speculative probe is now observable
        even when Wave A succeeded and the consume-gate would have skipped it.

        Pre-#339 the fallback never ran in this scenario (the `not results`
        gate suppressed both fire and log), so no error was visible. Post-#339
        the probe DID run and DID raise, so the error path must call
        `_log_album_title_fallback(..., error=...)` to surface it.
        """
        library_item = make_library_item(id=42, artist="A Real Artist", title="An Album")
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[library_item])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        # Wave A succeeds — the library row id=42 is surfaced.
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="A Song",
                artist="A Real Artist",
                releases=[
                    ReleaseInfo(
                        album="An Album",
                        artist="A Real Artist",
                        release_id=42,
                        release_url="https://www.discogs.com/release/42",
                    )
                ],
                total=1,
            )
        )
        # The speculative album-title probe raises mid-gather.
        service.search_releases_by_album_title = AsyncMock(
            side_effect=RuntimeError("simulated Discogs failure")
        )
        service.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="A Real Artist",
            album="An Album",
            song="A Song",
            raw_message="A Real Artist - A Song",
        )

        with (
            patch(
                "lookup.orchestrator.lookup_releases_by_track",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("lookup.orchestrator._log_album_title_fallback") as mock_log,
        ):
            results, _ = await search_compilations_for_track(db, parsed, discogs_service=service)

        # Wave A's library row still surfaces (the speculative-probe failure
        # must not affect the artist-scoped path).
        assert any(r.id == 42 for r in results)
        # The error path fires `_log_album_title_fallback(..., error=...)`.
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args.kwargs
        assert call_kwargs["album"] == "An Album"
        assert call_kwargs["n_candidates"] == 0
        assert call_kwargs["surfaced_library_match"] is False
        assert "simulated Discogs failure" in call_kwargs["error"]

    @pytest.mark.asyncio
    async def test_album_title_probe_does_not_run_when_album_missing(self):
        """When ``parsed.album`` is None, peak concurrency is 2 — the album-title
        probe must not fire (no album to search by) and must not appear in the
        gather."""
        active = {"count": 0, "peak": 0}

        async def _probe(*_args, **_kwargs):
            active["count"] += 1
            active["peak"] = max(active["peak"], active["count"])
            await asyncio.sleep(0)
            active["count"] -= 1
            return _empty_response()

        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = AsyncMock()
        service.cache_service.search_artists_by_name = AsyncMock(return_value=[])
        service.search_releases_by_track = AsyncMock(side_effect=_probe)
        service.search_releases_by_album_title = AsyncMock(side_effect=_probe)

        parsed = ParsedRequest(
            artist="Obscure Artist",
            song="A Song",
            raw_message="Obscure Artist - A Song",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            await search_compilations_for_track(db, parsed, discogs_service=service)

        assert active["peak"] == 2, (
            "When parsed.album is missing, only the two artist-scoped probes "
            f"should fire (peak == 2), but observed peak {active['peak']}."
        )
        service.search_releases_by_album_title.assert_not_called()
