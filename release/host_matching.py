"""Shared host-matching factory for streaming-URL field-name/host invariant checks.

``release/apple_music_url_parser.py`` and ``release/spotify_url_parser.py``
each need "does this URL's host belong to domain X" — exact match or a
``.``-suffixed subdomain — as a looser check than their strict album-ID
extractors (LML#873): both are used to null out a mislabeled
``apple_music_url`` / ``spotify_url`` artifact (a URL from a *different*
streaming service stored under the wrong field name) before it reaches a
caller. The two implementations were line-for-line identical modulo the host
literal; ``host_matcher`` factors the shared guard-empty /
urlparse-with-ValueError-guard / exact-or-suffix logic into one place so the
two call sites can't drift from each other.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse


def host_matcher(domain: str, *, doc: str | None = None) -> Callable[[str | None], bool]:
    """Build a predicate: True if a URL's host is ``domain`` or a subdomain of it.

    ``domain`` is the bare registrable domain (e.g. ``"apple.com"``); the
    returned predicate matches it exactly or as a ``.``-suffix (e.g.
    ``music.apple.com``), which also rejects lookalike hosts that merely
    contain ``domain`` as a substring (e.g. ``apple.com.evil.test``).

    The predicate guards empty/``None`` input and a ``urlparse`` ``ValueError``
    (malformed URL) by returning ``False`` rather than raising — callers treat
    "not this host" and "unparseable" the same way.

    ``doc``, if given, becomes the returned callable's ``__doc__`` so a
    module-level assignment like ``url_has_apple_music_host = host_matcher(...)``
    keeps a docstring for ``help()`` / IDE tooltips at the call site.
    """

    def matcher(url: str | None) -> bool:
        if not url:
            return False
        try:
            host = urlparse(url).netloc.lower()
        except ValueError:
            return False
        return host == domain or host.endswith(f".{domain}")

    if doc is not None:
        matcher.__doc__ = doc
    return matcher
