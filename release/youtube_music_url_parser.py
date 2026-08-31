"""YouTube Music host predicate.

Sibling to ``release.apple_music_url_parser`` and ``release.spotify_url_parser``,
added for LML#1295 (extending the LML#873 ``streaming_links`` field-name/host
invariant check — a mislabeled URL stored under a service's own column — to
YouTube Music). Unlike those two, YouTube Music has no album-ID extractor:
``lookup/streaming_url_registry.py``'s entry sets ``url_to_external_id=None``
because a resolved ``music.youtube.com/browse/<id>`` browse ID has no identity
column to mint (LML#1103), so there is no strict extractor for this module to
pair the host check with.
"""

from __future__ import annotations

from release.host_matching import host_matcher

url_has_youtube_music_host = host_matcher(
    "youtube.com",
    doc="""True if ``url``'s host is ``youtube.com`` or a subdomain of it.

    Covers both ``music.youtube.com`` (the canonical album/browse host) and
    bare ``youtube.com`` / ``www.youtube.com`` — mirroring
    ``url_has_apple_music_host``'s apex-domain looseness. Used to null out a
    mislabeled ``youtube_music_url`` artifact before it reaches a caller.
    """,
)
