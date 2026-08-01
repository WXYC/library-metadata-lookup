"""Shared template for "service unavailable" 503 detail strings (LML#1036).

Five-plus per-service 503 detail constants had drifted into independent
wording across ``identity/dependencies.py``, ``cache/router.py``,
``discogs/router.py``, and ``artists/router.py`` -- e.g. "Entity store is not
available. Ensure DATABASE_URL_DISCOGS is configured and the entity schema
has been applied." vs "Discogs cache is not available. Ensure
DATABASE_URL_DISCOGS is configured." Four of those five share one shape --
"<Service> is not available. <Remedy>" -- differing only in which service and
which remedy sentence. This module gives that shape a single build site so a
future service's 503 stays on the same tone instead of inventing new
phrasing.

Consolidates CONSTRUCTION, not CONTENT: every existing detail string built
here is byte-identical to what shipped before this refactor -- see each call
site's own comment for the literal it reproduces. One holdout,
``discogs/router.py``'s legacy ``_require_service`` detail ("Discogs service
is not configured. Set DISCOGS_TOKEN environment variable."), uses a
different verb ("not configured" vs "not available") and doesn't fit this
shape; it stays a hand-written literal there rather than being forced through
this template at the cost of changing its wire bytes.

Layering: this module has no imports of its own (a pure string builder), so
it is safe for any layer -- ``core/``, ``identity/``, or a router module -- to
import.
"""

from __future__ import annotations


def service_unavailable_detail(service: str, remedy: str) -> str:
    """Build a "<service> is not available. <remedy>" 503 detail string.

    ``service`` should read naturally mid-sentence (e.g. ``"Discogs cache"``,
    ``"Entity store"``); ``remedy`` is a complete sentence, including its
    trailing period (e.g. ``"Set DATABASE_URL_DISCOGS."``).
    """
    return f"{service} is not available. {remedy}"
