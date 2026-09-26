"""Spotify URL parsing: the album ID, and whether a URL names a release at all.

Companion to ``release.apple_music_url_parser``: the streaming-URL cache
post-process (``lookup/streaming_url_postprocess.py``) mints
``entity.release_identity.spotify_album_id`` from a freshly-resolved Spotify
album URL, and the ``spotify_album`` registry entry's ``url_to_external_id``
extractor is :func:`spotify_album_id_from_url`.

:func:`url_is_spotify_album_or_track` (LML#1352) is a second, deliberately
different question, asked at the serve seam rather than the mint path — see its
docstring for the axes on which it is looser, and why.

This is intentionally *stricter* than the loose
``release.orchestrator._spotify_album_id_from_url`` (regex ``[A-Za-z0-9]+``)
that fed the release-resolve endpoint's identifier merge before that
endpoint was removed (WXYC/wiki#87). The cache/mint
path keys the entity graph on the extracted ID, so the parser enforces the
canonical 22-char base62 Spotify album-ID shape and surfaces a malformed ID
as ``None`` — the post-process then skips the mint (the same defense-in-depth
posture the Apple parser's ``\\d{6,}`` floor provides against
``music.apple.com`` slugs).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from release.host_matching import host_matcher

# Anchored on the Spotify host so a ``/album/<id>`` path on another domain
# can't be mistaken for a Spotify album. ``//`` before the host and the
# ``(?![0-9A-Za-z])`` trailing guard pin the host boundary (rejecting
# ``open.spotify.com.evil.test``) and the exact 22-char base62 ID length
# (rejecting 21- or 23-char IDs) respectively. Spotify album IDs are always
# 22 base62 characters; ``external_urls.spotify`` from the API is always the
# bare ``https://open.spotify.com/album/<id>`` shape (no ``intl-xx`` segment).
SPOTIFY_ALBUM_ID_RE = re.compile(
    r"//open\.spotify\.com/album/([0-9A-Za-z]{22})(?![0-9A-Za-z])",
)

# Matched against ``urlparse(url).path`` and anchored at the path root, so a
# release kind reached later (``/user/x/album/y``) or carried inside a query
# string cannot pass for a release page. The optional leading segment is the
# web player's locale prefix — ``intl-de``, or the hyphenated regional variant
# ``intl-pt-br``, mirroring the Apple parser's locale group — which the curated
# ``streaming_links`` artifact carries on hand-pasted URLs (24 rows: fr 7, it 6,
# es 6, de 3, pt 2) and which names the same page. One character of
# non-separator has to follow the kind, rejecting an id-less ``/album``,
# ``/album/`` and ``/album/?si=…`` while admitting any id shape.
#
# Case-sensitive on both legs, unlike the Apple parser's ``re.IGNORECASE``:
# Spotify's routes are case-sensitive and every measured locale row is
# lowercase, so admitting ``/ALBUM/<id>`` would gate in a shape that 404s —
# the mirror image of dropping one that resolves. Host is not in this pattern
# at all: :func:`url_is_spotify_album_or_track` asks
# :func:`url_has_spotify_host` for that leg instead of carrying a second, and
# narrower, host literal.
_SPOTIFY_RELEASE_PATH_RE = re.compile(
    r"^/(?:intl-[a-z]{2}(?:-[a-z]{2,4})?/)?(?:album|track)/[^/]",
)


def spotify_album_id_from_url(url: str) -> str | None:
    """``open.spotify.com/album/<id>`` → ``<id>`` (22-char base62), else ``None``.

    Anchored on the Spotify host and strict on the 22-char ID shape. Returns
    ``None`` on a non-matching URL (track/artist path, wrong host, malformed
    ID) — the caller treats that as "URL did not carry an extractable
    album_id" rather than raising.
    """
    match = SPOTIFY_ALBUM_ID_RE.search(url)
    return match.group(1) if match else None


url_has_spotify_host = host_matcher(
    "spotify.com",
    doc="""True if ``url``'s host is ``spotify.com`` or a subdomain of it.

    Deliberately looser than :func:`spotify_album_id_from_url` — a
    field-name/host invariant check (LML#873) rather than an album-ID
    extraction, so it accepts any Spotify path (track, artist, playlist),
    not just the canonical album shape. Used to null out a mislabeled
    ``spotify_url`` artifact (a Deezer/Apple/Bandcamp URL stored under that
    field name) before it reaches a caller.

    Since LML#1352 that seam pairs this with
    :func:`url_is_spotify_album_or_track`, which supplies the path-kind test
    this predicate deliberately does not make: on its own it cannot tell a
    release page from an artist page.
    """,
)


def url_is_spotify_album_or_track(url: str | None) -> bool:
    """True if ``url`` is a Spotify page that names a release: album or track.

    The serve-side question (LML#1352), asked by
    ``lookup/enrichment/streaming_link_validation.py`` on the librarian-curated
    ``streaming_links`` override: does this URL in the album column name the
    release at all, or is it an artist / playlist / user / podcast page that
    cannot?

    **Track pages count.** A track page names a recording on the release, and
    ``scripts/export_streaming_links.py`` deliberately supplements
    ``spotify_url`` from ``track_results`` (gated on ``resolution_status`` in
    ``local_match``/``api_match``) for singles and compilations — and only when
    the album-level URL is absent, so it is that release's ONLY Spotify link.
    Rejecting the kind would leave those releases with no link at all, LML#573
    having removed the templated Spotify search fallback.

    Stricter than :func:`url_has_spotify_host` on the path and no stricter on
    the host: it reuses that predicate for the host leg, so every Spotify host
    the seam already admits keeps working and only the path kind decides. The
    host check alone admits every path kind Spotify serves, and the artifact's
    album column measurably holds artist, playlist, user and podcast pages.

    Looser than :func:`spotify_album_id_from_url` on three axes, all on
    purpose. That extractor's 22-char base62 floor exists because the mint path
    keys ``entity.release_identity`` on what it returns, so a malformed ID must
    read as "no ID" rather than poison the entity graph; nothing is keyed on
    this predicate's answer — it decides whether a URL is handed to a browser —
    so importing the floor would null a ``/album/<odd-id>`` value for a reason
    with no consequence at this seam. Path kind is the axis the artifact census
    has evidence on; ID charset is not. The locale prefix the artifact's
    hand-pasted URLs carry is in this grammar but not the extractor's, which
    only ever sees a live-resolved ``external_urls.spotify``. And the track
    kind is here for the reason above and is not mintable, so the extractor
    must keep rejecting it.

    Guards ``None``/empty input by returning ``False``, like
    :func:`release.host_matching.is_well_formed_web_url`.
    """
    if not url or not url_has_spotify_host(url):
        return False
    path = urlparse(url).path
    if ".." in path.split("/"):
        # RFC 3986 dot-segment removal pops the kind segment before a browser
        # requests the URL, so the path this pattern would read is not the path
        # that gets fetched: ``/album/../artist/<id>`` resolves to the working
        # artist page. Same bypass class as an album path in the query string.
        return False
    return _SPOTIFY_RELEASE_PATH_RE.match(path) is not None
