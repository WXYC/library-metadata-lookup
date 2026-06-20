"""Bandcamp album-URL canonicalization.

Companion to ``release.apple_music_url_parser`` and
``release.spotify_url_parser``: the streaming-URL cache post-process
(``lookup/streaming_url_postprocess.py``) mints
``entity.release_identity.bandcamp_album_url`` from a freshly-resolved Bandcamp
album URL, and the ``bandcamp`` registry entry's ``url_to_external_id``
extractor is this function.

Unlike Apple / Spotify — whose external_id is a numeric / base62 ID extracted
*from* the URL — Bandcamp has no opaque ID: the canonical album URL itself is
the identity (the ``bandcamp_album_url TEXT UNIQUE`` column). So this returns
the *canonical URL* (lowercased host, trailing slash stripped, query / fragment
dropped) rather than a sub-string ID, and surfaces a non-Bandcamp / malformed
URL as ``None`` so the post-process skips the mint.

Parsing is delegated to ``release.url_parser.parse_url`` — the same parser the
identity validator (``identity.release_validation._validate_bandcamp_album_url``)
canonicalizes against — so the form this emits round-trips through the validator
without drift.
"""

from __future__ import annotations

from release.url_parser import parse_url


def bandcamp_album_id_from_url(url: str) -> str | None:
    """``<artist>.bandcamp.com/album/<slug>`` → canonical album URL, else ``None``.

    Returns the canonical Bandcamp album URL (the entity's external_id) on a
    valid album URL, or ``None`` for a non-Bandcamp host, a non-album path, or
    malformed input — the caller treats ``None`` as "URL did not carry an
    extractable Bandcamp identity" and skips the mint rather than raising.
    """
    parsed = parse_url(url)
    if parsed is None or parsed.source != "bandcamp":
        return None
    return parsed.identifier
