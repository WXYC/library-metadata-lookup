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


#: The malformed-URL bar WXYC/Backend-Service#2351's wire contract uses: any
#: C0 control code point (``0x00``-``0x1F``), the ASCII space (``0x20``), DEL
#: (``0x7F``), or backslash (``0x5C``). This is deliberately NOT "characters
#: ``str.isspace()`` would flag" — ``str.isspace()`` also flags Unicode
#: whitespace this range check does not catch (NBSP ``U+00A0``, the line
#: separator ``U+2028``, ...), and conversely this range already catches
#: non-whitespace control bytes (NUL, BEL, ESC) that ``str.isspace()`` would
#: not flag at all. The backslash leg is separate from the control-code
#: range and closes a real WHATWG-vs-RFC-3986 host differential
#: (WXYC/Backend-Service#1710), and the differential lives in the
#: *authority* position, not the path: for the ``http``/``https`` special
#: schemes, WHATWG treats ``\`` as an authority/host terminator the same as
#: ``/``, so if it appears before any ``@`` the host is cut off right there
#: and everything after — including a later ``@`` — folds into the path
#: instead. ``https://evil.example\@a.bandcamp.com/x`` therefore resolves to
#: hostname ``evil.example`` under every WHATWG client (a browser, Node,
#: Backend-Service's own URL handling), while this module's ``urlparse``-based
#: :func:`host_matcher` never splits the netloc at all — it just suffix-checks
#: the raw ``netloc`` string, which here is still
#: ``evil.example\@a.bandcamp.com`` verbatim and still ends with
#: ``.bandcamp.com``, so the host check passes a value a browser would send
#: to ``evil.example``. (In the *path* position — ``\`` after the host, e.g.
#: ``.../album\@evil.example`` — both parsers agree on the host; WHATWG only
#: folds the ``\`` to ``/`` inside the path itself, which is a path-shape
#: difference, not a host spoof.) Matching the wire contract's bar
#: code-point-for-code-point keeps this module in parity with
#: Backend-Service either way, which is the point — not approximating
#: Python's notion of whitespace.
_MAX_DISALLOWED_CODE_POINT = 0x20
_DEL = 0x7F
_BACKSLASH = 0x5C


def is_well_formed_web_url(url: str | None) -> bool:
    """True if ``url`` is an absolute ``http``/``https`` URL with no control char or backslash.

    The host-agnostic floor every ``streaming_links`` URL field must clear
    (LML#1295) before a per-service :func:`host_matcher` predicate runs on top
    of it. ``host_matcher`` alone does not catch a scheme-relative URL
    (``//host/path`` — ``urlparse`` still yields the right ``netloc``), a
    bare host (``host/path`` — no ``scheme``, no ``netloc``, but the "host" is
    sitting right there in ``path``), or an embedded backslash (``0x5C``) —
    the first two were observed in production ``streaming_links`` values
    (WXYC/Backend-Service#1710) alongside control-character-corrupted URLs
    (embedded tab/LF/space among them); the backslash leg closes a WHATWG-vs-
    RFC-3986 host-parsing differential in the authority position (see the
    module-level comment above :data:`_MAX_DISALLOWED_CODE_POINT` for the
    concrete spoof). This function is the shared check for all of the above,
    plus non-web schemes (``ftp:``, ``mailto:``).

    Guards ``None``/empty input and a ``urlparse`` ``ValueError`` (malformed
    URL) the same way :func:`host_matcher`'s predicate does — "malformed" and
    "not present" both read as "not safe to surface".
    """
    if not url:
        return False
    if any(ord(ch) <= _MAX_DISALLOWED_CODE_POINT or ord(ch) in (_DEL, _BACKSLASH) for ch in url):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
