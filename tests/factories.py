"""Shared test factories for model construction."""

from discogs.models import DiscogsSearchResult
from library.models import LibraryItem


def make_library_item(id=1, artist="Artist", title="Album", **kwargs):
    """Build a LibraryItem with sensible defaults."""
    defaults = dict(
        call_letters="A",
        artist_call_number=1,
        release_call_number=1,
        genre="Rock",
        format="CD",
    )
    defaults.update(kwargs)
    return LibraryItem(id=id, artist=artist, title=title, **defaults)


def make_discogs_result(release_id=123, **kwargs):
    """Build a DiscogsSearchResult with sensible defaults."""
    defaults = dict(
        release_url=f"https://discogs.com/release/{release_id}",
        album="Test Album",
        artist="Test Artist",
    )
    defaults.update(kwargs)
    return DiscogsSearchResult(release_id=release_id, **defaults)


LOOKUP_BODY = {"artist": "Queen", "album": "The Game", "raw_message": "Queen - The Game"}
