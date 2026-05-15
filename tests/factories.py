"""Shared test factories for model construction."""

from discogs.models import DiscogsSearchResult
from generated.api_models import DiscogsMatchResult, LibraryCatalogItem
from library.models import LibraryItem


def make_library_item(id=1, artist="Stereolab", title="Aluminum Tunes", **kwargs):
    """Build a LibraryItem (internal domain model) with sensible defaults."""
    defaults = {
        "call_letters": "S",
        "artist_call_number": 1,
        "release_call_number": 1,
        "genre": "Rock",
        "format": "CD",
    }
    defaults.update(kwargs)
    return LibraryItem(id=id, artist=artist, title=title, **defaults)


def make_discogs_result(release_id=123, **kwargs):
    """Build a DiscogsSearchResult (internal domain model) with sensible defaults."""
    defaults = {
        "release_url": f"https://discogs.com/release/{release_id}",
        "album": "Aluminum Tunes",
        "artist": "Stereolab",
    }
    defaults.update(kwargs)
    return DiscogsSearchResult(release_id=release_id, **defaults)


def make_catalog_item(id=1, artist="Stereolab", title="Aluminum Tunes", **kwargs):
    """Build a LibraryCatalogItem (API contract model) with sensible defaults."""
    defaults = {
        "call_letters": "S",
        "artist_call_number": 1,
        "release_call_number": 1,
        "genre": "Rock",
        "format": "CD",
        "call_number": "Rock CD S 1/1",
        "library_url": f"http://www.wxyc.info/wxycdb/libraryRelease?id={id}",
    }
    defaults.update(kwargs)
    return LibraryCatalogItem(id=id, artist=artist, title=title, **defaults)


def make_match_result(release_id=123, **kwargs):
    """Build a DiscogsMatchResult (API contract model) with sensible defaults.

    Extended-metadata fields (discogs_artist_id, tracklist, genres, styles,
    label, full_release_date, artist_image_url, profile_tokens) default to
    None — the API contract treats them as opt-in for `extended=true`
    lookups. Pass them through ``kwargs`` to assert on their shape.
    """
    defaults = {
        "release_url": f"https://discogs.com/release/{release_id}",
        "album": "Aluminum Tunes",
        "artist": "Stereolab",
    }
    defaults.update(kwargs)
    return DiscogsMatchResult(release_id=release_id, **defaults)


LOOKUP_BODY = {
    "artist": "Jessica Pratt",
    "album": "On Your Own Love Again",
    "raw_message": "Jessica Pratt - On Your Own Love Again",
}
