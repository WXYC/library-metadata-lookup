"""Orchestrates streaming availability checks across multiple services."""

from __future__ import annotations

import asyncio
import logging

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.deezer import DeezerClient
from clients.streaming.spotify import SpotifyClient
from streaming.models import StreamingCheckResponse, StreamingCheckSources

logger = logging.getLogger(__name__)


async def check_streaming_availability(
    artist: str,
    title: str,
    *,
    spotify: SpotifyClient | None = None,
    deezer: DeezerClient | None = None,
    apple_music: AppleMusicClient | None = None,
    bandcamp: BandcampClient | None = None,
) -> StreamingCheckResponse:
    """Check streaming availability for an artist+title across all configured services.

    Runs every configured service concurrently via the
    ``BaseStreamingClient.find_album_match`` seam (LML#392) — the orchestrator
    never branches on service identity, so adding a fifth provider means
    adding an adapter, not editing here. Verdict matrix (LML#376):

    - ``on_streaming=True`` when any service confirmed a match (positive evidence wins
      even if other services errored).
    - ``on_streaming=False`` strictly when every dispatched service was checked AND
      none found a match AND none errored.
    - ``on_streaming=None`` when no services were dispatched, or when at least one
      service raised an exception without positive evidence elsewhere — callers
      should treat this as "do not persist" / "retry later".

    Args:
        artist: Artist name to search for.
        title: Album title to search for.
        spotify: Optional Spotify client.
        deezer: Optional Deezer client.
        apple_music: Optional Apple Music client.
        bandcamp: Optional Bandcamp client.

    Returns:
        ``StreamingCheckResponse`` with the verdict, per-source match details, and
        ``errored_sources`` listing services whose check raised (sorted; empty when
        every dispatched check completed without raising). The errored set is
        independent of the verdict — a service can match while others error.
    """
    # The kwarg names (spotify / deezer / apple_music / bandcamp) double as
    # `StreamingCheckSources` field names — the response shape is API-locked,
    # so the gather loop maps name-to-client uniformly without branching.
    clients = {
        "spotify": spotify,
        "deezer": deezer,
        "apple_music": apple_music,
        "bandcamp": bandcamp,
    }
    tasks: dict[str, asyncio.Task] = {
        name: asyncio.create_task(client.find_album_match(artist, title))
        for name, client in clients.items()
        if client is not None
    }

    sources = StreamingCheckSources()
    errored: set[str] = set()
    any_checked = False
    any_found = False

    for service, task in tasks.items():
        try:
            match = await task
            any_checked = True
            if match is not None:
                any_found = True
                setattr(sources, service, match)
        except Exception:
            errored.add(service)
            logger.exception("Streaming check failed for %s on %s - %s", service, artist, title)

    # LML#376: tighten False's meaning so partial-failures can't masquerade as
    # confirmed absences. Positive evidence still wins (a confirmed match elsewhere
    # is reason to persist True even if a flake errored), but absent positive
    # evidence, any error escalates the verdict to None — Backend's `!== null`
    # guard then refuses to write through. `False` now means strictly "every
    # dispatched service was checked AND none found a match AND none errored".
    # `errored_sources` carries the partial-failure signal for selective retry.
    if any_found:
        on_streaming: bool | None = True
    elif errored:
        on_streaming = None
    elif any_checked:
        on_streaming = False
    else:
        on_streaming = None

    return StreamingCheckResponse(
        on_streaming=on_streaming,
        sources=sources,
        errored_sources=sorted(errored),
    )
