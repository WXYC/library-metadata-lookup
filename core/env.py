"""Shared environment-variable readers for the repo's no-redeploy Railway levers.

Deliberately a LEAF module: stdlib imports only, no repo imports at all. The
boolean reader below started life beside ``resolve_positive_int_env`` in
``core/search.py`` (LML#1204 item 3), which imports ``discogs.service`` at
module scope — so every adopter dragged the whole Discogs stack in just to
read a bool, and ``discogs/*`` could never adopt it at all
(``discogs.ratelimit -> core.search -> discogs.service -> discogs.ratelimit``
is a real cycle). A generic env reader has to sit below everything that might
want one; ``config/settings.py`` and ``core/bulk_concurrency.py`` already defer
their ``core.search`` imports to function scope to dodge exactly this.

``resolve_positive_int_env`` stays in ``core/search.py`` for now — it predates
this module and has a wider blast radius; moving it is a separate change.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# The one boolean-env accept vocabulary (LML#1204 item 3): symmetric pairs
# (1/0, true/false, yes/no, on/off, enabled/disabled), replacing three
# per-module _TRUE_FLAG_VALUES copies and lookup/artist_resolution.py's
# inverse-polarity _FALSE_FLAG_VALUES — whose accept-set was asymmetric
# ("disabled" disabled a default-ON flag but "enabled" did NOT enable a
# default-OFF one). tests/unit/test_core_env.py pins the pairing structurally.
_TRUE_BOOL_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSE_BOOL_ENV_VALUES: frozenset[str] = frozenset({"0", "false", "no", "off", "disabled"})

_warned_bool_env_vars: set[str] = set()
"""Env-var names whose unrecognized spelling has already been warned about —
:func:`resolve_bool_env`'s adopters sit on per-request/per-item hot paths
(the enrichment coordinator, per-item probes, the admission gate), so a
typo'd Railway var must warn once per process, not once per read (the
``_warned_prefixes`` idiom ``wxyc_fastapi.observability.posthog
.get_posthog_client`` uses). Process-lifetime on purpose: the flags are
no-redeploy levers, but a fixed var stops hitting the warn path at all, and
a still-broken one doesn't need re-announcing."""


def resolve_bool_env(env_var: str, *, default: bool) -> bool:
    """Read a boolean flag from ``env_var``, falling back to ``default``.

    The shared reader for the repo's no-redeploy Railway boolean levers
    (read per-call, no ``Settings`` indirection, so tests monkeypatch the
    env var and operators flip without a restart), with the same
    operator-typo contract as ``core.search.resolve_positive_int_env``: an
    unrecognized spelling falls back to ``default`` with a WARN (once per
    process per env var — see :data:`_warned_bool_env_vars`) — it must not
    500 anything, and must not silently flip a flag in either direction.
    Case-insensitive, whitespace-trimmed; empty behaves like unset, matching
    every replaced per-module copy. Each call site keeps its own default
    polarity — this function unifies only the accept vocabulary.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if not normalized:
        return default
    if normalized in _TRUE_BOOL_ENV_VALUES:
        return True
    if normalized in _FALSE_BOOL_ENV_VALUES:
        return False
    if env_var not in _warned_bool_env_vars:
        _warned_bool_env_vars.add(env_var)
        logger.warning(
            "%s=%r is not a recognized boolean spelling; falling back to %s", env_var, raw, default
        )
    return default
