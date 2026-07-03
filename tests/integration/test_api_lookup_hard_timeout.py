"""End-to-end ``/api/v1/lookup`` latency-architecture guards.

Two endpoint-level invariants live here:

* **LML#370 hard cap surfaces as ``LookupResponse.timeout``** — when
  ``execute_search_pipeline``'s hard cap fires (loop gate or per-strategy
  ``asyncio.wait_for``), ``perform_lookup`` must project ``state.timed_out``
  into the response shape so callers can distinguish "no match" (empty
  ``results``, ``timeout: False``) from "ran out of time" (``timeout: True``).
  (This test carries the file's one genuine latency assertion — the
  ``elapsed < 1.5`` bound proving the hard cap limits wall time.)

* **LML#706 streaming probe stays off the response path** — the whole-endpoint
  guard for the #706 cold-tail fix, deterministic causality with fail-fast
  ceilings rather than latency thresholds. A cold lookup (streaming-URL cache
  miss) must return while the live probe is still blocked on a
  test-controlled ``asyncio.Event``; releasing the gate and draining the warm
  tasks then confirms the probe + mint ran to completion off-path.

This is an httpx-ASGITransport + mocked-dependency file — no `pg` or
`external_api` marker, runs in the default suite.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from clients.streaming.apple_music import AppleMusicClient
from discogs.models import DiscogsSearchResponse, TrackReleasesResponse
from entity.sources import PgSource
from entity.store import EntityStore
from entity.streaming_url_cache import ResolveOutcome
from lookup import streaming_url_postprocess as streaming_mod
from tests.conftest import drain_streaming_warm_tasks, reset_streaming_warm_state
from tests.factories import make_discogs_result


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


# Bounded drain shared with the unit suite (single source of truth for the
# subtle while-loop-because-done-callbacks-run-late semantics).
_drain_background_tasks = drain_streaming_warm_tasks


@pytest.fixture(autouse=True)
def _reset_warm_state():
    """Isolate the post-process module's process-global warm state around every
    test, via the shared ``tests.conftest.reset_streaming_warm_state`` (the
    same reset the unit suite delegates to)."""
    reset_streaming_warm_state()
    yield
    reset_streaming_warm_state()


@pytest_asyncio.fixture
async def offpath_harness(library_db, test_settings, monkeypatch):
    """A full ``/lookup`` app client wired so the streaming-URL post-process
    runs Apple-only, with the cache peek forced to a genuine miss and the live
    probe gated on a test-controlled ``asyncio.Event``.

    Guarded seams — stated precisely, because each has a boundary:

    * ``resolve_streaming_url_with_cache`` / ``peek_cached_streaming_url`` are
      patched **on the post-process module's bindings**
      (``lookup.streaming_url_postprocess``). A regression that re-inlines the
      probe through those bindings deadlocks the response against the closed
      gate and trips the outer ``wait_for``. A future call site that imports
      the resolver directly from ``entity.streaming_url_cache`` would NOT be
      intercepted here — if you add one, extend this harness.
    * The Apple client guards the client boundary: the track-level probes
      (``find_track_url`` / ``find_track_metadata``) resolve to ``None`` and
      are ALLOWED (the deliberately-synchronous artwork-bearing path — #706
      kept artwork synchronous), while the album-level methods
      (``find_album_match`` / ``search_album`` / ``search_song``) raise if
      awaited, so a direct-client bypass of the resolver fails loudly.

    Environment: the orchestrator reads flags via a direct, ``@lru_cache``d
    ``get_settings()`` call (not DI), so the fixture sets env and busts the
    cache (setup + teardown). The DI-injected settings object is given the
    same streaming flags so the two surfaces agree — a refactor that threads
    DI settings into the post-process must not flip the feature off. The
    row-less resolve-family flags are pinned OFF so the guarded pipeline path
    is identical on every machine regardless of ambient env/.env; the two
    DI seams not centrally mocked (``get_discogs_cache_service`` /
    ``get_musicbrainz_pg``) are overridden to ``None`` so no ambient
    ``DATABASE_URL_*`` can open real connections mid-test.

    Yields a namespace: ``client`` (httpx AsyncClient), ``gate`` (the Event
    the probe blocks on), ``probed`` (list of ``(service, artist, album)``
    awaits), ``apple`` (the Apple client mock), ``discogs`` (the Discogs
    service mock — reassign ``.search`` for the Discogs-match variant),
    ``entity_store``.

    Teardown is exception-safe: the gate is released and any still-pending
    warms are cancelled + drained before overrides/cache are restored, so an
    assertion failure mid-test doesn't spray "Task was destroyed but it is
    pending!" noise over the real failure.
    """
    from httpx import ASGITransport, AsyncClient

    from config.settings import get_settings
    from core.dependencies import (
        get_discogs_cache_pg,
        get_discogs_cache_service,
        get_discogs_service,
        get_library_db,
        get_musicbrainz_pg,
        get_posthog_client,
    )
    from identity.dependencies import get_entity_store
    from main import app
    from streaming.dependencies import (
        get_apple_music_client,
        get_bandcamp_client,
        get_spotify_client,
    )

    # Apple-only keeps warm-counting deterministic (one warm per cold
    # request). Spotify/Bandcamp are disabled by their client overrides
    # (None) below — the post-process skips a service with no client
    # regardless of its flag, so no per-service flag pinning is needed.
    monkeypatch.setenv("LML_PERSIST_STREAMING_URLS", "true")
    monkeypatch.setenv("LML_PERSIST_STREAMING_URL_APPLE_MUSIC", "true")
    # Pin the row-less resolve family OFF: these are read via direct
    # get_settings()/os.getenv on the same response path, and ambient env (or
    # a repo-root .env) would otherwise route the guarded lookup through
    # different pipeline branches per machine.
    monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "false")
    monkeypatch.setenv("LML_RESOLVE_COMPILATION_RELEASE", "false")
    monkeypatch.setenv("LML_RESOLVE_ARTIST_CANONICAL", "false")
    # The warm wraps the probe in Apple's per-call wait_for ceiling (4s
    # default). The gated probe deliberately blocks until the test releases
    # it, so widen the ceiling — otherwise a slow CI box could time the warm
    # out before the release and flake the post-drain assertions. 15s is
    # >1000x the observed post-release drain latency.
    monkeypatch.setenv("LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS", "15000")
    get_settings.cache_clear()

    # Make the DI-injected settings agree with the env-derived ones the
    # orchestrator reads directly — otherwise a refactor that threads DI
    # settings into the post-process would silently disable the feature under
    # test and fail these guards with a misleading zero-warms signal.
    flagged_settings = test_settings.model_copy(
        update={
            "lml_persist_streaming_urls": True,
            "lml_persist_streaming_url_apple_music": True,
        }
    )

    gate = asyncio.Event()
    probed: list[tuple[str, str, str]] = []

    async def gated_resolve(pg, client, *, service, artist, album, **kwargs):
        probed.append((service, artist, album))
        await gate.wait()
        return ResolveOutcome(
            url="https://music.apple.com/us/album/gated/1234567890", source="live_resolved"
        )

    def _raise_if_awaited(method: str) -> AsyncMock:
        return AsyncMock(
            side_effect=AssertionError(
                f"{method} awaited directly on the response path — album-level "
                "Apple calls must go through resolve_streaming_url_with_cache "
                "in a background warm"
            )
        )

    apple = AsyncMock(spec=AppleMusicClient)
    apple.find_track_url = AsyncMock(return_value=None)
    apple.find_track_metadata = AsyncMock(return_value=None)
    apple.find_album_match = _raise_if_awaited("find_album_match")
    apple.search_album = _raise_if_awaited("search_album")
    apple.search_song = _raise_if_awaited("search_song")

    # Default Discogs service finds nothing: enrichment must still run (a None
    # service skips Step 4 artwork entirely and with it the whole enrichment
    # block), and the empty search drives the LML#401 no-Discogs-match branch —
    # the synthesized streaming-only result whose URL fields are exactly what
    # the post-process backstops. This is the production cold shape from the
    # #706 incident. The Discogs-match variant test reassigns ``.search``.
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
    # NB: the real method returns TrackReleasesResponse (consumers read
    # .releases) — a DiscogsSearchResponse stub here would AttributeError→500
    # the moment a request pair misses the library seed and the track-probing
    # strategies run.
    discogs.search_releases_by_track = AsyncMock(return_value=TrackReleasesResponse(releases=[]))
    discogs.get_release = AsyncMock(return_value=None)
    discogs.validate_track_on_release = AsyncMock(return_value=False)

    entity_store = MagicMock(spec=EntityStore)
    entity_store.mint_or_get_release_identity = AsyncMock(return_value=(7, True))
    # The lookup pipeline reconciles artist identity via get_identity; an
    # auto-generated AsyncMock return would flow into the ReconciledIdentity
    # Pydantic model and 500 the request — "no identity row" is the neutral
    # answer for this harness.
    entity_store.get_identity = AsyncMock(return_value=None)

    try:
        app.dependency_overrides[get_library_db] = lambda: library_db
        app.dependency_overrides[get_discogs_service] = lambda: discogs
        app.dependency_overrides[get_posthog_client] = lambda: None
        app.dependency_overrides[get_settings] = lambda: flagged_settings
        app.dependency_overrides[get_entity_store] = lambda: entity_store
        app.dependency_overrides[get_discogs_cache_pg] = lambda: AsyncMock(spec=PgSource)
        # Neither seam is exercised by these tests; without an override each
        # falls through to its real factory, which reads ambient env/.env and
        # can open a real PG pool (up to a 10s connect) inside the first
        # request — a flake indistinguishable from the regression under guard.
        app.dependency_overrides[get_discogs_cache_service] = lambda: None
        app.dependency_overrides[get_musicbrainz_pg] = lambda: None
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
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield SimpleNamespace(
                    client=client,
                    gate=gate,
                    probed=probed,
                    apple=apple,
                    discogs=discogs,
                    entity_store=entity_store,
                )
    finally:
        # Unblock and reap any warms a failed test left parked on the gate so
        # the loop closes cleanly (no destroyed-pending-task noise burying the
        # real assertion failure).
        gate.set()
        pending = list(streaming_mod._background_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        app.dependency_overrides.clear()
        get_settings.cache_clear()


class TestApiLookupStreamingProbeOffPath:
    @pytest.mark.asyncio
    async def test_cold_lookup_returns_while_probe_is_still_blocked(self, offpath_harness):
        """The #706 invariant, endpoint-level: a cold lookup completes while the
        live probe is gate-blocked, then the released warm completes off-path.

        The 5s ``wait_for`` is a fail-fast ceiling, not a latency assertion
        (the happy path measures ~10ms): with the gate closed the probe can
        never finish, so a synchronous probe on the response path parks the
        request on the gate and trips the ceiling. The causality proof IS the
        request completing — program order guarantees the gate is still closed
        here (only this test body ever sets it, below).
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
            timeout=5.0,
        )

        assert resp.status_code == 200
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
    async def test_discogs_match_branch_also_keeps_probe_off_path(self, offpath_harness):
        """Same causality on the Discogs-MATCH enrichment branch.

        The default harness exercises only the empty-Discogs LML#401 synthesis
        branch. But the artwork-bearing match branch is where a synchronous
        streaming resolve is likeliest to be bolted on next — it already hosts
        the one deliberately-synchronous Apple probe (``find_track_url``, the
        happy path). Reassign the search mock so the request binds a real
        Discogs result, then assert the same off-path invariant, plus that the
        allowed track-level probe DID run synchronously (pinning the artwork
        carve-out rather than assuming it).
        """
        h = offpath_harness
        match = make_discogs_result(
            release_id=8001, album="Emperor Tomato Ketchup", artist="Stereolab"
        )
        h.discogs.search = AsyncMock(return_value=DiscogsSearchResponse(results=[match]))

        resp = await asyncio.wait_for(
            h.client.post(
                "/api/v1/lookup",
                json={
                    "artist": "Stereolab",
                    "album": "Emperor Tomato Ketchup",
                    "raw_message": "Stereolab - Emperor Tomato Ketchup",
                },
            ),
            timeout=5.0,
        )

        assert resp.status_code == 200
        assert "music.apple.com" not in resp.text
        assert len(streaming_mod._background_tasks) == 1

        h.gate.set()
        await _drain_background_tasks()

        assert h.probed == [("apple_music_album", "Stereolab", "Emperor Tomato Ketchup")]
        assert not streaming_mod._streaming_warm_in_flight
        # The happy-path track probe is the ALLOWED synchronous Apple call
        # (URL-only; artwork on this branch comes from the Discogs row) — it
        # must have run in-request, unlike the album-level probe.
        h.apple.find_track_url.assert_awaited()
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
            ("Autechre", "Confield"),
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
            timeout=10.0,
        )

        # All five responses returned while every probe was still gate-blocked
        # (program order: the gate is set only below).
        assert [r.status_code for r in responses] == [200] * 5
        # One warm per distinct (service, artist, album) — request-internal
        # duplicate misses (multi-row results) dedup to a single task.
        assert len(streaming_mod._background_tasks) == 5

        h.gate.set()
        await _drain_background_tasks()

        assert sorted(h.probed) == sorted(
            ("apple_music_album", artist, album) for artist, album in requests
        )
        assert not streaming_mod._streaming_warm_in_flight
