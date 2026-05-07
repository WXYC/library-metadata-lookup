"""Resolve a Bandcamp album URL to canonical fields + identifiers.

Bandcamp does not have a public API. Every album page embeds a
``<script type="application/ld+json">`` blob with schema.org MusicAlbum
properties (``name``, ``byArtist.name``, ``publisher.name``, ``datePublished``)
which is stable enough to scrape for the prefill flow.

The "self-released" case is detected by comparing the album's host
to the publisher's host: when an artist self-releases, Bandcamp lists the
artist itself as the publisher, with the same subdomain. In that case we
return ``label=None`` per the music director's instruction (don't pretend
the artist is the label).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import httpx

from release.models import CanonicalRelease, ReleaseIdentifiers
from scripts.bandcamp_client import BandcampClient

logger = logging.getLogger(__name__)


_LD_JSON_BLOCK_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


@dataclass
class BandcampResolveResult:
    """Result of resolving a single Bandcamp album URL."""

    canonical: CanonicalRelease | None
    identifiers: ReleaseIdentifiers
    warnings: list[str]


def _host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(url).netloc.lower() or None
    except ValueError:
        return None


def _parse_year(date_published: str | None) -> int | None:
    """Extract the year from Bandcamp's date string.

    Bandcamp ships ``datePublished`` as ``"DD Mon YYYY HH:MM:SS GMT"`` (RFC-2822-ish
    without the day-of-week). Try the most common shape; fall back to a regex
    pull on a 4-digit year.
    """
    if not date_published:
        return None
    try:
        return datetime.strptime(date_published, "%d %b %Y %H:%M:%S %Z").year
    except (ValueError, TypeError):
        match = re.search(r"\b(19|20)\d{2}\b", date_published)
        return int(match.group()) if match else None


def parse_bandcamp_album_html(html: str, album_url: str) -> BandcampResolveResult:
    """Pull the JSON-LD blob out of an album page and project to canonical form.

    Pure function — separated from the HTTP fetch so tests run against committed
    fixtures and don't hit Bandcamp.
    """
    warnings: list[str] = []

    blocks = _LD_JSON_BLOCK_RE.findall(html)
    if not blocks:
        return BandcampResolveResult(
            canonical=None,
            identifiers=ReleaseIdentifiers(bandcamp_album_url=album_url),
            warnings=["Bandcamp page did not contain a JSON-LD MusicAlbum block"],
        )

    # Find the MusicAlbum block (skip MusicGroup, BreadcrumbList, etc).
    # Bandcamp ships @type as a string most of the time, but schema.org allows
    # arrays (e.g. ["MusicAlbum", "Product"]); tolerate both shapes.
    album_data: dict | None = None
    for block in blocks:
        try:
            parsed = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        type_value = parsed.get("@type")
        types = type_value if isinstance(type_value, list) else [type_value]
        if "MusicAlbum" in types:
            album_data = parsed
            break

    if album_data is None:
        return BandcampResolveResult(
            canonical=None,
            identifiers=ReleaseIdentifiers(bandcamp_album_url=album_url),
            warnings=["Bandcamp page JSON-LD did not contain a MusicAlbum entry"],
        )

    name = album_data.get("name")
    by_artist = album_data.get("byArtist") or {}
    artist_name = by_artist.get("name") if isinstance(by_artist, dict) else None
    if not name or not artist_name:
        return BandcampResolveResult(
            canonical=None,
            identifiers=ReleaseIdentifiers(bandcamp_album_url=album_url),
            warnings=["Bandcamp JSON-LD missing required name or byArtist.name"],
        )

    # Self-released: the artist subdomain *is* the publisher subdomain.
    # Per the music director: don't pretend the artist is the label.
    publisher = album_data.get("publisher") or {}
    publisher_name = publisher.get("name") if isinstance(publisher, dict) else None
    publisher_url = publisher.get("@id") if isinstance(publisher, dict) else None
    artist_url = by_artist.get("@id") if isinstance(by_artist, dict) else None
    label: str | None = publisher_name
    if _host(publisher_url) and _host(artist_url) and _host(publisher_url) == _host(artist_url):
        label = None

    canonical = CanonicalRelease(
        artist=artist_name,
        title=name,
        label=label,
        catno=None,  # Bandcamp does not expose catalog numbers.
        year=_parse_year(album_data.get("datePublished")),
        country=None,
        formats=[],
    )

    identifiers = ReleaseIdentifiers(bandcamp_album_url=album_url)

    return BandcampResolveResult(canonical=canonical, identifiers=identifiers, warnings=warnings)


async def fetch_bandcamp_album_html(client: BandcampClient, album_url: str) -> str | None:
    """Fetch the raw HTML for a Bandcamp album page using the rate-limited client.

    Returns None on HTTP error so the resolver can surface a warning instead of
    an exception bubbling all the way to the form.
    """
    try:
        # Reuse the same retry-on-429 helper used by the autocomplete + scrape paths.
        response: httpx.Response | None = await client._request_with_retry(  # noqa: SLF001
            "GET", album_url, timeout=15.0, follow_redirects=True
        )
    except Exception:
        logger.exception("Bandcamp album fetch failed for %s", album_url)
        return None
    if response is None or response.status_code != 200:
        return None
    # Force UTF-8 decoding. Bandcamp serves UTF-8, but the Content-Type header
    # sometimes omits `charset=`; httpx then either runs autodetect (if
    # charset-normalizer is installed) or falls back to ISO-8859-1, both of
    # which can mojibake diacritic-bearing artist/album names (e.g. "Sigur Rós"
    # -> "Sigur RÃ³s") and land corrupt bytes in JSON-LD downstream.
    # Reference: WXYC/library-metadata-lookup#260, WXYC/docs#17 (WX-3.D).
    response.encoding = "utf-8"
    return response.text


async def resolve_bandcamp_album(client: BandcampClient, album_url: str) -> BandcampResolveResult:
    """Fetch + parse an album page in one call. Wraps the two helpers above."""
    html = await fetch_bandcamp_album_html(client, album_url)
    if html is None:
        return BandcampResolveResult(
            canonical=None,
            identifiers=ReleaseIdentifiers(bandcamp_album_url=album_url),
            warnings=[f"Bandcamp page could not be fetched: {album_url}"],
        )
    return parse_bandcamp_album_html(html, album_url)
