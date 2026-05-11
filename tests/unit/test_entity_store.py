"""Unit tests for entity store CRUD operations."""

from unittest.mock import AsyncMock

import pytest

from scripts.entity_resolution.store import (
    EntityStore,
    _strip_nul,
)


class TestStripNul:
    """Locks the WX-3.B boundary contract on the private helper."""

    def test_returns_none_for_none(self) -> None:
        assert _strip_nul(None) is None

    def test_returns_input_unchanged_when_no_nul(self) -> None:
        assert _strip_nul("Stereolab") == "Stereolab"

    def test_strips_single_nul(self) -> None:
        assert _strip_nul("a\x00b") == "ab"

    def test_strips_all_nuls(self) -> None:
        assert _strip_nul("\x00a\x00b\x00") == "ab"

    def test_empty_string_passthrough(self) -> None:
        assert _strip_nul("") == ""

    def test_idempotent(self) -> None:
        once = _strip_nul("a\x00b\x00c")
        twice = _strip_nul(once)
        assert once == twice == "abc"


@pytest.fixture
def mock_pg():
    """Mock PgSource with fetchall/fetchone/execute methods."""
    pg = AsyncMock()
    pg.fetchall = AsyncMock(return_value=[])
    pg.fetchone = AsyncMock(return_value=None)
    pg.execute = AsyncMock(return_value="INSERT 0 1")
    return pg


@pytest.fixture
def store(mock_pg):
    return EntityStore(mock_pg)


class TestUpsertIdentity:
    @pytest.mark.asyncio
    async def test_creates_new_identity(self, store, mock_pg):
        """upsert_identity inserts a new row and returns it with status='unreconciled'."""
        mock_pg.fetchone = AsyncMock(
            return_value={
                "id": 1,
                "library_name": "Autechre",
                "discogs_artist_id": None,
                "wikidata_qid": None,
                "musicbrainz_artist_id": None,
                "spotify_artist_id": None,
                "apple_music_artist_id": None,
                "bandcamp_id": None,
                "reconciliation_status": "unreconciled",
            }
        )
        identity = await store.upsert_identity(library_name="Autechre")
        assert identity is not None
        assert identity.library_name == "Autechre"
        assert identity.reconciliation_status == "unreconciled"
        assert identity.id == 1
        # Verify the query was an INSERT ... ON CONFLICT
        call_args = mock_pg.fetchone.call_args
        assert "INSERT" in call_args[0][0]
        assert "ON CONFLICT" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_updates_existing_with_coalesce(self, store, mock_pg):
        """upsert_identity uses COALESCE so populated fields are not overwritten with NULL."""
        mock_pg.fetchone = AsyncMock(
            return_value={
                "id": 1,
                "library_name": "Autechre",
                "discogs_artist_id": 42,
                "wikidata_qid": None,
                "musicbrainz_artist_id": None,
                "spotify_artist_id": None,
                "apple_music_artist_id": None,
                "bandcamp_id": None,
                "reconciliation_status": "unreconciled",
            }
        )
        identity = await store.upsert_identity(library_name="Autechre", discogs_artist_id=42)
        assert identity is not None
        assert identity.discogs_artist_id == 42
        # Verify COALESCE is used in the UPDATE clause
        call_args = mock_pg.fetchone.call_args
        assert "COALESCE" in call_args[0][0]


class TestGetIdentity:
    @pytest.mark.asyncio
    async def test_returns_identity_by_name(self, store, mock_pg):
        """get_identity returns the identity for an existing library_name."""
        mock_pg.fetchone = AsyncMock(
            return_value={
                "id": 1,
                "library_name": "Autechre",
                "discogs_artist_id": 12,
                "wikidata_qid": "Q378288",
                "musicbrainz_artist_id": None,
                "spotify_artist_id": None,
                "apple_music_artist_id": None,
                "bandcamp_id": None,
                "reconciliation_status": "reconciled",
            }
        )
        identity = await store.get_identity("Autechre")
        assert identity is not None
        assert identity.library_name == "Autechre"
        assert identity.discogs_artist_id == 12
        assert identity.wikidata_qid == "Q378288"

    @pytest.mark.asyncio
    async def test_returns_none_for_missing(self, store, mock_pg):
        """get_identity returns None when no matching library_name exists."""
        mock_pg.fetchone = AsyncMock(return_value=None)
        identity = await store.get_identity("Nonexistent")
        assert identity is None


class TestGetIdentityCanonical:
    """Bridge implementation for WXYC/library-metadata-lookup#274.

    ``get_identity_canonical()`` pre-normalizes the input via
    ``identity.normalize.canonicalize_for_identity_lookup`` and then performs
    the existing exact-match SQL. The stored ``library_name`` is assumed to be
    in the same canonical form (post-#207/#216/#217/#218 reconciliation).
    """

    @pytest.mark.asyncio
    async def test_passes_canonical_form_to_sql(self, store, mock_pg):
        """A non-canonical input is canonicalized before the SQL bind param."""
        mock_pg.fetchone = AsyncMock(return_value=None)
        await store.get_identity_canonical("Nilüfer Yanya")
        call_args = mock_pg.fetchone.call_args
        # SQL bind parameter (positional arg #1 of fetchone) is the canonical
        # form, not the raw input.
        assert call_args[0][1] == "nilufer yanya"

    @pytest.mark.asyncio
    async def test_returns_identity_when_canonical_form_matches(self, store, mock_pg):
        """Stored canonical row → returns Identity even when input is non-canonical."""
        mock_pg.fetchone = AsyncMock(
            return_value={
                "id": 7,
                "library_name": "nilufer yanya",
                "discogs_artist_id": 5499521,
                "wikidata_qid": "Q21470020",
                "musicbrainz_artist_id": None,
                "spotify_artist_id": None,
                "apple_music_artist_id": None,
                "bandcamp_id": None,
                "reconciliation_status": "reconciled",
            }
        )
        identity = await store.get_identity_canonical("Nilüfer Yanya")
        assert identity is not None
        assert identity.discogs_artist_id == 5499521
        assert identity.wikidata_qid == "Q21470020"

    @pytest.mark.asyncio
    async def test_returns_none_for_miss(self, store, mock_pg):
        mock_pg.fetchone = AsyncMock(return_value=None)
        identity = await store.get_identity_canonical("Some Artist")
        assert identity is None


class TestUpdateStatus:
    @pytest.mark.asyncio
    async def test_updates_reconciliation_status(self, store, mock_pg):
        """update_status changes the reconciliation_status for an identity."""
        await store.update_status(1, "reconciled")
        mock_pg.execute.assert_called_once()
        call_args = mock_pg.execute.call_args
        assert "UPDATE" in call_args[0][0]
        assert "reconciliation_status" in call_args[0][0]


class TestLogReconciliation:
    @pytest.mark.asyncio
    async def test_inserts_reconciliation_log(self, store, mock_pg):
        """log_reconciliation inserts a row into entity.reconciliation_log."""
        await store.log_reconciliation(
            identity_id=1,
            source="discogs",
            external_id="12",
            method="exact_match",
            confidence=1.0,
        )
        mock_pg.execute.assert_called_once()
        call_args = mock_pg.execute.call_args
        assert "entity.reconciliation_log" in call_args[0][0]
        assert "INSERT" in call_args[0][0]


class TestGetIdentitiesByStatus:
    @pytest.mark.asyncio
    async def test_returns_only_matching_status(self, store, mock_pg):
        """get_identities_by_status returns only identities with the given status."""
        mock_pg.fetchall = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "library_name": "Autechre",
                    "discogs_artist_id": None,
                    "wikidata_qid": None,
                    "musicbrainz_artist_id": None,
                    "spotify_artist_id": None,
                    "apple_music_artist_id": None,
                    "bandcamp_id": None,
                    "reconciliation_status": "unreconciled",
                },
                {
                    "id": 3,
                    "library_name": "Stereolab",
                    "discogs_artist_id": None,
                    "wikidata_qid": None,
                    "musicbrainz_artist_id": None,
                    "spotify_artist_id": None,
                    "apple_music_artist_id": None,
                    "bandcamp_id": None,
                    "reconciliation_status": "unreconciled",
                },
            ]
        )
        identities = await store.get_identities_by_status("unreconciled")
        assert len(identities) == 2
        assert all(i.reconciliation_status == "unreconciled" for i in identities)
        call_args = mock_pg.fetchall.call_args
        assert "reconciliation_status" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_matches(self, store, mock_pg):
        """get_identities_by_status returns empty list when no identities match."""
        mock_pg.fetchall = AsyncMock(return_value=[])
        identities = await store.get_identities_by_status("reconciled")
        assert identities == []
