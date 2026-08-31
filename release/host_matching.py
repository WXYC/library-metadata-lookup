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


#: Characters ``str.isspace()`` would flag inside a URL — none of them are
#: valid unescaped, so any of them present means the value is corrupted
#: (embedded tab/LF/space) rather than a URL a browser would accept as-is.
_WHITESPACE_CHARS = " \t\n\r\v\f"


def is_well_formed_web_url(url: str | None) -> bool:
    """True if ``url`` is an absolute ``http``/``https`` URL with no embedded whitespace.

    The host-agnostic floor every ``streaming_links`` URL field must clear
    (LML#1295) before a per-service :func:`host_matcher` predicate runs on top
    of it. ``host_matcher`` alone does not catch a scheme-relative URL
    (``//host/path`` — ``urlparse`` still yields the right ``netloc``) or a
    bare host (``host/path`` — no ``scheme``, no ``netloc``, but the "host" is
    sitting right there in ``path``); both were observed in production
    ``streaming_links`` values (WXYC/Backend-Service#1710) alongside
    whitespace-corrupted URLs, and this function is the shared check for all
    three, plus non-web schemes (``ftp:``, ``mailto:``).

    Guards ``None``/empty input and a ``urlparse`` ``ValueError`` (malformed
    URL) the same way :func:`host_matcher`'s predicate does — "malformed" and
    "not present" both read as "not safe to surface".
    """
    if not url:
        return False
    if any(ch in url for ch in _WHITESPACE_CHARS):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
