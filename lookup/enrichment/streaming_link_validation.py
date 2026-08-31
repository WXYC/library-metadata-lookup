"""Validate ``streaming_links`` URL fields before they reach a response (LML#1295).

The Backend-Service wire-golden production audit (2026-08-11, WXYC/Backend-Service#1710
and #2345) observed wrong-shaped values read straight out of the librarian-curated
``streaming_links`` artifact — scheme-relative, bare-host, and whitespace-corrupted
URLs — flowing unvalidated into lookup responses. ``spotify_url`` / ``apple_music_url``
already had a *host* check at the ``item.py`` seam (LML#873, guarding against a
mislabeled URL stored under the wrong field); this module is layer 3 of the
WXYC/wxyc-shared#428 decision — the one that stops a malformed value at the source,
before any consumer (Backend-Service's own boundary guard is layer 2,
WXYC/Backend-Service#2350) sees it.

Suppress-to-null only: a field that fails validation becomes ``None`` and falls
through the existing ``lookup/enrichment/search_urls.py`` fallback, so the response
still carries a usable URL. No rewrite of the persisted ``streaming_links`` row.

``bandcamp_url`` gets ``is_well_formed_web_url`` but no host allowlist. Unlike the
other four services, a Bandcamp album can be served from a label's custom domain
rather than ``<artist>.bandcamp.com`` — the same caveat WXYC/Backend-Service#2350's
client-boundary guard adopts, so this is deliberately the one field where "well-formed"
is the floor rather than "well-formed and the right host".
"""

from __future__ import annotations

from collections.abc import Callable

from release.apple_music_url_parser import url_has_apple_music_host
from release.host_matching import is_well_formed_web_url
from release.soundcloud_url_parser import url_has_soundcloud_host
from release.spotify_url_parser import url_has_spotify_host
from release.youtube_music_url_parser import url_has_youtube_music_host

#: Per-field host check, ``None`` for the one field (Bandcamp) that gets
#: well-formedness only. Order matches the ``streaming_links`` column order.
_FIELD_HOST_CHECKS: dict[str, Callable[[str], bool] | None] = {
    "spotify_url": url_has_spotify_host,
    "apple_music_url": url_has_apple_music_host,
    "youtube_music_url": url_has_youtube_music_host,
    "bandcamp_url": None,
    "soundcloud_url": url_has_soundcloud_host,
}


def _validate(url: str | None, host_check: Callable[[str], bool] | None) -> str | None:
    if not url or not is_well_formed_web_url(url):
        return None
    if host_check is not None and not host_check(url):
        return None
    return url


def validate_streaming_link_urls(links: dict[str, str | None]) -> dict[str, str | None]:
    """Suppress-to-null every ``streaming_links`` URL field that fails validation.

    ``links`` is the dict ``library.db.LibraryDB.get_streaming_links`` returns
    (a missing key reads the same as an explicit ``None``). Returns a dict with
    the same five keys, each either the original URL (unchanged) or ``None``.
    """
    return {
        field: _validate(links.get(field), host_check)
        for field, host_check in _FIELD_HOST_CHECKS.items()
    }
