"""Apple Music URL ID extraction.

Hoisted out of ``release/orchestrator.py`` so the lookup orchestrator can
extract the album_id from a freshly-resolved Apple Music URL without
importing a ``_``-prefixed private symbol across modules. The regex and
helper move together unchanged; ``release.orchestrator`` keeps thin
backwards-compatible aliases so existing call sites continue to work.
"""

from __future__ import annotations

import re

APPLE_ALBUM_ID_RE = re.compile(r"music\.apple\.com/[a-z]{2}/album/[^/?#]+/(?:id)?(\d{6,})")


def apple_album_id_from_url(url: str) -> str | None:
    """``music.apple.com/<locale>/album/<slug>/<id>`` (or ``/id<id>``) → ``<id>``.

    Anchored on the Apple host so a slug containing digits cannot be mistaken
    for the album ID. Returns ``None`` on a non-matching URL — caller should
    treat that as "URL did not carry an extractable album_id" rather than
    raise.
    """
    match = APPLE_ALBUM_ID_RE.search(url)
    return match.group(1) if match else None
