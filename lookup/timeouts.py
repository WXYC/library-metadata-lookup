"""Shared wall-clock helpers for the lookup hot path.

Per-call timeouts that bound a single upstream probe so a single 429/5xx
storm can't pin a request past its budget. Read at request time (not via
``Settings``) so the knobs can be tuned via Railway env vars without a
redeploy — mirrors the ``LML_BULK_MAX_CONCURRENT`` and
``LML_SEARCH_BUDGET_MS`` patterns.

Hoisted out of ``lookup/orchestrator.py`` so the in-line per-item Apple
Music probe (``lookup/orchestrator.py::enrich_artwork_results``) has a
single tunable source of truth for its wall-clock ceiling. The persistent
streaming-URL post-process (``lookup/streaming_url_postprocess.py``) no
longer shares this helper: since LML#573 it sources a per-service
``probe_timeout_s`` from ``STREAMING_URL_CACHE_CONFIG`` and applies it at
the gather level (preempting #594), so the two ceilings are now
independently tunable.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S = 4.0
"""Default wall-clock ceiling for a single Apple Music probe from the
lookup hot path. Sized to the legacy iTunes Search ``httpx`` timeout (5s)
with margin so the orchestrator's ``asyncio.wait_for`` trips before the
underlying ``BaseStreamingClient`` httpx timeout. LML#449 + LML#450."""

_APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR = "LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS"


def apple_music_lookup_timeout_s() -> float:
    """Per-call wall-clock ceiling for a single Apple Music probe.

    Read at request time (not via ``Settings``) so the knob can be tuned
    via Railway env vars without a redeploy — mirrors the
    ``LML_BULK_MAX_CONCURRENT`` and ``LML_SEARCH_BUDGET_MS`` patterns.

    A misconfigured value (negative, zero, or unparseable) falls back to
    the default with a WARN. The default is
    ``_APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S`` (4s). See LML#449 + LML#450.
    """
    raw = os.getenv(_APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR)
    if not raw:
        return _APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S
    try:
        ms = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r, falling back to %.1fs",
            _APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR,
            raw,
            _APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S,
        )
        return _APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S
    if ms <= 0:
        logger.warning(
            "%s=%d must be > 0, falling back to %.1fs",
            _APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR,
            ms,
            _APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S,
        )
        return _APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S
    return ms / 1000.0
