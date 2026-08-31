"""SoundCloud host predicate.

Sibling to ``release.apple_music_url_parser`` and ``release.spotify_url_parser``,
added for LML#1295 (extending the LML#873 ``streaming_links`` field-name/host
invariant check — a mislabeled URL stored under a service's own column — to
SoundCloud). SoundCloud has no cache tier (absent from
``lookup/streaming_url_registry.py``) and no opaque album ID to extract, so
this module carries only the host predicate.
"""

from __future__ import annotations

from release.host_matching import host_matcher

url_has_soundcloud_host = host_matcher(
    "soundcloud.com",
    doc="""True if ``url``'s host is ``soundcloud.com`` or a subdomain of it.

    Used to null out a mislabeled ``soundcloud_url`` artifact before it
    reaches a caller.
    """,
)
