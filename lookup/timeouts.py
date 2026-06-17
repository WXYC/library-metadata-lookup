"""Shared wall-clock helpers for the lookup hot path.

Per-call timeouts that bound a single upstream probe so a single 429/5xx
storm can't pin a request past its budget. Read at request time (not via
``Settings``) so the knobs can be tuned via Railway env vars without a
redeploy — mirrors the ``LML_BULK_MAX_CONCURRENT`` and
``LML_SEARCH_BUDGET_MS`` patterns.

``probe_timeout_s_from_env`` is the shared resolver: the in-line per-item
Apple probe (``lookup/orchestrator.py::enrich_artwork_results``) uses it via
the ``apple_music_lookup_timeout_s`` wrapper, and the persistent streaming-URL
post-process (``lookup/streaming_url_postprocess.py``) uses it too — its
``STREAMING_URL_CACHE_CONFIG`` Apple entry carries ``timeout_env_var =
APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR``, so ``LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS``
tunes BOTH Apple call sites from one knob (LML#573 preserved this back-compat).
The post-process applies a per-service ceiling at the gather level (preempting
#594): services without a ``timeout_env_var`` (e.g. Spotify) use the static
``probe_timeout_s`` on their registry entry; Apple's is env-overridable.
"""

from __future__ import annotations

from core.search import resolve_positive_int_env

_APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S = 4.0
"""Default wall-clock ceiling for a single Apple Music probe from the
lookup hot path. Sized to the legacy iTunes Search ``httpx`` timeout (5s)
with margin so the orchestrator's ``asyncio.wait_for`` trips before the
underlying ``BaseStreamingClient`` httpx timeout. LML#449 + LML#450."""

APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR = "LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS"
"""Public so the streaming-URL post-process registry can wire it onto the
Apple cache entry's ``timeout_env_var`` (LML#573), keeping this knob tuning
BOTH the in-line probe and the post-process probe of the same Apple client."""


def probe_timeout_s_from_env(env_var: str, default_s: float) -> float:
    """Resolve a per-call wall-clock ceiling (seconds) from an integer-ms env var.

    Read at request time (not via ``Settings``) so the knob can be tuned via
    Railway env vars without a redeploy — mirrors the ``LML_BULK_MAX_CONCURRENT``
    and ``LML_SEARCH_BUDGET_MS`` patterns. Delegates the integer parse +
    unparseable/zero/negative fallback (with WARN) to the shared
    ``core.search.resolve_positive_int_env`` so there is one env-int policy,
    then converts the ms value to seconds.
    """
    return resolve_positive_int_env(env_var, round(default_s * 1000)) / 1000.0


def apple_music_lookup_timeout_s() -> float:
    """Per-call wall-clock ceiling for a single Apple Music probe.

    Thin back-compat wrapper over ``probe_timeout_s_from_env`` for the in-line
    per-item Apple probe (``lookup/orchestrator.py``). The default is 4s. See
    LML#449 + LML#450.
    """
    return probe_timeout_s_from_env(
        APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, _APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S
    )
