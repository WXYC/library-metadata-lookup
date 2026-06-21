"""Router for the streaming availability check endpoint."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from wxyc_fastapi.observability import RequestTelemetry, init_cache_stats

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.deezer import DeezerClient
from clients.streaming.spotify import SpotifyClient
from core.dependencies import get_posthog_client
from streaming.dependencies import (
    get_apple_music_client,
    get_bandcamp_client,
    get_deezer_client,
    get_spotify_client,
)
from streaming.models import (
    StreamingCheckRequest,
    StreamingCheckResponse,
    StreamingCheckSources,
)
from streaming.orchestrator import check_streaming_availability

if TYPE_CHECKING:
    from posthog import Posthog

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streaming"])

# The streaming services dispatched by ``check_streaming_availability``, in a
# fixed order. Derived from ``StreamingCheckSources`` (pydantic preserves
# field-declaration order) so the telemetry service list can't drift from the
# response shape. Adding a provider still requires editing the orchestrator
# (its ``clients`` dict + kwargs + the ``_EXPECTED_SERVICE_FIELDS`` guard, which
# halts import on drift) — but this list then updates automatically rather than
# being a separate literal to forget. Doubles as the ``RequestTelemetry``
# ``api_call_keys`` (stable ``api_calls`` shape, all zero until LML#641
# instruments the clients) and as the verdict-iteration order.
_STREAMING_SERVICES: tuple[str, ...] = tuple(StreamingCheckSources.model_fields)


def _summary_properties(
    response: StreamingCheckResponse | None,
    *,
    error_type: str | None = None,
) -> dict[str, object]:
    """Per-service verdict + roll-up for the summary event, one stable shape.

    The orchestrator fans out all services concurrently in one gather, so
    per-service *timing* isn't separable at this layer, but each service's
    *outcome* is. Both the success and total-failure paths emit this same shape
    so PostHog sees one schema (LML#639):

    - Success (``response`` set, ``error_type`` None): each service is
      ``matched`` (a ``SourceMatch`` is present), ``errored`` (the service raised
      — listed in ``errored_sources``; a 200 with ``on_streaming=None`` per
      LML#376), or ``absent`` (dispatched-and-no-match, or not dispatched —
      indistinguishable from the response alone; both mean "no URL").
    - Total failure (``response`` None, ``error_type`` set): ``check_streaming_
      availability`` itself raised, so no result exists — every verdict is
      ``unknown`` and ``on_streaming`` is None. This is the 500 path.
    """
    # Total-failure path: no result exists, so every verdict is ``unknown``.
    # Early-return keeps the success branch below free of ``if response`` noise.
    if response is None:
        return {
            "outcome": "error",
            "error_type": error_type,
            "on_streaming": None,
            "errored_sources": [],
            "errored_count": 0,
            "match_count": 0,
            **{f"verdict_{service}": "unknown" for service in _STREAMING_SERVICES},
        }

    errored = set(response.errored_sources)
    props: dict[str, object] = {
        "outcome": "success",
        "error_type": None,
        "on_streaming": response.on_streaming,
        "errored_sources": response.errored_sources,
        "errored_count": len(response.errored_sources),
    }
    match_count = 0
    for service in _STREAMING_SERVICES:
        if getattr(response.sources, service) is not None:
            verdict = "matched"
            match_count += 1
        elif service in errored:
            verdict = "errored"
        else:
            verdict = "absent"
        props[f"verdict_{service}"] = verdict
    props["match_count"] = match_count
    return props


def _emit_summary(
    posthog_client: Posthog | None,
    telemetry: RequestTelemetry,
    response: StreamingCheckResponse | None,
    *,
    error_type: str | None = None,
) -> None:
    """Best-effort summary emit, used on BOTH the success and failure paths.

    Building the properties (``_summary_properties``) is done *inside* the swallow
    too, not just the send — so the whole telemetry step (incl. deriving verdicts)
    can never change the handler's outcome: on success the response still returns,
    on failure the 500 still raises (LML#639). No-op when telemetry is disabled.
    """
    if not posthog_client:
        return
    try:
        telemetry.send_to_posthog(
            posthog_client, _summary_properties(response, error_type=error_type)
        )
    except Exception:
        logger.warning("streaming-check telemetry emit failed", exc_info=True)


@router.post(
    "/streaming-check",
    response_model=StreamingCheckResponse,
    summary="Check streaming availability for an album",
    description=(
        "Checks Spotify, Deezer, Apple Music, and Bandcamp for a given artist+title. "
        "Returns whether the album is available on streaming platforms and URLs for each match."
    ),
    responses={
        200: {"description": "Streaming availability result"},
        500: {"description": "Internal server error"},
    },
)
async def handle_streaming_check(
    request: StreamingCheckRequest,
    spotify: SpotifyClient | None = Depends(get_spotify_client),
    deezer: DeezerClient = Depends(get_deezer_client),
    apple_music: AppleMusicClient | None = Depends(get_apple_music_client),
    bandcamp: BandcampClient = Depends(get_bandcamp_client),
    posthog_client: Posthog | None = Depends(get_posthog_client),
) -> StreamingCheckResponse:
    """Check streaming availability across all configured services."""
    # Per-request telemetry (LML#639). The cache-stats bracket and api_calls map
    # are seeded with a stable, all-zero shape: the streaming-check path is
    # application-cache-free and the clients record no API calls yet (LML#641).
    init_cache_stats()
    telemetry = RequestTelemetry(
        api_call_keys=list(_STREAMING_SERVICES),
        distinct_id="library-metadata-lookup-service",
        event_prefix="streaming_check",
    )

    try:
        with telemetry.track_step("availability"):
            response = await check_streaming_availability(
                request.artist,
                request.title,
                spotify=spotify,
                deezer=deezer,
                apple_music=apple_music,
                bandcamp=bandcamp,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Streaming check failed for %s - %s", request.artist, request.title)
        # Report the total failure before raising. The emit is itself swallowed,
        # so it can neither suppress nor alter the 500 (LML#639).
        _emit_summary(posthog_client, telemetry, None, error_type=type(e).__name__)
        raise HTTPException(status_code=500, detail="Streaming check failed") from e

    _emit_summary(posthog_client, telemetry, response)
    return response
