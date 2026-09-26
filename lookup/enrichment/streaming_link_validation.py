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
``soundcloud_url``. ``spotify_url`` / ``apple_music_url`` do not get it: review
found that nulling a value here the old host check alone would have accepted
changes behavior well outside this module — a ``spotify_url`` that comes out
``None`` becomes eligible for ``lookup/streaming_url_postprocess.py``'s
cache-UPSERT / mint leg, and an ``apple_music_url`` that comes out ``None``
flips ``skip_happy_probe`` off in ``item.py``, spending an Apple Music quota
slot + wall-clock on a live probe.

**LML#1352 deliberately reverses that declination for ``spotify_url``, on one
axis: the path kind.** The field must now name a release — an album or a track
page (``url_is_spotify_album_or_track``; that function documents why the track
kind counts) — not merely be a URL on a Spotify host. A 2026-09-25 pull of the
artifact found ~6.2k of its 46,907 populated ``spotify_url`` values pointing at
an artist, playlist, user or podcast page, or at an id-less ``/album``, and 100%
of the artist-shaped ones carry no match provenance at all
(``spotify_matched_artist`` holds a strategy name like ``backfill-wiki
(spotify)``) — so the guard removes one April-2026 campaign that resolved
*artists* into an album column and touches nothing properly matched. Per-shape
counts are pinned as the accept/reject table in
``tests/unit/test_streaming_link_validation.py``.

Serving those was not a soft failure: ``item.py``'s ``_slot_urls`` /
``_RESOLUTION_PROVING_URL_SERVICES`` force ``streaming_status.spotify =
"verified"`` on any non-null Spotify slot, so an artist page was labelled a
confirmed album match, and ``verified`` is terminal. Which makes the
consequence the LML#1295 review weighed as a cost the *point* for this field:
a suppressed ``spotify_url`` falls through to the post-process's cache-UPSERT /
mint leg, which resolves an album page for the REQUEST's (artist, album) and
whose verdict a later leg can still supersede.

Three scope facts, all routed on #1352 rather than handled here, because each
lives at another layer: LML#1352's *reported* Mob/Money value is album-shaped
(the wrong album, right shape), so the path axis does not close it — that is
its criterion 1's identity agreement; the fallthrough above does not run on
``/lookup/bulk``, where ``should_suppress_streaming_warm()`` makes the
post-process cache-read-only and the rowless warm exemption does not cover
library rows; and it heals the response, not the rows Backend-Service already
stored as ``verified``, whose merge treats that status as terminal too.

``apple_music_url`` stays host-check-only, by measurement rather than
oversight: all 288 of its populated artifact values are already album URLs, so
this guard would have nothing to do there, and nulling one would flip
``skip_happy_probe`` off. Neither field gets the well-formedness floor — path
kind is a different check with a different justification, not that floor
arriving by the back door.

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
from release.spotify_url_parser import url_has_spotify_host, url_is_spotify_album_or_track
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
    # spotify_url / apple_music_url: no well-formedness floor (see the module
    # docstring). A falsy input passes through unchanged — the item.py
    # update-dict `or None` coerces it later, and nulling it here would be an
    # out-of-band change to that seam.
    if not url:
        return url
    if not host_check(url):
        return None
    # LML#1352: spotify_url alone also has to name a RELEASE. Spelled as a
    # conditional rather than a per-field table — a second field wanting a
    # path-kind check would want a different question asked of it, and the
    # three well-formedness fields structurally cannot reach this line.
    if field == "spotify_url" and not url_is_spotify_album_or_track(url):
        return None
    return url


def validate_streaming_link_urls(links: dict[str, str | None]) -> dict[str, str | None]:
    """Suppress-to-null every ``streaming_links`` URL field that fails validation.

    ``links`` is the dict ``library.db.LibraryDB.get_streaming_links`` returns
    (a missing key reads the same as an explicit ``None``). Returns a dict
    with the same five keys, each either the original URL (unchanged) or
    ``None`` (see the module docstring for which checks apply per field —
    a host check on all five, a well-formedness floor on three, and a
    release-path-kind check on ``spotify_url``).
    """
    return {
        field: _validate(field, links.get(field), host_check)
        for field, host_check in _FIELD_HOST_CHECKS.items()
    }
