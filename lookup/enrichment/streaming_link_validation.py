"""Validate ``streaming_links`` URL fields before they reach a response (LML#1295).

The Backend-Service wire-golden production audit (2026-08-11, WXYC/Backend-Service#1710
and #2345) observed wrong-shaped values read straight out of the librarian-curated
``streaming_links`` artifact — scheme-relative, bare-host, and control-character-corrupted
URLs — flowing unvalidated into lookup responses. ``spotify_url`` / ``apple_music_url``
already had a *host* check at the ``item.py`` seam (LML#873, guarding against a
mislabeled URL stored under the wrong field); this module is layer 3 of the
WXYC/wxyc-shared#428 decision — the one that stops a malformed value at the source,
before any consumer (Backend-Service's own boundary guard is layer 2,
WXYC/Backend-Service#2351) sees it.

Suppress-to-null only: a field that fails validation becomes ``None`` and falls
through the downstream fallback (a search-URL template, a post-process
cache/probe leg, or nothing — see ``item.py``'s update dict for the per-field
consequence). No rewrite of the persisted ``streaming_links`` row.

**The well-formedness floor (``is_well_formed_web_url``) applies to exactly
the three fields LML#1295 added** — ``youtube_music_url``, ``bandcamp_url``,
``soundcloud_url``. ``spotify_url`` / ``apple_music_url`` pre-date this ticket
(LML#873) and keep their *exact* pre-LML#1295 semantics, a host check and
nothing else: review found that nulling a value here the old host check alone
would have accepted changes behavior well outside this module — a
``spotify_url`` that comes out ``None`` becomes eligible for
``lookup/streaming_url_postprocess.py``'s cache-UPSERT / mint leg, and an
``apple_music_url`` that comes out ``None`` flips ``skip_happy_probe`` off in
``item.py``, spending an Apple Music quota slot + wall-clock on a live probe.
Both are out-of-scope L1/cache-semantics changes, not this validator's call.

**All five fields get a per-service host check** — the coordinated cross-PR
split with Backend-Service's own boundary guard (BS#2351) is per-seam, not a
blanket rule. This seam reads the curated ``streaming_links`` column, where a
2026-08-11 audit found zero of 2,800 Bandcamp rows off ``bandcamp.com``, so
``bandcamp_url`` gets the same host check the other four get. BS#2351 is
dropping ITS bandcamp allowlist to well-formedness-only because it also sees
probe/cache-resolved custom-domain deep-links this validator never touches —
the two guards disagree on Bandcamp because they see different URL
populations, not because one of them is wrong.
"""

from __future__ import annotations

from collections.abc import Callable

from release.apple_music_url_parser import url_has_apple_music_host
from release.bandcamp_url_parser import url_has_bandcamp_host
from release.host_matching import is_well_formed_web_url
from release.soundcloud_url_parser import url_has_soundcloud_host
from release.spotify_url_parser import url_has_spotify_host
from release.youtube_music_url_parser import url_has_youtube_music_host

#: Per-field host check. Order matches ``lookup/enrichment/item.py``'s own
#: variable declaration order for these five fields, NOT the
#: ``streaming_links`` SQLite column order (``library/db.py``'s
#: ``get_streaming_links``: ``spotify_url, apple_music_url, deezer_url,
#: bandcamp_url, tidal_url, youtube_music_url, soundcloud_url`` — it
#: interleaves two columns this validator never sees).
_FIELD_HOST_CHECKS: dict[str, Callable[[str], bool]] = {
    "spotify_url": url_has_spotify_host,
    "apple_music_url": url_has_apple_music_host,
    "youtube_music_url": url_has_youtube_music_host,
    "bandcamp_url": url_has_bandcamp_host,
    "soundcloud_url": url_has_soundcloud_host,
}

#: The three fields LML#1295 added the well-formedness floor to.
#: ``spotify_url`` / ``apple_music_url`` pre-date this ticket (LML#873) and
#: are deliberately excluded — see the module docstring for why a new floor
#: on those two is an out-of-scope behavior change, not a stricter check.
_WELL_FORMEDNESS_FIELDS = frozenset({"youtube_music_url", "bandcamp_url", "soundcloud_url"})


def _validate(field: str, url: str | None, host_check: Callable[[str], bool]) -> str | None:
    if field in _WELL_FORMEDNESS_FIELDS:
        if not url or not is_well_formed_web_url(url):
            return None
        return url if host_check(url) else None
    # spotify_url / apple_music_url: host-check only, byte-identical to the
    # pre-LML#1295 (LML#873) behavior — a falsy input passes through
    # unchanged (the item.py update-dict `or None` coerces it later), and
    # only a present-but-wrong-host value is nulled.
    if url and not host_check(url):
        return None
    return url


def validate_streaming_link_urls(links: dict[str, str | None]) -> dict[str, str | None]:
    """Suppress-to-null every ``streaming_links`` URL field that fails validation.

    ``links`` is the dict ``library.db.LibraryDB.get_streaming_links`` returns
    (a missing key reads the same as an explicit ``None``). Returns a dict
    with the same five keys, each either the original URL (unchanged) or
    ``None`` (see the module docstring for which check applies per field).
    """
    return {
        field: _validate(field, links.get(field), host_check)
        for field, host_check in _FIELD_HOST_CHECKS.items()
    }
