"""Tests for the streaming-check router endpoint."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import get_settings
from core.dependencies import get_streaming_posthog_client
from streaming.dependencies import (
    get_apple_music_client,
    get_bandcamp_client,
    get_deezer_client,
    get_spotify_client,
)
from streaming.models import SourceMatch, StreamingCheckResponse, StreamingCheckSources
from tests.unit.conftest import override_deps


def _captured_events(mock_posthog):
    """Event names PostHog.capture was called with (kwargs-style call sites)."""
    return [call.kwargs.get("event") for call in mock_posthog.capture.call_args_list]


def _completed_props(mock_posthog):
    """Properties dict from the single ``streaming_check_completed`` summary event."""
    for call in mock_posthog.capture.call_args_list:
        if call.kwargs.get("event") == "streaming_check_completed":
            return call.kwargs["properties"]
    raise AssertionError(
        f"no streaming_check_completed event emitted; saw {_captured_events(mock_posthog)}"
    )


@pytest.fixture
def app_client(mock_settings):
    """FastAPI test app with streaming dependencies overridden."""
    from main import app

    with override_deps(
        app,
        {
            get_settings: mock_settings,
            get_spotify_client: None,
            get_deezer_client: AsyncMock(),
            get_apple_music_client: None,
            get_bandcamp_client: None,
        },
    ):
        yield app


@pytest.mark.asyncio
async def test_streaming_check_success(app_client):
    """POST /api/v1/streaming-check returns 200 with valid response."""
    mock_response = StreamingCheckResponse(
        on_streaming=True,
        sources=StreamingCheckSources(
            spotify=SourceMatch(url="https://open.spotify.com/album/abc", confidence=95.0),
        ),
    )

    with patch(
        "streaming.router.check_streaming_availability",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/streaming-check",
                json={"artist": "Stereolab", "title": "Aluminum Tunes"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["on_streaming"] is True
    assert data["sources"]["spotify"]["url"] == "https://open.spotify.com/album/abc"
    assert data["sources"]["spotify"]["confidence"] == 95.0


@pytest.mark.asyncio
async def test_streaming_check_not_found(app_client):
    """POST /api/v1/streaming-check returns on_streaming=False when no match."""
    mock_response = StreamingCheckResponse(
        on_streaming=False,
        sources=StreamingCheckSources(),
    )

    with patch(
        "streaming.router.check_streaming_availability",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/streaming-check",
                json={"artist": "Stereolab", "title": "Aluminum Tunes"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["on_streaming"] is False


@pytest.mark.asyncio
async def test_streaming_check_missing_fields(app_client):
    """POST /api/v1/streaming-check returns 422 when required fields are missing."""
    async with AsyncClient(
        transport=ASGITransport(app=app_client), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/streaming-check", json={})

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_streaming_check_internal_error(app_client):
    """POST /api/v1/streaming-check returns 500 on unexpected exception."""
    with patch(
        "streaming.router.check_streaming_availability",
        new_callable=AsyncMock,
        side_effect=RuntimeError("unexpected"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/streaming-check",
                json={"artist": "Stereolab", "title": "Aluminum Tunes"},
            )

    assert resp.status_code == 500


# --- Telemetry (LML#639) -------------------------------------------------------
#
# /api/v1/streaming-check is on the synchronous flowsheet-add path (Backend-Service
# and tubafrenzy call it on every add), so it must emit a per-request PostHog
# summary event for observability — but a telemetry failure must never fail the
# check (see test_telemetry_failure_does_not_fail_check).


@pytest.fixture
def telemetry_app_client(mock_settings):
    """App with a mock PostHog client injected so emitted events are inspectable."""
    from main import app

    mock_posthog = Mock()
    mock_posthog.capture = Mock()

    with override_deps(
        app,
        {
            get_settings: mock_settings,
            get_spotify_client: None,
            get_deezer_client: AsyncMock(),
            get_apple_music_client: None,
            get_bandcamp_client: None,
            get_streaming_posthog_client: mock_posthog,
        },
    ):
        yield app, mock_posthog


async def _post_streaming_check(app, response):
    with patch(
        "streaming.router.check_streaming_availability",
        new_callable=AsyncMock,
        return_value=response,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(
                "/api/v1/streaming-check",
                json={"artist": "Stereolab", "title": "Aluminum Tunes"},
            )


@pytest.mark.asyncio
async def test_summary_event_emitted_on_success(telemetry_app_client):
    """A successful check emits a streaming_check_completed summary event."""
    app, mock_posthog = telemetry_app_client
    response = StreamingCheckResponse(
        on_streaming=True,
        sources=StreamingCheckSources(
            spotify=SourceMatch(url="https://open.spotify.com/album/abc", confidence=95.0),
        ),
    )

    resp = await _post_streaming_check(app, response)

    assert resp.status_code == 200
    assert "streaming_check_completed" in _captured_events(mock_posthog)


@pytest.mark.asyncio
async def test_summary_event_carries_environment_property(telemetry_app_client, monkeypatch):
    """WXYC/library-metadata-lookup#1170: `streaming_check_completed` is one of
    the `RequestTelemetry`-backed summary events the new LML PostHog project's
    guard alert needs to slice by `environment` -- same rationale and property
    name as `lookup_completed` and the `discogs_rate_gate_*` counters.
    `_emit_summary` reads `environment` directly via `get_settings()` at the
    emit site (mirrors `discogs/ratelimit.py`'s counters), not via FastAPI DI,
    so this drives it through env + `cache_clear()` rather than an
    `app.dependency_overrides` swap.
    """
    app, mock_posthog = telemetry_app_client
    monkeypatch.setenv("ENVIRONMENT", "unit-test-env")
    get_settings.cache_clear()

    try:
        response = StreamingCheckResponse(on_streaming=False, sources=StreamingCheckSources())
        resp = await _post_streaming_check(app, response)
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 200
    props = _completed_props(mock_posthog)
    assert props["environment"] == "unit-test-env"


@pytest.mark.asyncio
async def test_summary_event_carries_per_service_verdict(telemetry_app_client):
    """The summary event derives on_streaming + per-service verdicts from the response.

    Each service is classified matched / errored / absent from the response alone
    (sources, errored_sources). A service can match while another errors (LML#376).
    """
    app, mock_posthog = telemetry_app_client
    response = StreamingCheckResponse(
        on_streaming=True,
        sources=StreamingCheckSources(
            spotify=SourceMatch(url="https://open.spotify.com/album/abc", confidence=95.0),
        ),
        errored_sources=["deezer"],
    )

    resp = await _post_streaming_check(app, response)

    assert resp.status_code == 200
    props = _completed_props(mock_posthog)
    assert props["on_streaming"] is True
    assert props["match_count"] == 1
    assert props["errored_count"] == 1
    assert props["errored_sources"] == ["deezer"]
    assert props["verdict_spotify"] == "matched"
    assert props["verdict_deezer"] == "errored"
    assert props["verdict_apple_music"] == "absent"
    assert props["verdict_bandcamp"] == "absent"


@pytest.mark.asyncio
async def test_summary_event_has_stable_zeroed_cache_and_api_calls(telemetry_app_client):
    """cache + api_calls are emitted with a stable shape, zeroed until LML#641.

    The streaming-check path is application-cache-free and the clients record no
    API calls yet, so these fields carry the *shape* (base cache keys; one key
    per service) at value 0 — asserted here so #641 has a contract to fill.
    """
    app, mock_posthog = telemetry_app_client
    response = StreamingCheckResponse(on_streaming=False, sources=StreamingCheckSources())

    resp = await _post_streaming_check(app, response)

    assert resp.status_code == 200
    props = _completed_props(mock_posthog)

    assert props["api_calls"] == {
        "spotify": 0,
        "deezer": 0,
        "apple_music": 0,
        "bandcamp": 0,
    }
    # Base cache-stats keys present and zeroed (shape stability, not values).
    cache = props["cache"]
    assert set(cache) == {
        "memory_hits",
        "pg_hits",
        "pg_misses",
        "api_calls",
        "pg_time_ms",
        "api_time_ms",
    }
    assert all(value == 0 for value in cache.values())
    # Real per-request timing is present (assert the key exists and is numeric —
    # `>= 0` alone is tautological for a perf_counter delta).
    assert "availability_ms" in props["steps"]
    assert isinstance(props["total_duration_ms"], (int, float))


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_fail_check(telemetry_app_client):
    """A PostHog emit failure is swallowed — the check still returns its result.

    /streaming-check is on the synchronous flowsheet-add path; a telemetry hiccup
    becoming a 500 would fail a DJ's flowsheet write. The emit must be wrapped in
    its own swallow, NOT the handler's 500 try (LML#639).
    """
    app, mock_posthog = telemetry_app_client
    mock_posthog.capture.side_effect = RuntimeError("posthog down")
    response = StreamingCheckResponse(
        on_streaming=True,
        sources=StreamingCheckSources(
            spotify=SourceMatch(url="https://open.spotify.com/album/abc", confidence=95.0),
        ),
    )

    resp = await _post_streaming_check(app, response)

    assert resp.status_code == 200
    assert resp.json()["on_streaming"] is True
    # Guard against a vacuous pass: the swallow is only exercised if the emit was
    # actually attempted. If a regression short-circuited telemetry, capture would
    # never fire and this test would pass without testing the swallow at all.
    mock_posthog.capture.assert_called()


@pytest.mark.asyncio
async def test_failure_path_emits_error_summary(telemetry_app_client, monkeypatch):
    """A total failure (500 path) still emits a streaming_check_completed event.

    The orchestrator absorbs per-service failures into errored_sources (a 200);
    a raise out of check_streaming_availability is a total failure that 500s. We
    must report it too (LML#639) — outcome=error, error_type set, per-service
    verdicts 'unknown' since no result was produced. Also pins `environment`
    (LML#1170) on this path specifically: it is built by a different
    `_emit_summary` call (positional `None` response, distinct arg list) than
    the success path, so the property needs its own coverage here.
    """
    app, mock_posthog = telemetry_app_client
    monkeypatch.setenv("ENVIRONMENT", "unit-test-env")
    get_settings.cache_clear()

    try:
        with patch(
            "streaming.router.check_streaming_availability",
            new_callable=AsyncMock,
            side_effect=RuntimeError("upstream exploded"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/streaming-check",
                    json={"artist": "Stereolab", "title": "Aluminum Tunes"},
                )
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 500
    props = _completed_props(mock_posthog)
    assert props["outcome"] == "error"
    assert props["error_type"] == "RuntimeError"
    assert props["on_streaming"] is None
    assert props["verdict_spotify"] == "unknown"
    assert props["verdict_bandcamp"] == "unknown"
    # Timing is still captured even though the step raised.
    assert "availability_ms" in props["steps"]
    assert props["environment"] == "unit-test-env"


@pytest.mark.asyncio
async def test_telemetry_failure_on_error_path_preserves_500(telemetry_app_client):
    """A telemetry failure on the 500 path must not mask the real error.

    The failure-path emit is swallowed, so the handler still raises its own
    HTTPException(500, "Streaming check failed") rather than leaking the PostHog
    exception as a generic 500 (LML#639).
    """
    app, mock_posthog = telemetry_app_client
    mock_posthog.capture.side_effect = RuntimeError("posthog down")

    with patch(
        "streaming.router.check_streaming_availability",
        new_callable=AsyncMock,
        side_effect=RuntimeError("upstream exploded"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/streaming-check",
                json={"artist": "Stereolab", "title": "Aluminum Tunes"},
            )

    assert resp.status_code == 500
    assert resp.json()["message"] == "Streaming check failed"


@pytest.mark.asyncio
async def test_success_summary_marks_outcome(telemetry_app_client):
    """The success event shares the failure event's shape: outcome + error_type."""
    app, mock_posthog = telemetry_app_client
    response = StreamingCheckResponse(on_streaming=False, sources=StreamingCheckSources())

    resp = await _post_streaming_check(app, response)

    assert resp.status_code == 200
    props = _completed_props(mock_posthog)
    assert props["outcome"] == "success"
    assert props["error_type"] is None


@pytest.mark.asyncio
async def test_cache_stats_bracket_is_live(telemetry_app_client):
    """init_cache_stats() actually wires the per-request cache-stats dict.

    The zeroed-shape test above passes even if the bracket is removed (the
    telemetry helper falls back to a zeroed default), so it can't prove the
    bracket is live. This records into cache_stats *during* the request — the way
    LML#641's instrumented clients will — and asserts the value surfaces in the
    emitted event. Removing init_cache_stats() makes the recorder a silent no-op,
    so this fails: cache.memory_hits would be 0, not 1.
    """
    from wxyc_fastapi.observability.cache_stats import get_cache_stats_recorder

    app, mock_posthog = telemetry_app_client
    response = StreamingCheckResponse(on_streaming=False, sources=StreamingCheckSources())

    async def _record_then_return(*args, **kwargs):
        get_cache_stats_recorder().record_memory_cache_hit()
        return response

    with patch(
        "streaming.router.check_streaming_availability",
        side_effect=_record_then_return,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/streaming-check",
                json={"artist": "Stereolab", "title": "Aluminum Tunes"},
            )

    assert resp.status_code == 200
    assert _completed_props(mock_posthog)["cache"]["memory_hits"] == 1


@pytest.mark.asyncio
async def test_per_step_event_emitted_alongside_summary(telemetry_app_client):
    """The shared helper emits a per-step streaming_check_availability event too.

    Documented alongside the summary; pinned here so a helper change that drops or
    renames the per-step event is caught against the docs.
    """
    app, mock_posthog = telemetry_app_client
    response = StreamingCheckResponse(on_streaming=False, sources=StreamingCheckSources())

    resp = await _post_streaming_check(app, response)

    assert resp.status_code == 200
    assert "streaming_check_availability" in _captured_events(mock_posthog)
