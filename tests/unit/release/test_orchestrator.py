"""End-to-end tests for the resolve_release orchestrator with all I/O mocked.

Bandcamp branch is intentionally tested only at the "returns warning, not yet
supported" level here. Full Bandcamp orchestration tests come with the
follow-up PR that wires the resolver in.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from generated.api_models import (
    DiscogsLabelCredit,
    DiscogsReleaseMetadata,
)
from release.models import ReleaseResolveRequest
from release.orchestrator import (
    _apple_album_id_from_url,
    _ResolverDependencies,
    _spotify_album_id_from_url,
    resolve_release,
)
from streaming.models import SourceMatch, StreamingCheckResponse, StreamingCheckSources


def _deps(
    *,
    discogs=None,
    spotify=None,
    deezer=None,
    apple_music=None,
    entity_store=None,
):
    """Build a _ResolverDependencies with sensible defaults for tests."""
    return _ResolverDependencies(
        discogs=discogs or AsyncMock(),
        spotify=spotify,
        deezer=deezer,
        apple_music=apple_music,
        entity_store=entity_store,
    )


def _stream(
    *,
    on_streaming=False,
    spotify_url=None,
    apple_url=None,
):
    sources = StreamingCheckSources(
        spotify=SourceMatch(url=spotify_url, confidence=95.0) if spotify_url else None,
        apple_music=SourceMatch(url=apple_url, confidence=90.0) if apple_url else None,
    )
    return StreamingCheckResponse(on_streaming=on_streaming, sources=sources)


def _make_release(**overrides) -> DiscogsReleaseMetadata:
    base = {
        "release_id": 12345,
        "title": "DOGA",
        "artist": "Juana Molina",
        "year": 2024,
        "label": "Sonamos",
        "artist_id": 999,
        "labels": [DiscogsLabelCredit(label_id=42, name="Sonamos", catno="SON-001")],
        "release_url": "https://www.discogs.com/release/12345",
    }
    base.update(overrides)
    return DiscogsReleaseMetadata.model_validate(base)


class TestSpotifyAlbumIdExtraction:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://open.spotify.com/album/abc123XYZ", "abc123XYZ"),
            ("https://open.spotify.com/album/abc123XYZ?si=tracking", "abc123XYZ"),
            ("https://open.spotify.com/track/zzz", None),
            ("https://example.com/album/notspotify", None),
            ("", None),
        ],
    )
    def test_extraction(self, url, expected):
        assert _spotify_album_id_from_url(url) == expected


class TestAppleAlbumIdExtraction:
    @pytest.mark.parametrize(
        "url,expected",
        [
            # Modern Apple URL shape: /<locale>/album/<slug>/<id>
            ("https://music.apple.com/us/album/aluminum-tunes/123456789", "123456789"),
            # Legacy "id<digits>" shape still seen in some iTunes responses.
            ("https://music.apple.com/us/album/foo/id987654321", "987654321"),
            # Slug containing digits must NOT be mistaken for the album ID.
            ("https://music.apple.com/us/album/my-album-1999/123456789", "123456789"),
            # Wrong host: don't claim a match.
            ("https://example.com/album/foo/123456789", None),
            # Track URL, not album.
            ("https://music.apple.com/us/track/foo/123456789", None),
            ("", None),
        ],
    )
    def test_extraction(self, url, expected):
        assert _apple_album_id_from_url(url) == expected


class TestParsedUrlRouting:
    @pytest.mark.asyncio
    async def test_unrecognized_url_returns_warning(self):
        deps = _deps()
        with patch("release.orchestrator.check_streaming_availability") as cs:
            response = await resolve_release(
                ReleaseResolveRequest(url="https://example.com/foo"), deps
            )
        cs.assert_not_called()
        # source="unknown" so consumers can disambiguate from a real "discogs_release"
        # response that happens to have an empty canonical (very different reasons).
        assert response.source == "unknown"
        assert response.source_id == "https://example.com/foo"
        assert response.canonical.artist == ""
        assert any("recognize the URL" in w for w in response.warnings)

    @pytest.mark.asyncio
    async def test_explicit_source_and_id_routes_to_discogs(self):
        discogs = AsyncMock()
        discogs.get_release.return_value = _make_release()
        deps = _deps(discogs=discogs)

        with patch(
            "release.orchestrator.check_streaming_availability",
            new=AsyncMock(return_value=_stream()),
        ):
            response = await resolve_release(
                ReleaseResolveRequest(source="discogs_release", id="12345"), deps
            )

        assert response.source == "discogs_release"
        assert response.canonical.artist == "Juana Molina"
        discogs.get_release.assert_awaited_once_with(12345)


class TestDiscogsBranch:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        discogs = AsyncMock()
        discogs.get_release.return_value = _make_release()
        deps = _deps(discogs=discogs)

        with patch(
            "release.orchestrator.check_streaming_availability",
            new=AsyncMock(
                return_value=_stream(
                    on_streaming=True,
                    spotify_url="https://open.spotify.com/album/abc123def",
                )
            ),
        ):
            response = await resolve_release(
                ReleaseResolveRequest(url="https://www.discogs.com/release/12345"), deps
            )

        assert response.source == "discogs_release"
        assert response.source_id == "12345"
        assert response.canonical.artist == "Juana Molina"
        assert response.canonical.label == "Sonamos"
        assert response.canonical.catno == "SON-001"
        assert response.identifiers.discogs_release_id == 12345
        assert response.identifiers.discogs_artist_id == 999
        assert response.identifiers.spotify_album_id == "abc123def"
        assert response.streaming is not None
        assert response.streaming.on_streaming is True

    @pytest.mark.asyncio
    async def test_master_url_returns_warning(self):
        discogs = AsyncMock()
        deps = _deps(discogs=discogs)

        # Streaming check should not run when canonical is missing.
        with patch("release.orchestrator.check_streaming_availability") as cs:
            response = await resolve_release(
                ReleaseResolveRequest(url="https://www.discogs.com/master/789"), deps
            )

        cs.assert_not_called()
        assert response.source == "discogs_master"
        assert response.identifiers.discogs_master_id == 789
        assert any("master" in w.lower() for w in response.warnings)


class TestBandcampNotYetSupported:
    @pytest.mark.asyncio
    async def test_bandcamp_url_returns_warning_without_io(self):
        # The URL parser recognizes Bandcamp, but the resolver isn't wired up yet.
        # Until the follow-up PR lands, a bandcamp paste returns a clear warning
        # and never touches the streaming or identity paths.
        deps = _deps()
        with patch("release.orchestrator.check_streaming_availability") as cs:
            response = await resolve_release(
                ReleaseResolveRequest(url="https://juana-molina.bandcamp.com/album/doga"), deps
            )

        cs.assert_not_called()
        assert response.source == "bandcamp"
        assert response.source_id == "https://juana-molina.bandcamp.com/album/doga"
        assert response.identifiers.bandcamp_album_url == response.source_id
        assert response.canonical.artist == ""
        assert any("not yet supported" in w.lower() for w in response.warnings)


class TestStreamingFailureSurfacesAsWarning:
    @pytest.mark.asyncio
    async def test_streaming_exception_does_not_break_response(self):
        discogs = AsyncMock()
        discogs.get_release.return_value = _make_release()
        deps = _deps(discogs=discogs)

        with patch(
            "release.orchestrator.check_streaming_availability",
            new=AsyncMock(side_effect=RuntimeError("streaming exploded")),
        ):
            response = await resolve_release(
                ReleaseResolveRequest(url="https://www.discogs.com/release/12345"), deps
            )

        # Canonical metadata is still returned; streaming is null + warning logged.
        assert response.canonical.artist == "Juana Molina"
        assert response.streaming is None
        assert any("Streaming" in w for w in response.warnings)


class TestIdentityWriteBack:
    @pytest.mark.asyncio
    async def test_writes_back_when_store_present(self):
        discogs = AsyncMock()
        discogs.get_release.return_value = _make_release()
        store = AsyncMock()
        deps = _deps(discogs=discogs, entity_store=store)

        with patch(
            "release.orchestrator.check_streaming_availability",
            new=AsyncMock(return_value=_stream()),
        ):
            await resolve_release(
                ReleaseResolveRequest(url="https://www.discogs.com/release/12345"), deps
            )

        store.upsert_identity.assert_awaited_once()
        args, kwargs = store.upsert_identity.call_args
        assert args[0] == "Juana Molina"
        assert kwargs["discogs_artist_id"] == 999

    @pytest.mark.asyncio
    async def test_skips_when_store_unavailable(self):
        discogs = AsyncMock()
        discogs.get_release.return_value = _make_release()
        deps = _deps(discogs=discogs, entity_store=None)

        with patch(
            "release.orchestrator.check_streaming_availability",
            new=AsyncMock(return_value=_stream()),
        ):
            response = await resolve_release(
                ReleaseResolveRequest(url="https://www.discogs.com/release/12345"), deps
            )

        # No exception, no warning — identity write-back is best-effort.
        assert "identifiers" not in " ".join(response.warnings).lower()

    @pytest.mark.asyncio
    async def test_writeback_failure_becomes_warning(self):
        discogs = AsyncMock()
        discogs.get_release.return_value = _make_release()
        store = AsyncMock()
        store.upsert_identity.side_effect = RuntimeError("PG down")
        deps = _deps(discogs=discogs, entity_store=store)

        with patch(
            "release.orchestrator.check_streaming_availability",
            new=AsyncMock(return_value=_stream()),
        ):
            response = await resolve_release(
                ReleaseResolveRequest(url="https://www.discogs.com/release/12345"), deps
            )

        # Caller still gets a usable response.
        assert response.canonical.artist == "Juana Molina"
        assert any("identifiers" in w.lower() for w in response.warnings)

    @pytest.mark.asyncio
    async def test_skips_when_canonical_artist_is_empty(self):
        # Defensive: a Discogs release with missing artist should not write
        # library_name="" into entity.identity.
        discogs = AsyncMock()
        discogs.get_release.return_value = _make_release(artist="", artist_id=None)
        store = AsyncMock()
        deps = _deps(discogs=discogs, entity_store=store)

        with patch(
            "release.orchestrator.check_streaming_availability",
            new=AsyncMock(return_value=_stream()),
        ):
            await resolve_release(
                ReleaseResolveRequest(url="https://www.discogs.com/release/12345"), deps
            )

        store.upsert_identity.assert_not_called()
