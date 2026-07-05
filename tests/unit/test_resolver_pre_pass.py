"""Unit tests for the canonical-artist resolver pre-pass.

Sibling of #237. See WXYC/library-metadata-lookup#318.

The resolver pre-pass runs before ``search_compilations_for_track`` issues
its two parallel Discogs probes, looking up the inbound artist string in
the discogs-cache ``artist`` + ``artist_name_variation`` tables via
``cache_service.search_artists_by_name``. When the top trigram match
scores at or above ``CANONICAL_ARTIST_SIMILARITY_FLOOR``, the canonical
``artist.name`` is swapped in; otherwise the original input flows through.

Behavior under test:

- ``resolve_canonical_artist`` returns a ``ResolverOutcome`` with the
  original input, the (possibly swapped) canonical name, the top score,
  and a ``swapped`` flag.
- The pre-pass is a no-op when the cache is unavailable, the artist
  string is empty/whitespace, or the cache returns no candidates.
- Floor decisions are expressed relative to the
  ``CANONICAL_ARTIST_SIMILARITY_FLOOR`` constant; boundary cases at
  ``floor + 0.10`` (swap), ``floor - 0.10`` (no swap), and ``0.99``
  (short-circuit) are exercised.
- Identical input (canonical name equals input modulo case + diacritics)
  short-circuits without burning a swap log entry.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.thresholds import CANONICAL_ARTIST_SIMILARITY_FLOOR
from discogs.models import ReleaseInfo, TrackReleasesResponse
from lookup.artist_resolution import ResolverOutcome, resolve_canonical_artist
from lookup.orchestrator import search_compilations_for_track
from services.parser import ParsedRequest
from tests.factories import make_library_item


@pytest.fixture
def cache():
    """AsyncMock standing in for DiscogsCacheService."""
    c = AsyncMock()
    c.search_artists_by_name = AsyncMock(return_value=[])
    return c


class TestResolveCanonicalArtist:
    @pytest.mark.asyncio
    async def test_returns_canonical_when_score_above_floor(self, cache):
        """Score above floor → ``swapped=True`` and canonical name returned."""
        score = CANONICAL_ARTIST_SIMILARITY_FLOOR + 0.10
        cache.search_artists_by_name.return_value = [
            {"id": 1, "name": "Alexander Robotnick", "score": score},
        ]

        outcome = await resolve_canonical_artist("Alexander Robotnik", cache_service=cache)

        assert isinstance(outcome, ResolverOutcome)
        assert outcome.original == "Alexander Robotnik"
        assert outcome.canonical == "Alexander Robotnick"
        assert outcome.score == score
        assert outcome.swapped is True

    @pytest.mark.asyncio
    async def test_returns_original_when_score_below_floor(self, cache):
        """Score below floor → ``swapped=False`` and original returned."""
        score = CANONICAL_ARTIST_SIMILARITY_FLOOR - 0.10
        cache.search_artists_by_name.return_value = [
            {"id": 1, "name": "Aleksandr Robotnyk", "score": score},
        ]

        outcome = await resolve_canonical_artist("Alexander Robotnik", cache_service=cache)

        assert outcome.original == "Alexander Robotnik"
        assert outcome.canonical == "Alexander Robotnik"  # unchanged
        assert outcome.score == score
        assert outcome.swapped is False

    @pytest.mark.asyncio
    async def test_high_score_exact_match_yields_swap_to_canonical_form(self, cache):
        """When the cache's top hit equals the input modulo case/diacritics but
        scores at/above the floor, the outcome records the canonical-cased form
        and ``swapped=True``. Downstream code paths use ``canonical`` directly,
        so case fixups (e.g. ``stereolab`` → ``Stereolab``) flow through."""
        cache.search_artists_by_name.return_value = [
            {"id": 1, "name": "Stereolab", "score": 0.99},
        ]

        outcome = await resolve_canonical_artist("stereolab", cache_service=cache)

        assert outcome.canonical == "Stereolab"
        assert outcome.swapped is True
        assert outcome.score == 0.99

    @pytest.mark.asyncio
    async def test_returns_original_when_cache_returns_nothing(self, cache):
        cache.search_artists_by_name.return_value = []

        outcome = await resolve_canonical_artist("Nonexistent Artist", cache_service=cache)

        assert outcome.original == "Nonexistent Artist"
        assert outcome.canonical == "Nonexistent Artist"
        assert outcome.score == 0.0
        assert outcome.swapped is False

    @pytest.mark.asyncio
    async def test_returns_original_when_cache_service_is_none(self):
        """No cache configured → graceful degradation, ``swapped=False``."""
        outcome = await resolve_canonical_artist("Stereolab", cache_service=None)

        assert outcome.original == "Stereolab"
        assert outcome.canonical == "Stereolab"
        assert outcome.swapped is False

    @pytest.mark.asyncio
    async def test_returns_original_for_empty_or_whitespace_input(self, cache):
        """Empty / whitespace artist string → no cache query, no swap."""
        outcome = await resolve_canonical_artist("   ", cache_service=cache)

        assert outcome.swapped is False
        cache.search_artists_by_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_exception_is_swallowed(self, cache):
        """Cache failure → log + return original. Resolver must not break /lookup."""
        cache.search_artists_by_name.side_effect = RuntimeError("pool down")

        outcome = await resolve_canonical_artist("Stereolab", cache_service=cache)

        assert outcome.original == "Stereolab"
        assert outcome.canonical == "Stereolab"
        assert outcome.swapped is False

    @pytest.mark.asyncio
    async def test_results_are_memoized_per_normalized_key(self, cache):
        """Repeated calls with the same lowercased+unaccented input hit the
        in-memory cache and skip the PG round-trip."""
        cache.search_artists_by_name.return_value = [
            {"id": 1, "name": "Stereolab", "score": 0.95},
        ]

        await resolve_canonical_artist("Stereolab", cache_service=cache)
        await resolve_canonical_artist("stereolab", cache_service=cache)
        await resolve_canonical_artist("STEREOLAB ", cache_service=cache)

        assert cache.search_artists_by_name.await_count == 1


class TestResolverPrePassCallSite:
    """Behavior of ``search_compilations_for_track`` with the resolver pre-pass.

    Both parallel Discogs probes should receive the canonical artist name when
    the resolver swaps AND ``lml_resolve_artist_canonical`` is enabled; the
    original name otherwise.
    """

    @pytest.fixture
    def enforce_resolver_swap(self, monkeypatch):
        """Enable the resolver enforcement flag for tests that exercise the swap path."""
        monkeypatch.setenv("LML_RESOLVE_ARTIST_CANONICAL", "true")
        from config.settings import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_canonical_name_flows_into_both_probes_when_swapped(self, enforce_resolver_swap):
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        cache_service = AsyncMock()
        cache_service.search_artists_by_name = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "name": "Alexander Robotnick",
                    "score": CANONICAL_ARTIST_SIMILARITY_FLOOR + 0.10,
                }
            ]
        )
        service.cache_service = cache_service
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Problemes damour",
                artist="Alexander Robotnick",
                releases=[],
                total=0,
            )
        )

        parsed = ParsedRequest(
            artist="Alexander Robotnik",
            song="Problemes damour",
            raw_message="Alexander Robotnik - Problemes damour",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            await search_compilations_for_track(db, parsed, discogs_service=service)

        assert service.search_releases_by_track.await_count == 2
        for call in service.search_releases_by_track.await_args_list:
            # search_releases_by_track(track, artist, ...) — artist is positional or kwarg
            artist_arg = call.kwargs.get("artist")
            if artist_arg is None and len(call.args) >= 2:
                artist_arg = call.args[1]
            assert artist_arg == "Alexander Robotnick"

    @pytest.mark.asyncio
    async def test_pre_pass_skipped_when_flag_off_no_pg_call(self):
        """Flag default is False. The pre-pass must skip the PG trigram call
        entirely — that's the ~50 ms / lookup we don't pay until calibration
        clears the swap to be enabled. See WXYC/library-metadata-lookup#343
        Option 2. The original artist must still reach both probes."""
        from config.settings import get_settings

        get_settings.cache_clear()  # ensure no stale env from another test
        try:
            db = AsyncMock()
            db.search = AsyncMock(return_value=[])

            service = AsyncMock()
            cache_service = AsyncMock()
            cache_service.search_artists_by_name = AsyncMock(
                return_value=[
                    {
                        "id": 1,
                        "name": "Alexander Robotnick",
                        "score": CANONICAL_ARTIST_SIMILARITY_FLOOR + 0.10,
                    }
                ]
            )
            service.cache_service = cache_service
            service.search_releases_by_track = AsyncMock(
                return_value=TrackReleasesResponse(
                    track="Problemes damour", artist=None, releases=[], total=0
                )
            )

            parsed = ParsedRequest(
                artist="Alexander Robotnik",
                song="Problemes damour",
                raw_message="Alexander Robotnik - Problemes damour",
            )

            with patch(
                "lookup.orchestrator.lookup_releases_by_track",
                new_callable=AsyncMock,
                return_value=[],
            ):
                await search_compilations_for_track(db, parsed, discogs_service=service)

            cache_service.search_artists_by_name.assert_not_awaited()
            assert service.search_releases_by_track.await_count == 2
            for call in service.search_releases_by_track.await_args_list:
                artist_arg = call.kwargs.get("artist")
                if artist_arg is None and len(call.args) >= 2:
                    artist_arg = call.args[1]
                assert artist_arg == "Alexander Robotnik"
        finally:
            get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_original_name_flows_when_score_below_floor(self):
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        cache_service = AsyncMock()
        cache_service.search_artists_by_name = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "name": "Aleksandr Robotnyk",
                    "score": CANONICAL_ARTIST_SIMILARITY_FLOOR - 0.10,
                }
            ]
        )
        service.cache_service = cache_service
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Problemes damour", artist=None, releases=[], total=0
            )
        )

        parsed = ParsedRequest(
            artist="Alexander Robotnik",
            song="Problemes damour",
            raw_message="Alexander Robotnik - Problemes damour",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            await search_compilations_for_track(db, parsed, discogs_service=service)

        assert service.search_releases_by_track.await_count == 2
        for call in service.search_releases_by_track.await_args_list:
            artist_arg = call.kwargs.get("artist")
            if artist_arg is None and len(call.args) >= 2:
                artist_arg = call.args[1]
            assert artist_arg == "Alexander Robotnik"


class TestLibraryCorrectionTwoChannelSeam:
    """The B1 two-channel seam (WXYC/library-metadata-lookup#626).

    ``perform_lookup`` no longer overwrites ``parsed.artist`` with the
    library-fuzzy correction. Instead it threads the corrected name on
    ``parsed.library_artist`` — used ONLY by the library-side legs — while the
    typed ``parsed.artist`` flows unchanged to every Discogs-facing path. These
    tests pin the seam at ``search_compilations_for_track``, where one function
    drives both a Discogs probe (typed) and a library match-back (corrected).

    The trap this prevents: a *distinct* non-library artist whose name is one
    edit from a library artist ("Plug" vs library "Plugz") must hit Discogs
    under the typed name, not the snapped library vocabulary.
    """

    @pytest.mark.asyncio
    async def test_probe_sees_typed_artist_library_match_back_sees_corrected(self):
        """Typed "Plug", corrected library_artist "Plugz".

        The two Discogs probes and ``validate_release_for_track`` must receive
        the TYPED "Plug"; the library match-back must accept the row whose
        artist is the corrected "Plugz".
        """
        db = AsyncMock()
        db.exact_title = AsyncMock(
            return_value=[make_library_item(id=7, artist="Plugz", title="Electric Dreams")]
        )
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = None
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Electric Dreams",
                artist="Plug",
                releases=[
                    ReleaseInfo(
                        album="Electric Dreams",
                        artist="Plug",
                        release_id=555,
                        release_url="https://discogs.com/release/555",
                        is_compilation=False,
                    )
                ],
                total=1,
            )
        )

        parsed = ParsedRequest(
            artist="Plug",
            library_artist="Plugz",
            song="Electric Dreams",
            raw_message="Plug - Electric Dreams",
        )

        with patch(
            "lookup.orchestrator.validate_release_for_track",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_validate:
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        # Discogs probes: the TYPED artist, never the corrected library name.
        assert service.search_releases_by_track.await_count == 2
        for call in service.search_releases_by_track.await_args_list:
            artist_arg = call.kwargs.get("artist")
            if artist_arg is None and len(call.args) >= 2:
                artist_arg = call.args[1]
            assert artist_arg == "Plug"

        # validate_release_for_track: the TYPED artist.
        mock_validate.assert_awaited()
        for call in mock_validate.await_args_list:
            validate_artist = call.args[3] if len(call.args) >= 4 else call.kwargs.get("artist")
            assert validate_artist == "Plug"

        # The corrected library row still binds — the library match-back used
        # "Plugz" (typed "Plug" would also prefix-match here, so the binding
        # alone isn't decisive; the probe/validate assertions above are).
        assert [item.id for item in results] == [7]

    @pytest.mark.asyncio
    async def test_distinct_artist_match_back_uses_corrected_not_typed(self):
        """Typed "Sun" (distinct), corrected library_artist "Sun Araw".

        A library row by an unrelated "Sunburned Hand of the Man" must NOT bind
        off the typed "Sun" — the match-back keys on the corrected "Sun Araw",
        which that row's artist does not start with. Without the seam, the
        match-back would run against whichever single value won, conflating the
        two channels.
        """
        db = AsyncMock()
        # Library album-fuzzy returns an unrelated "Sun"-prefixed row.
        db.exact_title = AsyncMock(
            return_value=[
                make_library_item(id=9, artist="Sunburned Hand of the Man", title="Headdress")
            ]
        )
        db.search = AsyncMock(return_value=[])

        service = AsyncMock()
        service.cache_service = None
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Headdress",
                artist="Sun",
                releases=[
                    ReleaseInfo(
                        album="Headdress",
                        artist="Sun",
                        release_id=556,
                        release_url="https://discogs.com/release/556",
                        is_compilation=False,
                    )
                ],
                total=1,
            )
        )

        parsed = ParsedRequest(
            artist="Sun",
            library_artist="Sun Araw",
            song="Headdress",
            raw_message="Sun - Headdress",
        )

        with patch(
            "lookup.orchestrator.validate_release_for_track",
            new_callable=AsyncMock,
            return_value=True,
        ):
            results, _titles = await search_compilations_for_track(
                db, parsed, discogs_service=service
            )

        # Probes still see the typed "Sun".
        for call in service.search_releases_by_track.await_args_list:
            artist_arg = call.kwargs.get("artist")
            if artist_arg is None and len(call.args) >= 2:
                artist_arg = call.args[1]
            assert artist_arg == "Sun"

        # The unrelated "Sunburned..." row is rejected by the corrected
        # "Sun Araw" match-back — proving the match-back used library_artist,
        # not the typed "Sun" (which the row *would* prefix-match).
        assert results == []
