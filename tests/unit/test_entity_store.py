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


class TestResolveLibraryName:
    """Three-leg fall-through lookup per WXYC/library-metadata-lookup#276.

    `resolve_library_name()` is the bulk-resolve-libraries handler's lookup
    path. The #276 production audit found `entity.identity.library_name` rows
    are stored in verbatim Backend casing (99.8% mixed-case, articles
    preserved, `&` preserved). A canonical-only lookup misses almost
    everything; the legacy exact-match handles the dominant path.

    The method tries three legs in order, returning the first hit:

    1. **Exact match** — `WHERE library_name = $verbatim_input`. Catches
       the 99.8% dominant case where Backend's input shape equals the
       stored shape. Uses the unique index.
    2. **Case-insensitive match** — `WHERE LOWER(library_name) = LOWER($verbatim_input)`.
       Catches pure case-drift (Backend posts ``"stereolab"`` against stored
       ``"Stereolab"``, or vice-versa). Seq-scans ~23K rows without a
       functional index but stays sub-millisecond at that size.
    3. **Canonical match** — `WHERE library_name = $canonical_input`.
       Catches inputs whose canonical form happens to equal a stored
       canonical row (only ~0.2% of stored rows per the #276 audit are
       already canonical, but it's free correctness for those rows and
       the eventual full backfill).

    Short-circuits: legs 2 and 3 are skipped when their bind value
    duplicates an earlier leg's (e.g., input ``"stereolab"`` is its own
    lower-form, so leg 2 is redundant). The exact-match leg always runs.

    Net hit rate is strictly ≥ legacy `get_identity()` exact-match; the
    additional legs are pure additive coverage.
    """

    @staticmethod
    def _row(library_name: str, **overrides: object) -> dict[str, object]:
        return {
            "id": 1,
            "library_name": library_name,
            "discogs_artist_id": None,
            "wikidata_qid": None,
            "musicbrainz_artist_id": None,
            "spotify_artist_id": None,
            "apple_music_artist_id": None,
            "bandcamp_id": None,
            "reconciliation_status": "reconciled",
            **overrides,
        }

    @pytest.mark.asyncio
    async def test_exact_match_short_circuits_other_legs(self, store, mock_pg):
        """Verbatim input hits row on leg 1 → no further queries.

        This is the dominant production path: 99.8% of stored rows are in
        verbatim casing, so the exact-match leg handles them in one query.
        """
        mock_pg.fetchone = AsyncMock(
            return_value=self._row("Stereolab", id=42, discogs_artist_id=2154)
        )
        identity = await store.resolve_library_name("Stereolab")
        assert identity is not None
        assert identity.id == 42
        assert identity.discogs_artist_id == 2154
        # One query — leg 1 alone.
        assert mock_pg.fetchone.await_count == 1
        # First leg uses the verbatim input (NUL-stripped, otherwise unchanged).
        assert mock_pg.fetchone.await_args_list[0][0][1] == "Stereolab"

    @pytest.mark.asyncio
    async def test_falls_through_to_case_insensitive_leg(self, store, mock_pg):
        """Leg 1 misses, leg 2 (LOWER both sides) hits via case-insensitive match.

        Backend posts ``"stereolab"``; stored is ``"Stereolab"``. Leg 1 misses
        because the bind is the verbatim lowercase input. Leg 2 queries
        ``WHERE LOWER(library_name) = LOWER('stereolab')`` and hits.
        """
        mock_pg.fetchone = AsyncMock(
            side_effect=[
                None,  # leg 1 miss
                self._row("Stereolab", id=42, discogs_artist_id=2154),
            ]
        )
        identity = await store.resolve_library_name("stereolab")
        assert identity is not None
        assert identity.id == 42
        assert mock_pg.fetchone.await_count == 2
        # Leg 2 SQL contains LOWER(...) — assert via the SQL string itself,
        # not the bind value (the bind is still the verbatim input).
        leg_2_sql = mock_pg.fetchone.await_args_list[1][0][0]
        assert "lower(library_name)" in leg_2_sql.lower()
        # Canonical leg is skipped (canonical(input) equals input).
        # No third query.

    @pytest.mark.asyncio
    async def test_falls_through_to_canonical_leg(self, store, mock_pg):
        """All three legs fire when the input diverges beyond case alone.

        Input ``"Nilüfer Yanya"`` (diacritic) against a stored canonical
        row ``"nilufer yanya"``:
        - Leg 1 (verbatim ``"Nilüfer Yanya"``) misses
        - Leg 2 (`LOWER(library_name) = LOWER("Nilüfer Yanya")`) misses
          (`"Nilüfer Yanya".lower()` keeps the umlaut)
        - Leg 3 (canonical ``"nilufer yanya"``) hits
        """
        mock_pg.fetchone = AsyncMock(
            side_effect=[
                None,  # leg 1
                None,  # leg 2
                self._row("nilufer yanya", id=7, discogs_artist_id=5499521),
            ]
        )
        identity = await store.resolve_library_name("Nilüfer Yanya")
        assert identity is not None
        assert identity.id == 7
        assert mock_pg.fetchone.await_count == 3
        # Leg 3 uses the canonical form.
        assert mock_pg.fetchone.await_args_list[2][0][1] == "nilufer yanya"

    @pytest.mark.asyncio
    async def test_returns_none_when_all_legs_miss(self, store, mock_pg):
        """Input with diacritic forces all three legs (canonical ≠ verbatim.lower)."""
        mock_pg.fetchone = AsyncMock(return_value=None)
        identity = await store.resolve_library_name("Nilüfer Yanya")
        assert identity is None
        assert mock_pg.fetchone.await_count == 3

    @pytest.mark.asyncio
    async def test_lowercase_input_runs_legs_1_and_2_only(self, store, mock_pg):
        """Already-lowercase input → leg 1 (verbatim) + leg 2 (LOWER) only.

        Backend posts ``"stereolab"``. Leg 1's `WHERE library_name =
        'stereolab'` misses if storage is mixed-case; leg 2's
        `WHERE LOWER(library_name) = LOWER('stereolab')` catches it. Leg 3's
        canonical form is also ``"stereolab"`` — that bind equals
        ``verbatim.lower()`` so leg 2's superset already covered it. Skip.
        """
        mock_pg.fetchone = AsyncMock(return_value=None)
        await store.resolve_library_name("stereolab")
        assert mock_pg.fetchone.await_count == 2

    @pytest.mark.asyncio
    async def test_mixed_case_ascii_skips_canonical_leg(self, store, mock_pg):
        """Mixed-case ASCII input with no normalization differences → 2 queries.

        Input ``"Stereolab"``: leg 1 verbatim, leg 2 case-insensitive,
        leg 3 canonical form is ``"stereolab"`` which equals
        ``verbatim.lower()`` — leg 2 already covers it.
        """
        mock_pg.fetchone = AsyncMock(return_value=None)
        await store.resolve_library_name("Stereolab")
        assert mock_pg.fetchone.await_count == 2


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
