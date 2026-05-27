"""Unit tests for entity store CRUD operations."""

from unittest.mock import AsyncMock

import pytest

from scripts.entity_resolution.store import (
    _GET_IDENTITY_LOWER_SQL,
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


@pytest.fixture
def store_tx(mock_pg_tx):
    """EntityStore wired to the shared transactional mock-pg fixture.

    ``mock_pg_tx`` lives in ``tests/unit/conftest.py`` so it's reusable across
    tests of any store method that runs ``acquire() + conn.transaction()``.
    """
    return EntityStore(mock_pg_tx)


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
        # Assert against the SQL constant rather than substring-matching the
        # SQL text — keeps the test sensitive to the right thing (which
        # query ran) without breaking on whitespace / formatting changes.
        leg_2_sql = mock_pg.fetchone.await_args_list[1][0][0]
        assert leg_2_sql == _GET_IDENTITY_LOWER_SQL
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

    @pytest.mark.asyncio
    async def test_empty_string_input_returns_none_cleanly(self, store, mock_pg):
        """Empty input must not raise and must not exercise leg 3.

        Leg 1 binds the empty string and misses; leg 2 the same; leg 3's
        canonical form is also empty, so the `if canonical and ...` guard
        skips it. Net: 2 queries, None returned.
        """
        mock_pg.fetchone = AsyncMock(return_value=None)
        identity = await store.resolve_library_name("")
        assert identity is None
        assert mock_pg.fetchone.await_count == 2

    @pytest.mark.asyncio
    async def test_leg_2_lower_sql_includes_order_by_id_for_determinism(self, store, mock_pg):
        """Locks the deterministic tie-break.

        When two stored rows lower-match (e.g., both ``'Stereolab'`` and
        ``'stereolab'`` exist — `library_name`'s UNIQUE constraint is
        case-sensitive, so this is allowed), `ORDER BY id ASC` guarantees
        the oldest row wins. Without it the row PG returns can flip between
        deploys / connection pools.
        """
        mock_pg.fetchone = AsyncMock(
            side_effect=[
                None,  # leg 1 miss
                self._row("Stereolab", id=42, discogs_artist_id=2154),
            ]
        )
        await store.resolve_library_name("stereolab")
        leg_2_sql = mock_pg.fetchone.await_args_list[1][0][0]
        # Both clauses must be present — `LIMIT 1` without `ORDER BY` would
        # be non-deterministic on ambiguous case.
        assert "ORDER BY id ASC" in leg_2_sql
        assert "LIMIT 1" in leg_2_sql


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


class TestFetchAllIdentityLibraryNames:
    """LML#377 orphan-pass primitive: set of every stored library_name."""

    @pytest.mark.asyncio
    async def test_returns_set_of_names(self, store, mock_pg):
        mock_pg.fetchall = AsyncMock(
            return_value=[
                {"library_name": "Stereolab"},
                {"library_name": "Autechre"},
                {"library_name": "Nilüfer Yanya"},
            ]
        )
        names = await store.fetch_all_identity_library_names()
        assert names == {"Stereolab", "Autechre", "Nilüfer Yanya"}
        # Set type matters: callers do O(N) set-difference, not list scans.
        assert isinstance(names, set)

    @pytest.mark.asyncio
    async def test_returns_empty_set_when_table_empty(self, store, mock_pg):
        mock_pg.fetchall = AsyncMock(return_value=[])
        names = await store.fetch_all_identity_library_names()
        assert names == set()


class TestDeleteIdentityByLibraryName:
    """LML#377 orphan-pass primitive: hard DELETE with log-row cleanup.

    `entity.reconciliation_log.identity_id` has no ON DELETE CASCADE, so the
    method must remove log rows first, then the identity row, both inside a
    single transaction.
    """

    @pytest.mark.asyncio
    async def test_deletes_log_rows_before_identity(self, store_tx, mock_pg_tx):
        """Log DELETE precedes identity DELETE; both run on the acquired conn."""
        conn = mock_pg_tx._mock_conn
        conn.fetchrow = AsyncMock(return_value={"id": 42, "library_name": "Beyonce"})
        conn.execute = AsyncMock(side_effect=["DELETE 3", "DELETE 1"])

        log_rows_deleted = await store_tx.delete_identity_by_library_name("Beyonce")

        assert log_rows_deleted == 3
        # Two execute calls — log delete first, then identity delete.
        assert conn.execute.await_count == 2
        first_sql = conn.execute.await_args_list[0][0][0]
        second_sql = conn.execute.await_args_list[1][0][0]
        assert "DELETE FROM entity.reconciliation_log" in first_sql
        assert "DELETE FROM entity.identity" in second_sql

    @pytest.mark.asyncio
    async def test_no_op_when_identity_missing(self, store_tx, mock_pg_tx):
        """fetchrow returns None → no DELETEs issued, return 0."""
        conn = mock_pg_tx._mock_conn
        conn.fetchrow = AsyncMock(return_value=None)

        log_rows_deleted = await store_tx.delete_identity_by_library_name("Nonexistent")

        assert log_rows_deleted == 0
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_in_single_transaction(self, store_tx, mock_pg_tx):
        """One acquire(), one transaction(), all statements on the same conn."""
        conn = mock_pg_tx._mock_conn
        conn.fetchrow = AsyncMock(return_value={"id": 1, "library_name": "X"})
        conn.execute = AsyncMock(return_value="DELETE 0")

        await store_tx.delete_identity_by_library_name("X")

        mock_pg_tx.acquire.assert_called_once()
        conn.transaction.assert_called_once()
        conn._mock_tx_ctx.__aenter__.assert_awaited_once()
        conn._mock_tx_ctx.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_strips_nul_from_input(self, store_tx, mock_pg_tx):
        """U+0000 in input is stripped before the lookup query."""
        conn = mock_pg_tx._mock_conn
        conn.fetchrow = AsyncMock(return_value=None)

        await store_tx.delete_identity_by_library_name("Bey\x00once")

        bind_arg = conn.fetchrow.await_args_list[0][0][1]
        assert bind_arg == "Beyonce"


class TestMergeIdentityByLibraryName:
    """LML#377 orphan-pass primitive: merge an orphan into a canonical row.

    Preserves accumulated reconciliation_log provenance across renames like
    ``"Beyonce"`` → ``"Beyoncé"``. Adapted from `EntityDeduplicator.merge_group`;
    same UPDATE-then-reassign-then-DELETE shape inside a single transaction.
    """

    @pytest.mark.asyncio
    async def test_unreconciled_orphan_merges_without_status_upgrade(self, store_tx, mock_pg_tx):
        """Orphan with status=unreconciled: 3 executes (no status upgrade)."""
        conn = mock_pg_tx._mock_conn
        conn.fetchrow = AsyncMock(
            return_value={
                "id": 17,
                "library_name": "Beyonce",
                "discogs_artist_id": 1419,
                "wikidata_qid": None,
                "musicbrainz_artist_id": "mbid-1",
                "spotify_artist_id": "spotify-1",
                "apple_music_artist_id": None,
                "bandcamp_id": None,
                "reconciliation_status": "unreconciled",
            }
        )
        conn.execute = AsyncMock(return_value="UPDATE 1")

        merged = await store_tx.merge_identity_by_library_name(from_name="Beyonce", into_id=42)

        assert merged is True
        # Three execute calls: UPDATE merged, REASSIGN logs, DELETE from row.
        # No status upgrade because the orphan was unreconciled.
        assert conn.execute.await_count == 3
        update_sql = conn.execute.await_args_list[0][0][0]
        reassign_sql = conn.execute.await_args_list[1][0][0]
        delete_sql = conn.execute.await_args_list[2][0][0]
        assert "UPDATE entity.identity" in update_sql
        assert "COALESCE" in update_sql
        assert "UPDATE entity.reconciliation_log" in reassign_sql
        assert "DELETE FROM entity.identity" in delete_sql

        # UPDATE binds into_id first, then the from row's external IDs + QID.
        update_args = conn.execute.await_args_list[0][0]
        assert update_args[1] == 42  # into_id
        assert update_args[2] == 1419  # discogs_artist_id from the orphan
        assert update_args[3] == "mbid-1"
        assert update_args[4] == "spotify-1"
        assert update_args[5] is None  # apple_music_artist_id
        assert update_args[6] is None  # bandcamp_id
        assert update_args[7] is None  # wikidata_qid

        # REASSIGN binds (new_id, old_id).
        reassign_args = conn.execute.await_args_list[1][0]
        assert reassign_args[1] == 42  # into_id (new)
        assert reassign_args[2] == 17  # from_id (old)

        # DELETE targets the from row's id.
        delete_args = conn.execute.await_args_list[2][0]
        assert delete_args[1] == 17

    @pytest.mark.asyncio
    async def test_reconciled_orphan_upgrades_target_status_and_carries_qid(
        self, store_tx, mock_pg_tx
    ):
        """Orphan with status=reconciled + wikidata_qid: target gets both via merge.

        Locks the LML#377 contract that the orphan-pass merge carries the
        Wikidata QID and the reconciliation status across, not just the
        non-QID external IDs. Without this, a "Beyonce" → "Beyoncé" rename
        would silently drop the QID (set by the Wikidata bridge) and leave
        the freshly-seeded target row at status='unreconciled', which would
        trigger a wasted re-fetch on the next reconciliation pass.
        """
        conn = mock_pg_tx._mock_conn
        conn.fetchrow = AsyncMock(
            return_value={
                "id": 17,
                "library_name": "Beyonce",
                "discogs_artist_id": 1419,
                "wikidata_qid": "Q36153",
                "musicbrainz_artist_id": "mbid-1",
                "spotify_artist_id": "spotify-1",
                "apple_music_artist_id": None,
                "bandcamp_id": None,
                "reconciliation_status": "reconciled",
            }
        )
        conn.execute = AsyncMock(return_value="UPDATE 1")

        merged = await store_tx.merge_identity_by_library_name(from_name="Beyonce", into_id=42)

        assert merged is True
        # Four execute calls: UPDATE merged, UPDATE status, REASSIGN logs, DELETE.
        assert conn.execute.await_count == 4
        update_merged_args = conn.execute.await_args_list[0][0]
        update_status_args = conn.execute.await_args_list[1][0]

        # _UPDATE_MERGED_SQL carries the QID at $7.
        assert update_merged_args[1] == 42  # into_id
        assert update_merged_args[7] == "Q36153"
        assert "wikidata_qid" in update_merged_args[0]

        # _UPDATE_STATUS_SQL is called with (into_id, 'reconciled').
        assert "UPDATE entity.identity" in update_status_args[0]
        assert "reconciliation_status" in update_status_args[0]
        assert update_status_args[1] == 42
        assert update_status_args[2] == "reconciled"

    @pytest.mark.asyncio
    async def test_no_op_when_from_name_missing(self, store_tx, mock_pg_tx):
        """fetchrow None → no merge, return False, no executes."""
        conn = mock_pg_tx._mock_conn
        conn.fetchrow = AsyncMock(return_value=None)

        merged = await store_tx.merge_identity_by_library_name(from_name="Nonexistent", into_id=42)

        assert merged is False
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_when_from_equals_into(self, store_tx, mock_pg_tx):
        """If from_name's id equals into_id, no-op (return False) — re-invocation safe."""
        conn = mock_pg_tx._mock_conn
        conn.fetchrow = AsyncMock(
            return_value={
                "id": 42,
                "library_name": "X",
                "discogs_artist_id": None,
                "wikidata_qid": None,
                "musicbrainz_artist_id": None,
                "spotify_artist_id": None,
                "apple_music_artist_id": None,
                "bandcamp_id": None,
                "reconciliation_status": "reconciled",
            }
        )

        merged = await store_tx.merge_identity_by_library_name(from_name="X", into_id=42)

        assert merged is False
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_in_single_transaction(self, store_tx, mock_pg_tx):
        """Three statements all on the same conn under one transaction."""
        conn = mock_pg_tx._mock_conn
        conn.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "library_name": "X",
                "discogs_artist_id": 99,
                "wikidata_qid": None,
                "musicbrainz_artist_id": None,
                "spotify_artist_id": None,
                "apple_music_artist_id": None,
                "bandcamp_id": None,
                "reconciliation_status": "reconciled",
            }
        )

        await store_tx.merge_identity_by_library_name(from_name="X", into_id=2)

        mock_pg_tx.acquire.assert_called_once()
        conn.transaction.assert_called_once()
