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

from release.host_matching import host_matcher
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


url_has_bandcamp_host = host_matcher(
    "bandcamp.com",
    doc="""True if ``url``'s host is ``bandcamp.com`` or a subdomain of it.

    Deliberately looser than :func:`bandcamp_album_id_from_url` — a
    field-name/host invariant check (LML#873/LML#1295) rather than an
    album-ID extraction, so it accepts any Bandcamp path, not just the
    canonical album shape. This is the LML#1295 seam's own field
    (``lookup/enrichment/item.py``'s ``streaming_links.bandcamp_url``,
    a librarian-curated column where a 2026-08-11 audit of the 2,800-row
    table found zero rows off ``bandcamp.com``) — NOT the same question as
    the Backend-Service boundary guard's (BS#2351), which also sees
    probe/cache-resolved custom-domain Bandcamp deep-links this validator
    never touches and drops its own host allowlist for that reason. Used to
    null out a mislabeled ``bandcamp_url`` artifact (a Deezer/Spotify/Apple
    URL stored under that field name) before it reaches a caller.
    """,
)
