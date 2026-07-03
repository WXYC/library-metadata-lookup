"""End-to-end ``/api/v1/lookup`` latency-architecture guards.

Two endpoint-level invariants live here, both deterministic (no wall-clock
thresholds beyond generous fail-fast ceilings):

* **LML#370 hard cap surfaces as ``LookupResponse.timeout``** — when
  ``execute_search_pipeline``'s hard cap fires (loop gate or per-strategy
  ``asyncio.wait_for``), ``perform_lookup`` must project ``state.timed_out``
  into the response shape so callers can distinguish "no match" (empty
  ``results``, ``timeout: False``) from "ran out of time" (``timeout: True``).

* **LML#706 streaming probe stays off the response path** — the whole-endpoint
  guard for the #706 cold-tail fix. A cold lookup (streaming-URL cache miss)
  must return while the live probe is still blocked on a test-controlled
  ``asyncio.Event``, proving the inline external fan-out cannot silently
  re-block ``/lookup``. Causality, not timing: the response completing while
  the gate is closed is the assertion; releasing the gate and draining
  ``_background_tasks`` then confirms the warm ran to completion off-path.

This is a pure TestClient + mocked-dependency file — no `pg` or `external_api`
marker, runs in the default suite.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from clients.streaming.apple_music import AppleMusicClient
from discogs.models import DiscogsSearchResponse
from entity.sources import PgSource
from entity.store import EntityStore
from entity.streaming_url_cache import ResolveOutcome
from lookup import streaming_url_postprocess as streaming_mod


class TestApiLookupHardTimeout:
    @pytest.mark.asyncio
    async def test_hard_cap_surfaces_as_timeout_true(self, app_client, monkeypatch):
        """Slow first strategy + tight hard cap → response has ``timeout: true``.

        Patches the orchestrator's ``search_library_with_fallback`` so it
        hangs for longer than the configured hard cap. With the cap at
        150 ms, the per-strategy ``wait_for`` raises ``TimeoutError``, the
        loop records ``state.timed_out=True``, and the response should
        carry ``timeout: true`` (default ``false``) with empty ``results``.
        """
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "150")
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "60000")

        async def slow_empty(*_args, **_kwargs):
            await asyncio.sleep(10)
            return ([], False)

        with patch(
            "lookup.orchestrator.search_library_with_fallback",
            side_effect=slow_empty,
        ):
            # 5s outer wait_for: if wait_for-propagation regresses, fail
            # fast instead of stalling CI for 10s waiting on the mock.
            start = time.monotonic()
            resp = await asyncio.wait_for(
                app_client.post(
                    "/api/v1/lookup",
                    json={
                        "artist": "Untraceable Artist",
                        "song": "Untraceable Song",
                        "raw_message": "Untraceable Artist - Untraceable Song",
                    },
                ),
                timeout=5.0,
            )
            elapsed = time.monotonic() - start

        assert resp.status_code == 200
        body = resp.json()
        # The whole point: response carries the hard-cap signal.
        assert body.get("timeout") is True, body
        # Pipeline abandoned mid-execution; no library results promoted.
        assert body.get("results") == []
        # Wall time bounded by hard cap (plus pipeline overhead).
        assert elapsed < 1.5, f"hard cap should bound wall time, took {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_normal_lookup_has_timeout_false(self, app_client):
        """A successful lookup carries ``timeout: false`` (default).

        Guards against accidentally always-setting the field — the default
        path must remain byte-identical for callers that ignore it.
        """
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Stereolab",
                "album": "Dots and Loops",
                "raw_message": "Stereolab - Dots and Loops",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # Field is present and false (or omitted, which the consumer reads as false).
        assert body.get("timeout") is False or "timeout" not in body


# ---------------------------------------------------------------------------
# LML#706: the streaming-URL live probe must stay off the /lookup response path.
# ---------------------------------------------------------------------------


async def _drain_background_tasks() -> None:
    """Await every scheduled warm so its done-callbacks run (clearing the task
    set and the dedup key), letting post-drain assertions observe the result."""
    while streaming_mod._background_tasks:
        await asyncio.gather(*list(streaming_mod._background_tasks), return_exceptions=True)


@pytest.fixture(autouse=True)
def _reset_warm_state():
    """Isolate the post-process module's process-global warm state (semaphore,
    dedup set, task set) — same reset the unit suite uses, so a leaked dedup
    key or a semaphore bound to a prior event loop can't leak across tests."""
    streaming_mod._streaming_warm_semaphore = None
    streaming_mod._streaming_warm_in_flight.clear()
    streaming_mod._background_tasks.clear()
    yield
    streaming_mod._streaming_warm_in_flight.clear()
    streaming_mod._background_tasks.clear()
    streaming_mod._streaming_warm_semaphore = None


@pytest_asyncio.fixture
async def offpath_harness(library_db, test_settings, monkeypatch):
    """A full ``/lookup`` app client wired so the streaming-URL post-process
    runs Apple-only, with the cache peek forced to a genuine miss and the live
    probe gated on a test-controlled ``asyncio.Event``.

    The two patches sit at the same seams the unit suite uses
    (``peek_cached_streaming_url`` / ``resolve_streaming_url_with_cache``), but
    the request travels the real router → orchestrator → post-process chain, so
    a synchronous probe reintroduced ANYWHERE on that chain deadlocks the
    response against the closed gate and fails the test's outer ``wait_for``.

    Yields a namespace: ``client`` (httpx AsyncClient), ``gate`` (the Event the
    probe blocks on), ``probed`` (list of ``(service, artist, album)`` awaits),
    ``apple`` (the Apple client mock), ``entity_store``.

    The Apple client's track-level probes (``find_track_url`` /
    ``find_track_metadata``) resolve to ``None`` — those calls are the
    deliberately-synchronous artwork-bearing path and are ALLOWED (#706 kept
    them on the hot path; artwork stays synchronous). ``find_album_match`` —
    the album-level probe behind the resolver — raises if awaited, guarding
    the resolver seam against a direct-client-call bypass.
    """
    from httpx import ASGITransport, AsyncClient

    from config.settings import get_settings
    from core.dependencies import (
        get_discogs_cache_pg,
        get_discogs_service,
        get_library_db,
        get_posthog_client,
    )
    from identity.dependencies import get_entity_store
    from main import app
    from streaming.dependencies import (
        get_apple_music_client,
        get_bandcamp_client,
        get_spotify_client,
    )

    # The orchestrator reads these via a direct ``get_settings()`` call (not
    # DI), and ``get_settings`` is ``@lru_cache``d — env alone is not enough
    # once any earlier test (or import-time caller) has populated the cache
    # with the flags at their defaults. Set env, then bust the cache; bust it
    # again on teardown so the flag-enabled Settings can't leak onward.
    # Apple-only keeps warm-counting deterministic (one warm per cold request).
    monkeypatch.setenv("LML_PERSIST_STREAMING_URLS", "true")
    monkeypatch.setenv("LML_PERSIST_STREAMING_URL_APPLE_MUSIC", "true")
    monkeypatch.setenv("LML_PERSIST_STREAMING_URL_SPOTIFY", "false")
    monkeypatch.setenv("LML_PERSIST_STREAMING_URL_BANDCAMP", "false")
    # The warm wraps the probe in Apple's per-call wait_for ceiling (4s
    # default). The gated probe deliberately blocks until the test releases
    # it, so widen the ceiling — otherwise a slow CI box could time the warm
    # out before the release and flake the post-drain assertions.
    monkeypatch.setenv("LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS", "60000")
    get_settings.cache_clear()

    gate = asyncio.Event()
    probed: list[tuple[str, str, str]] = []

    async def gated_resolve(pg, client, *, service, artist, album, **kwargs):
        probed.append((service, artist, album))
        await gate.wait()
        return ResolveOutcome(
            url="https://music.apple.com/us/album/gated/1234567890", source="live_resolved"
        )

    apple = AsyncMock(spec=AppleMusicClient)
    apple.find_track_url = AsyncMock(return_value=None)
    apple.find_track_metadata = AsyncMock(return_value=None)
    apple.find_album_match = AsyncMock(
        side_effect=AssertionError(
            "find_album_match awaited directly — the album probe must go "
            "through resolve_streaming_url_with_cache in a background warm"
        )
    )

    # A Discogs service that finds nothing: enrichment must still run (a None
    # service skips Step 4 artwork entirely and with it the whole enrichment
    # block), and the empty search drives the LML#401 no-Discogs-match branch —
    # the synthesized streaming-only result whose URL fields are exactly what
    # the post-process backstops. This is the production cold shape from the
    # #706 incident: album absent from the Discogs catalog, streaming cache cold.
    discogs = AsyncMock(
        spec_set=[
            "search",
            "search_releases_by_track",
            "validate_track_on_release",
            "get_release",
            "cache_service",
        ]
    )
    discogs.cache_service = None
    discogs.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
    discogs.search_releases_by_track = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
    discogs.get_release = AsyncMock(return_value=None)
    discogs.validate_track_on_release = AsyncMock(return_value=False)

    entity_store = MagicMock(spec=EntityStore)
    entity_store.mint_or_get_release_identity = AsyncMock(return_value=(7, True))
    # The lookup pipeline reconciles artist identity via get_identity; an
    # auto-generated AsyncMock return would flow into the ReconciledIdentity
    # Pydantic model and 500 the request — "no identity row" is the neutral
    # answer for this harness.
    entity_store.get_identity = AsyncMock(return_value=None)

    app.dependency_overrides[get_library_db] = lambda: library_db
    app.dependency_overrides[get_discogs_service] = lambda: discogs
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_entity_store] = lambda: entity_store
    app.dependency_overrides[get_discogs_cache_pg] = lambda: AsyncMock(spec=PgSource)
    app.dependency_overrides[get_apple_music_client] = lambda: apple
    app.dependency_overrides[get_spotify_client] = lambda: None
    app.dependency_overrides[get_bandcamp_client] = lambda: None

    with (
        patch.object(
            streaming_mod,
            "peek_cached_streaming_url",
            new=AsyncMock(return_value=(None, False)),
        ),
        patch.object(
            streaming_mod,
            "resolve_streaming_url_with_cache",
            new=AsyncMock(side_effect=gated_resolve),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield SimpleNamespace(
                client=client,
                gate=gate,
                probed=probed,
                apple=apple,
                entity_store=entity_store,
            )

    app.dependency_overrides.clear()
    get_settings.cache_clear()


class TestApiLookupStreamingProbeOffPath:
    @pytest.mark.asyncio
    async def test_cold_lookup_returns_while_probe_is_still_blocked(self, offpath_harness):
        """The #706 invariant, endpoint-level: a cold lookup completes while the
        live probe is gate-blocked, then the released warm completes off-path.

        The 10s ``wait_for`` is a fail-fast ceiling, not a latency assertion:
        with the gate closed the probe can never finish, so a synchronous
        probe anywhere on the response path parks the request on the gate and
        trips the ceiling.
        """
        h = offpath_harness
        resp = await asyncio.wait_for(
            h.client.post(
                "/api/v1/lookup",
                json={
                    "artist": "Stereolab",
                    "album": "Aluminum Tunes",
                    "raw_message": "Stereolab - Aluminum Tunes",
                },
            ),
            timeout=10.0,
        )

        # Causality: the response is complete and the gate was never released,
        # so nothing on the response path waited for the probe.
        assert resp.status_code == 200
        assert not h.gate.is_set()
        # Eventual consistency: the first (cold) lookup surfaces no Apple URL —
        # neither the gated probe's URL nor anything else Apple-shaped.
        assert "music.apple.com" not in resp.text
        # Exactly one warm was scheduled for the (apple, artist, album) miss.
        assert len(streaming_mod._background_tasks) == 1

        # Release the gate: the warm must run the probe + mint to completion.
        h.gate.set()
        await _drain_background_tasks()

        assert h.probed == [("apple_music_album", "Stereolab", "Aluminum Tunes")]
        h.entity_store.mint_or_get_release_identity.assert_awaited_once_with(
            source="apple_music_album", external_id="1234567890"
        )
        assert not streaming_mod._streaming_warm_in_flight
        # The album-level client probe was never bypass-called directly.
        h.apple.find_album_match.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_concurrent_cold_lookups_all_return_before_any_probe_releases(
        self, offpath_harness
    ):
        """K=5 concurrent cold lookups (distinct albums) all complete while
        every probe is still gate-blocked — the congestion shape from the #706
        incident (cold requests stacking up behind synchronous fan-out) cannot
        re-form. Same causality assertion as above, N-wide.
        """
        h = offpath_harness
        requests = [
            ("Stereolab", "Dots and Loops"),
            ("Jessica Pratt", "On Your Own Love Again"),
            ("Cat Power", "Moon Pix"),
            ("Juana Molina", "DOGA"),
            ("Grimes", "Visions"),
        ]

        responses = await asyncio.wait_for(
            asyncio.gather(
                *(
                    h.client.post(
                        "/api/v1/lookup",
                        json={
                            "artist": artist,
                            "album": album,
                            "raw_message": f"{artist} - {album}",
                        },
                    )
                    for artist, album in requests
                )
            ),
            timeout=20.0,
        )

        # All five responses returned; no probe has been allowed to finish.
        assert [r.status_code for r in responses] == [200] * 5
        assert not h.gate.is_set()
        # One warm per distinct (service, artist, album) — request-internal
        # duplicate misses (multi-row results) dedup to a single task.
        assert len(streaming_mod._background_tasks) == 5

        h.gate.set()
        await _drain_background_tasks()

        assert sorted(h.probed) == sorted(
            ("apple_music_album", artist, album) for artist, album in requests
        )
        assert not streaming_mod._streaming_warm_in_flight
