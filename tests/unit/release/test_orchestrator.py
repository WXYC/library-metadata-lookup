"""End-to-end tests for the resolve_release orchestrator with all I/O mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generated.api_models import (
    DiscogsLabelCredit,
    DiscogsReleaseMetadata,
)
from release.apple_music_url_parser import apple_album_id_from_url
from release.models import ReleaseResolveRequest
from release.orchestrator import (
    _ResolverDependencies,
    _spotify_album_id_from_url,
    resolve_release,
)
from streaming.models import SourceMatch, StreamingCheckResponse, StreamingCheckSources


def _deps(
    *,
    discogs=None,
    bandcamp=None,
    spotify=None,
    deezer=None,
    apple_music=None,
    entity_store=None,
):
    """Build a _ResolverDependencies with sensible defaults for tests."""
    return _ResolverDependencies(
        discogs=discogs or AsyncMock(),
        bandcamp=bandcamp or MagicMock(),
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
        assert apple_album_id_from_url(url) == expected


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


class TestBandcampBranch:
    @pytest.mark.asyncio
    async def test_short_circuits_bandcamp_streaming_check(self):
        # The DJ pasted a Bandcamp URL — we should mark Bandcamp as a confident
        # match and not waste a fuzzy lookup.
        bandcamp = MagicMock()
        with (
            patch(
                "release.orchestrator.resolve_bandcamp_album",
                new=AsyncMock(),
            ) as mock_resolve,
            patch(
                "release.orchestrator.check_streaming_availability",
                new=AsyncMock(return_value=_stream(on_streaming=False)),
            ),
        ):
            from release.models import CanonicalRelease, ReleaseIdentifiers, ResolveResult

            url = "https://juana-molina.bandcamp.com/album/doga"
            mock_resolve.return_value = ResolveResult(
                canonical=CanonicalRelease(artist="Juana Molina", title="DOGA", label=None),
                identifiers=ReleaseIdentifiers(bandcamp_album_url=url),
                warnings=[],
            )

            response = await resolve_release(
                ReleaseResolveRequest(url=url), _deps(bandcamp=bandcamp)
            )

        assert response.source == "bandcamp"
        assert response.canonical.label is None  # self-released stays null
        assert response.streaming is not None
        # Short-circuit: Bandcamp pinned to the input URL with confidence 100,
        # on_streaming flipped to True.
        assert response.streaming.sources.bandcamp is not None
        assert response.streaming.sources.bandcamp.url == url
        assert response.streaming.sources.bandcamp.confidence == 100.0
        assert response.streaming.on_streaming is True

    @pytest.mark.asyncio
    async def test_passes_bandcamp_none_to_streaming_when_input_is_bandcamp(self):
        # Performance guarantee: don't run a Bandcamp fuzzy search when the
        # caller already gave us a Bandcamp URL.
        bandcamp = MagicMock()
        check_mock = AsyncMock(return_value=_stream(on_streaming=False))
        with (
            patch("release.orchestrator.resolve_bandcamp_album", new=AsyncMock()) as mock_resolve,
            patch("release.orchestrator.check_streaming_availability", new=check_mock),
        ):
            from release.models import CanonicalRelease, ReleaseIdentifiers, ResolveResult

            url = "https://juana-molina.bandcamp.com/album/doga"
            mock_resolve.return_value = ResolveResult(
                canonical=CanonicalRelease(artist="Juana Molina", title="DOGA"),
                identifiers=ReleaseIdentifiers(bandcamp_album_url=url),
                warnings=[],
            )

            await resolve_release(ReleaseResolveRequest(url=url), _deps(bandcamp=bandcamp))

        check_mock.assert_awaited_once()
        assert check_mock.await_args.kwargs["bandcamp"] is None

    @pytest.mark.asyncio
    async def test_passes_bandcamp_client_to_streaming_for_discogs_input(self):
        # Sanity check the inverse: a Discogs paste should route through the
        # full Bandcamp fuzzy match (so we still discover the Bandcamp URL).
        discogs = AsyncMock()
        discogs.get_release.return_value = _make_release()
        bandcamp = MagicMock()
        check_mock = AsyncMock(return_value=_stream(on_streaming=False))
        with patch("release.orchestrator.check_streaming_availability", new=check_mock):
            await resolve_release(
                ReleaseResolveRequest(url="https://www.discogs.com/release/12345"),
                _deps(discogs=discogs, bandcamp=bandcamp),
            )

        check_mock.assert_awaited_once()
        assert check_mock.await_args.kwargs["bandcamp"] is bandcamp

    @pytest.mark.asyncio
    async def test_bandcamp_fetch_failure_returns_warnings(self):
        bandcamp = MagicMock()
        with (
            patch(
                "release.orchestrator.resolve_bandcamp_album",
                new=AsyncMock(),
            ) as mock_resolve,
            patch("release.orchestrator.check_streaming_availability") as cs,
        ):
            from release.models import ReleaseIdentifiers, ResolveResult

            mock_resolve.return_value = ResolveResult(
                canonical=None,
                identifiers=ReleaseIdentifiers(bandcamp_album_url="https://x.bandcamp.com/album/y"),
                warnings=["could not be fetched"],
            )

            response = await resolve_release(
                ReleaseResolveRequest(url="https://x.bandcamp.com/album/y"),
                _deps(bandcamp=bandcamp),
            )

        cs.assert_not_called()
        assert response.canonical.artist == ""
        assert response.warnings == ["could not be fetched"]
        assert response.streaming is None

    @pytest.mark.asyncio
    async def test_bandcamp_writeback_uses_slug(self):
        bandcamp = MagicMock()
        store = AsyncMock()
        with (
            patch(
                "release.orchestrator.resolve_bandcamp_album",
                new=AsyncMock(),
            ) as mock_resolve,
            patch(
                "release.orchestrator.check_streaming_availability",
                new=AsyncMock(return_value=_stream()),
            ),
        ):
            from release.models import CanonicalRelease, ReleaseIdentifiers, ResolveResult

            url = "https://juana-molina.bandcamp.com/album/doga"
            mock_resolve.return_value = ResolveResult(
                canonical=CanonicalRelease(artist="Juana Molina", title="DOGA"),
                identifiers=ReleaseIdentifiers(bandcamp_album_url=url),
                warnings=[],
            )

            await resolve_release(
                ReleaseResolveRequest(url=url),
                _deps(bandcamp=bandcamp, entity_store=store),
            )

        kwargs = store.upsert_identity.call_args.kwargs
        # The slug, not the full URL — matches what bandcamp_pipeline.py writes.
        assert kwargs["bandcamp_id"] == "juana-molina"


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

        # LML#766: the discogs_artist_id write is fill-if-null so a concurrent
        # (bare-name resolver) id is never clobbered. No bandcamp id + a
        # discogs id present means the COALESCE-additive upsert is skipped.
        store.fill_identity_discogs_id.assert_awaited_once_with("Juana Molina", 999)
        store.upsert_identity.assert_not_called()

    @pytest.mark.asyncio
    async def test_lost_race_does_not_surface_a_warning(self):
        """LML#766: the release-derived write is higher-evidence than a
        bare-name search win, but if a concurrent writer already set a
        different id the fill primitive keeps the stored value. The store
        logs it; the DJ sees no warning (either id is a valid Discogs artist
        for the release), and the response is unaffected."""
        from entity.store import FillOutcome, Identity

        discogs = AsyncMock()
        discogs.get_release.return_value = _make_release()
        store = AsyncMock()
        store.fill_identity_discogs_id.return_value = FillOutcome(
            identity=Identity(
                id=1,
                library_name="Juana Molina",
                discogs_artist_id=42,
                reconciliation_status="reconciled",
            ),
            filled=False,
            lost_race=True,
            previous_discogs_artist_id=42,
        )
        deps = _deps(discogs=discogs, entity_store=store)

        with patch(
            "release.orchestrator.check_streaming_availability",
            new=AsyncMock(return_value=_stream()),
        ):
            response = await resolve_release(
                ReleaseResolveRequest(url="https://www.discogs.com/release/12345"), deps
            )

        store.fill_identity_discogs_id.assert_awaited_once_with("Juana Molina", 999)
        assert response.canonical.artist == "Juana Molina"
        # A lost race is not an error condition — no warning to the caller.
        assert not any("identifiers" in w.lower() for w in response.warnings)

    @pytest.mark.asyncio
    async def test_writes_both_ids_when_discogs_and_bandcamp_present(self):
        """LML#766: when the release carries BOTH a discogs_artist_id and a
        bandcamp id, the two writes fire — fill-if-null for the contested
        discogs_artist_id, COALESCE-additive upsert for the bandcamp_id."""
        discogs = AsyncMock()
        discogs.get_release.return_value = _make_release()
        store = AsyncMock()
        deps = _deps(discogs=discogs, entity_store=store)

        # A bandcamp source promoted by the streaming check gives the release a
        # bandcamp_album_url alongside the Discogs artist id.
        streaming = _stream()
        streaming.sources.bandcamp = SourceMatch(
            url="https://juana-molina.bandcamp.com/album/doga", confidence=100.0
        )
        with patch(
            "release.orchestrator.check_streaming_availability",
            new=AsyncMock(return_value=streaming),
        ):
            await resolve_release(
                ReleaseResolveRequest(url="https://www.discogs.com/release/12345"), deps
            )

        # Contested id → fill-if-null; bandcamp id → additive upsert. Both run.
        store.fill_identity_discogs_id.assert_awaited_once_with("Juana Molina", 999)
        store.upsert_identity.assert_awaited_once()
        assert store.upsert_identity.call_args.kwargs["bandcamp_id"] == "juana-molina"

    @pytest.mark.asyncio
    async def test_concurrent_delete_of_row_is_logged_not_silent(self, caplog):
        """LML#766: the fill primitive's concurrent-delete path returns
        ``identity=None`` (the row vanished between the blocked upsert and the
        read-back). With no bandcamp id the additive upsert is skipped, so the
        learned id is not re-stamped — but the drop must not be silent."""
        import logging

        from entity.store import FillOutcome

        discogs = AsyncMock()
        discogs.get_release.return_value = _make_release()
        store = AsyncMock()
        store.fill_identity_discogs_id.return_value = FillOutcome(
            identity=None, filled=False, lost_race=False, previous_discogs_artist_id=None
        )
        deps = _deps(discogs=discogs, entity_store=store)

        with (
            patch(
                "release.orchestrator.check_streaming_availability",
                new=AsyncMock(return_value=_stream()),
            ),
            caplog.at_level(logging.WARNING, logger="release.orchestrator"),
        ):
            response = await resolve_release(
                ReleaseResolveRequest(url="https://www.discogs.com/release/12345"), deps
            )

        # No bandcamp id and a discogs id present → additive upsert skipped.
        store.upsert_identity.assert_not_called()
        # Response is unaffected, but the vanished-row drop left a trail.
        assert response.canonical.artist == "Juana Molina"
        assert any(
            "Juana Molina" in rec.getMessage() and "999" in rec.getMessage()
            for rec in caplog.records
        ), caplog.text

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
        store.fill_identity_discogs_id.side_effect = RuntimeError("PG down")
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
        store.fill_identity_discogs_id.assert_not_called()

    @pytest.mark.parametrize(
        "va_artist",
        [
            "Various",
            "Various Artists",
            "V.A.",
            "V/A",
        ],
    )
    @pytest.mark.asyncio
    async def test_skips_when_canonical_artist_is_compilation(self, va_artist):
        """V/A artists must not pollute entity.identity.

        Read-side V/A detection (identity/router.py, discogs/service.py) skips
        these rows from the matcher's lookup space — but the rows still exist
        if write-side never guarded against them. See WXYC/library-metadata-lookup#379.
        """
        discogs = AsyncMock()
        discogs.get_release.return_value = _make_release(artist=va_artist)
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
        store.fill_identity_discogs_id.assert_not_called()
