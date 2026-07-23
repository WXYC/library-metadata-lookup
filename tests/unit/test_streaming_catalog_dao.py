"""Unit tests for ``StreamingCatalogDAO`` construction and validation (LML#842 PR B).

The DAO's PG behavior lives in ``tests/integration/test_streaming_catalog_dao.py``
(``-m pg``). This file pins the pure surface: the dsn/pool XOR construction
contract and the service-token validation, which must fail loudly BEFORE any
pool is created — a typo'd service name in an offline drain script should die
at the call site, not after opening connections to the shared discogs-cache PG.
"""

from __future__ import annotations

import pytest

from scripts.streaming_availability.catalog_dao import StreamingCatalogDAO

# A DSN that must never be dialed: the validation-order tests below pass it to
# prove ValueError beats pool creation. If a test hangs or errors on connect,
# validation has been reordered behind I/O.
_NEVER_DIALED_DSN = "postgresql://lml-unit-test-never-connects/nonexistent"


class TestConstruction:
    def test_requires_dsn_or_pool(self) -> None:
        with pytest.raises(ValueError):
            StreamingCatalogDAO()

    def test_rejects_both_dsn_and_pool(self) -> None:
        with pytest.raises(ValueError):
            StreamingCatalogDAO(dsn=_NEVER_DIALED_DSN, pool=object())  # type: ignore[arg-type]


class TestEmptyBatches:
    """Empty batches return 0 without dialing PG — the set-based bulk inserts
    have no rows to arbitrate, and an offline caller flushing an empty buffer
    should not pay (or fail on) a connection."""

    @pytest.mark.asyncio
    async def test_insert_albums_empty_batch_returns_zero_without_connecting(self) -> None:
        dao = StreamingCatalogDAO(dsn=_NEVER_DIALED_DSN)
        assert await dao.insert_albums([]) == 0

    @pytest.mark.asyncio
    async def test_insert_tracks_empty_batch_returns_zero_without_connecting(self) -> None:
        dao = StreamingCatalogDAO(dsn=_NEVER_DIALED_DSN)
        assert await dao.insert_tracks([]) == 0


class TestServiceValidation:
    @pytest.mark.asyncio
    async def test_get_pending_unknown_service_raises_before_any_connection(self) -> None:
        dao = StreamingCatalogDAO(dsn=_NEVER_DIALED_DSN)
        with pytest.raises(ValueError, match="napster"):
            await dao.get_pending("napster")

    @pytest.mark.asyncio
    async def test_get_pending_rejects_unpivoted_canonical_service(self) -> None:
        """``tidal`` is a legal ``_SERVICES`` slug but outside the legacy
        ResultsDB surface (no wide-pivot column set); the DAO speaks only the
        legacy tokens (spotify/apple/deezer/bandcamp + the discogs
        pseudo-service) until a PR D+ caller needs more."""
        dao = StreamingCatalogDAO(dsn=_NEVER_DIALED_DSN)
        with pytest.raises(ValueError, match="tidal"):
            await dao.get_pending("tidal")

    @pytest.mark.asyncio
    async def test_update_result_rejects_bandcamp(self) -> None:
        """Bandcamp has dedicated methods (``update_bandcamp_url`` /
        ``mark_bandcamp_not_found``); routing it through ``update_result``
        was never legal in the legacy DAO either (silent no-op there — a
        loud ValueError here)."""
        dao = StreamingCatalogDAO(dsn=_NEVER_DIALED_DSN)
        with pytest.raises(ValueError, match="bandcamp"):
            await dao.update_result(1, "bandcamp", "found", url="https://example.test/x")

    @pytest.mark.asyncio
    async def test_update_result_rejects_unknown_service(self) -> None:
        dao = StreamingCatalogDAO(dsn=_NEVER_DIALED_DSN)
        with pytest.raises(ValueError, match="napster"):
            await dao.update_result(1, "napster", "found")

    @pytest.mark.asyncio
    async def test_update_track_resolution_rejects_false_positive(self) -> None:
        """``false_positive`` is the guarded demotion, sanctioned only via
        ``mark_track_false_positive``. The legacy validate phase wrote it
        through this method; accepting it here would let the PR A trigger
        kill a PR D drain mid-phase — a loud redirect at the call site (and
        at port time) beats a RaiseError in production."""
        dao = StreamingCatalogDAO(dsn=_NEVER_DIALED_DSN)
        with pytest.raises(ValueError, match="mark_track_false_positive"):
            await dao.update_track_resolution(1, "false_positive")
