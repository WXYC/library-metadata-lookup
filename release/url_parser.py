"""Parse Discogs and Bandcamp URLs into a (source, identifier) tuple.

Used by the ``POST /api/v1/releases/resolve`` endpoint to dispatch a pasted URL
to the right resolver.

The identifier shape depends on source:

- Discogs ``release`` → integer release ID as a string (``"123456"``)
- Discogs ``master`` → integer master ID as a string (``"7890"``)
- Bandcamp → the canonical album URL itself (``"https://artist.bandcamp.com/album/slug"``).
  Bandcamp has no integer ID; the canonical URL is the identity.

The parser does not perform any I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

ParsedSource = Literal["discogs_release", "discogs_master", "bandcamp"]


@dataclass(frozen=True)
class ParsedUrl:
    """Result of parsing a paste-URL.

    ``source`` distinguishes Discogs releases, Discogs masters, and Bandcamp
    so resolvers can dispatch directly. ``identifier`` is a string for both:
    a numeric ID for Discogs and the album URL for Bandcamp.
    """

    source: ParsedSource
    identifier: str


_DISCOGS_PATH_RE = re.compile(r"^/(?:[a-z]{2}/)?(release|master)/(\d+)(?:[-/].*)?$")
"""Matches /release/12345, /master/678, /es/release/12345, /release/12345-some-slug."""

_BANDCAMP_HOST_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)\.bandcamp\.com$")
_BANDCAMP_ALBUM_PATH_RE = re.compile(r"^/album/[a-z0-9][a-z0-9-]*/?$")


def parse_url(url: str) -> ParsedUrl | None:
    """Parse a Discogs or Bandcamp URL. Returns None on unsupported / malformed input.

    Examples (all return a ParsedUrl):

        >>> parse_url("https://www.discogs.com/release/123456").source
        'discogs_release'
        >>> parse_url("https://discogs.com/master/789").source
        'discogs_master'
        >>> parse_url("https://www.discogs.com/release/123-some-title").identifier
        '123'
        >>> parse_url("https://autechre.bandcamp.com/album/confield").source
        'bandcamp'

    Returns None for unsupported hosts, missing paths, and malformed input:

        >>> parse_url("https://example.com/release/123") is None
        True
        >>> parse_url("not a url") is None
        True
        >>> parse_url("") is None
        True
    """
    if not url or not url.strip():
        return None

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None

    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None

    host = parsed.netloc.lower()
    path = parsed.path or "/"

    # Discogs: discogs.com or www.discogs.com (and other locale subdomains
    # are handled by the path regex itself, not the host check).
    if host == "discogs.com" or host == "www.discogs.com":
        match = _DISCOGS_PATH_RE.match(path)
        if match is None:
            return None
        kind, id_str = match.groups()
        if kind == "release":
            return ParsedUrl(source="discogs_release", identifier=id_str)
        return ParsedUrl(source="discogs_master", identifier=id_str)

    # Bandcamp: <slug>.bandcamp.com/album/<slug>
    bandcamp_match = _BANDCAMP_HOST_RE.match(host)
    if bandcamp_match is not None:
        if not _BANDCAMP_ALBUM_PATH_RE.match(path):
            return None
        # Canonicalize: strip query/fragment, drop trailing slash.
        canonical = f"https://{host}{path.rstrip('/')}"
        return ParsedUrl(source="bandcamp", identifier=canonical)

    return None
