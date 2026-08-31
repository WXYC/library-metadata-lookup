"""YouTube Music host predicate.

Sibling to ``release.apple_music_url_parser`` and ``release.spotify_url_parser``,
added for LML#1295 (extending the LML#873 ``streaming_links`` field-name/host
invariant check — a mislabeled URL stored under a service's own column — to
YouTube Music). Unlike those two, YouTube Music has no album-ID extractor:
``lookup/streaming_url_registry.py``'s entry sets ``url_to_external_id=None``
because a resolved ``music.youtube.com/browse/<id>`` browse ID has no identity
column to mint (LML#1103), so there is no strict extractor for this module to
pair the host check with.

The predicate is two combined ``host_matcher`` checks (``youtube.com`` and
``youtu.be``) rather than one, so it accepts the ``youtu.be`` short-link host
alongside the canonical domain — matching the vocabulary Backend-Service's
own boundary guard (BS#2351) adopts for this field.
"""

from __future__ import annotations

from release.host_matching import host_matcher

_url_has_youtube_com_host = host_matcher("youtube.com")
_url_has_youtu_be_host = host_matcher("youtu.be")


def url_has_youtube_music_host(url: str | None) -> bool:
    """True if ``url``'s host is ``youtube.com``, ``youtu.be``, or a subdomain of either.

    Covers ``music.youtube.com`` (the canonical album/browse host), bare
    ``youtube.com`` / ``www.youtube.com`` — mirroring
    ``url_has_apple_music_host``'s apex-domain looseness — and the ``youtu.be``
    short-link host, which Backend-Service's boundary guard (BS#2351) also
    accepts for this field so the two vocabularies match. Used to null out a
    mislabeled ``youtube_music_url`` artifact before it reaches a caller. YTM
    search-URL degradation from an over-eager reject is durable (BS#1747 —
    Backend-Service stops re-asking once an album's URL fields are non-null),
    so this predicate stays as permissive as the field-name/host invariant
    check allows.
    """
    return _url_has_youtube_com_host(url) or _url_has_youtu_be_host(url)
