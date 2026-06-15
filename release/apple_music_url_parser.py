"""Apple Music URL ID extraction.

Hoisted out of ``release/orchestrator.py`` so the lookup orchestrator can
extract the album_id from a freshly-resolved Apple Music URL without
importing a ``_``-prefixed private symbol across modules. The regex and
helper move together unchanged; ``release.orchestrator`` keeps thin
backwards-compatible aliases so existing call sites continue to work.
"""

from __future__ import annotations

import re

# Locale segment accepts:
#   - 2-letter ISO codes ("us", "gb", "jp") — the canonical Apple shape.
#   - Hyphenated regional variants ("zh-cn", "pt-br") — Apple's non-Latin
#     storefronts. Without these, the regex misses real production URLs.
# Case-insensitive on the match so paste-from-Safari-Reader URLs with an
# uppercased locale ("US") also resolve. The album-ID floor stays at 6
# digits to admit Apple's full historical catalog; Apple has minted IDs
# in that range though most modern IDs are 9-10.
APPLE_ALBUM_ID_RE = re.compile(
    r"music\.apple\.com/[a-z]{2}(?:-[a-z]{2,4})?/album/[^/?#]+/(?:id)?(\d{6,})",
    re.IGNORECASE,
)


def apple_album_id_from_url(url: str) -> str | None:
    """``music.apple.com/<locale>/album/<slug>/<id>`` (or ``/id<id>``) → ``<id>``.

    Anchored on the Apple host so a slug containing digits cannot be mistaken
    for the album ID. Returns ``None`` on a non-matching URL — caller should
    treat that as "URL did not carry an extractable album_id" rather than
    raise.
    """
    match = APPLE_ALBUM_ID_RE.search(url)
    return match.group(1) if match else None
