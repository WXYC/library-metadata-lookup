"""Spotify album-URL parsing: the album ID, and whether a URL is an album page.

Companion to ``release.apple_music_url_parser``: the streaming-URL cache
post-process (``lookup/streaming_url_postprocess.py``) mints
``entity.release_identity.spotify_album_id`` from a freshly-resolved Spotify
album URL, and the ``spotify_album`` registry entry's ``url_to_external_id``
extractor is :func:`spotify_album_id_from_url`.

The module answers two questions with one shared grammar (host + optional
locale segment + ``album/``), and they are deliberately not the same question:
:func:`spotify_album_id_from_url` is the strict extractor the mint path needs,
while :func:`url_is_spotify_album` (LML#1352) only asks whether the path is an
album *page* — see that function for why the 22-char floor must not be
imported into it.

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

from release.host_matching import host_matcher

# The album path prefix both questions share: the Spotify host, an optional
# web-player locale segment, and the ``album/`` path kind. ``//`` before the
# host pins the host boundary (rejecting ``open.spotify.com.evil.test``), and
# requiring ``album/`` to be the FIRST path segment after the optional locale
# keeps a later ``/album/`` (e.g. ``/user/x/album/y``) from matching.
#
# ``external_urls.spotify`` from the API is always the bare
# ``https://open.spotify.com/album/<id>`` shape, but the curated
# ``streaming_links`` artifact also holds hand-pasted web-player URLs carrying
# a locale prefix (24 rows: fr 7, it 6, es 6, de 3, pt 2). It is a presentation
# segment — the album ID after it is the same ID the bare URL carries — so both
# questions accept it. The hyphenated regional variant (``intl-pt-br``) mirrors
# the Apple parser's locale group.
_SPOTIFY_ALBUM_PATH_PREFIX = r"//open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2,4})?/)?album/"

# The ``(?![0-9A-Za-z])`` trailing guard pins the exact 22-char base62 ID
# length (rejecting 21- or 23-char IDs). Spotify album IDs are always 22
# base62 characters.
SPOTIFY_ALBUM_ID_RE = re.compile(
    _SPOTIFY_ALBUM_PATH_PREFIX + r"([0-9A-Za-z]{22})(?![0-9A-Za-z])",
)

# Path *kind* only: one character of anything that is not a path separator,
# query or fragment delimiter has to follow ``album/``, which rejects an
# id-less ``/album``, ``/album/`` and ``/album/?si=…`` while admitting any id
# shape. No 22-char floor — see :func:`url_is_spotify_album`.
SPOTIFY_ALBUM_PATH_RE = re.compile(_SPOTIFY_ALBUM_PATH_PREFIX + r"[^/?#]")


def spotify_album_id_from_url(url: str) -> str | None:
    """``open.spotify.com/album/<id>`` → ``<id>`` (22-char base62), else ``None``.

    Anchored on the Spotify host and strict on the 22-char ID shape. Returns
    ``None`` on a non-matching URL (track/artist path, wrong host, malformed
    ID) — the caller treats that as "URL did not carry an extractable
    album_id" rather than raising.
    """
    match = SPOTIFY_ALBUM_ID_RE.search(url)
    return match.group(1) if match else None


def url_is_spotify_album(url: str | None) -> bool:
    """True if ``url`` is a Spotify album *page* (``open.spotify.com/album/<id>``).

    The serve-side question (LML#1352), asked by
    ``lookup/enrichment/streaming_link_validation.py`` on the librarian-curated
    ``streaming_links`` override: is this URL in the album column actually an
    album?

    Stricter than :func:`url_has_spotify_host`, which admits every path kind
    Spotify serves — the production artifact's album column holds 6,143 artist
    pages, 989 tracks, 13 playlists, 7 user pages and a podcast that the host
    check passed.

    Looser than :func:`spotify_album_id_from_url` on exactly one axis, and on
    purpose: that extractor's 22-char base62 floor exists because the mint path
    keys ``entity.release_identity`` on what it returns, so a malformed ID must
    read as "no ID" rather than poison the entity graph. Nothing is keyed on
    this predicate's answer — it decides whether a URL is handed to a browser —
    so importing the floor would null a ``/album/<odd-id>`` value for a reason
    with no consequence at this seam. Path kind is the axis the artifact
    measurement has evidence on; ID charset is not.

    Guards ``None``/empty input by returning ``False``, like
    :func:`release.host_matching.is_well_formed_web_url`.
    """
    if not url:
        return False
    return SPOTIFY_ALBUM_PATH_RE.search(url) is not None


url_has_spotify_host = host_matcher(
    "spotify.com",
    doc="""True if ``url``'s host is ``spotify.com`` or a subdomain of it.

    Deliberately looser than :func:`spotify_album_id_from_url` — a
    field-name/host invariant check (LML#873) rather than an album-ID
    extraction, so it accepts any Spotify path (track, artist, playlist),
    not just the canonical album shape. Used to null out a mislabeled
    ``spotify_url`` artifact (a Deezer/Apple/Bandcamp URL stored under that
    field name) before it reaches a caller.

    Since LML#1352 that seam pairs this with :func:`url_is_spotify_album`,
    which supplies the path-kind test this predicate deliberately does not
    make: on its own it cannot tell an album page from an artist page.
    """,
)
