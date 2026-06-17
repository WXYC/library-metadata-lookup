"""Spotify album-URL ID extraction.

Companion to ``release.apple_music_url_parser``: the streaming-URL cache
post-process (``lookup/streaming_url_postprocess.py``) mints
``entity.release_identity.spotify_album_id`` from a freshly-resolved Spotify
album URL, and the ``spotify_album`` registry entry's ``url_to_external_id``
extractor is this function.

This is intentionally *stricter* than the loose
``release.orchestrator._spotify_album_id_from_url`` (regex ``[A-Za-z0-9]+``)
that feeds the release-resolve endpoint's identifier merge. The cache/mint
path keys the entity graph on the extracted ID, so the parser enforces the
canonical 22-char base62 Spotify album-ID shape and surfaces a malformed ID
as ``None`` — the post-process then skips the mint (the same defense-in-depth
posture the Apple parser's ``\\d{6,}`` floor provides against
``music.apple.com`` slugs).
"""

from __future__ import annotations

import re

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


def spotify_album_id_from_url(url: str) -> str | None:
    """``open.spotify.com/album/<id>`` → ``<id>`` (22-char base62), else ``None``.

    Anchored on the Spotify host and strict on the 22-char ID shape. Returns
    ``None`` on a non-matching URL (track/artist path, wrong host, malformed
    ID) — the caller treats that as "URL did not carry an extractable
    album_id" rather than raising.
    """
    match = SPOTIFY_ALBUM_ID_RE.search(url)
    return match.group(1) if match else None
