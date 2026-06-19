"""Charset-torture round-trip CI for entity.identity (PostgreSQL).

Implements WX-1.2.1 for the PG leg of LML's primary storage. The
``entity.identity`` table is the canonical name index consumed by
semantic-index (via ``--entity-source=lml``); a byte-corrupting write
here propagates into every downstream consumer of ``EntityStore``.

Each corpus entry round-trips through ``EntityStore.upsert_identity`` →
``get_identity``. We assert byte equality of ``library_name``. The
table's UNIQUE constraint on ``library_name`` is the only normalization
that could matter; PostgreSQL's TEXT type preserves bytes verbatim, so
this test catches any future regression where the column type changes
or a normalization step is silently inserted.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from entity.sources import PgSource
from entity.store import EntityStore
from tests.charset_torture import CharsetTortureEntry, entry_id, iter_entries

# ``pg_pool`` (max_size=3) lives in conftest; ``DATABASE_URL`` is imported from
# there for the ``source._dsn`` wiring below.
from tests.integration.conftest import DATABASE_URL

CORPUS_ENTRIES = list(iter_entries())

# Inputs whose stored form differs from ``entry["input"]`` because
# ``EntityStore`` strips them at the write boundary. PostgreSQL TEXT cannot
# carry U+0000 (SQL standard 22021); per WXYC/docs#18 the org-wide policy is
# to strip at every PG TEXT write boundary. The corpus keeps the original
# input bytes — this map records what comes back out. The vendored
# ``charset-torture.json`` is SHA-pinned to ``@wxyc/shared``; this override
# lives in-repo because the strip is an LML-side decision and the corpus's
# ``expected_storage`` field remains the upstream byte-preservation contract.
PG_TEXT_STRIP_OVERRIDES: dict[tuple[str, str], str] = {
    ("quoting", "null\x00byte"): "nullbyte",
}


@pytest_asyncio.fixture
async def entity_store(pg_pool):
    """EntityStore over a freshly-created entity schema."""
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")
        await conn.execute("CREATE SCHEMA entity")
        await conn.execute("""
            CREATE TABLE entity.identity (
                id SERIAL PRIMARY KEY,
                library_name TEXT NOT NULL UNIQUE,
                discogs_artist_id INTEGER,
                wikidata_qid TEXT,
                musicbrainz_artist_id TEXT,
                spotify_artist_id TEXT,
                apple_music_artist_id TEXT,
                bandcamp_id TEXT,
                reconciliation_status TEXT NOT NULL DEFAULT 'unreconciled',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    source = PgSource.__new__(PgSource)
    source._dsn = DATABASE_URL
    source._pool = pg_pool
    yield EntityStore(source)
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")


@pytest.mark.pg
@pytest.mark.asyncio
@pytest.mark.parametrize("entry", CORPUS_ENTRIES, ids=entry_id)
async def test_entity_identity_storage_roundtrip(
    entity_store: EntityStore,
    entry: CharsetTortureEntry,
    request: pytest.FixtureRequest,
) -> None:
    """upsert_identity → get_identity must preserve library_name byte-for-byte.

    Exception: inputs in ``PG_TEXT_STRIP_OVERRIDES`` are stripped at the write
    boundary (see WXYC/docs#18) and read back in their stripped form.
    """
    expected_stored = PG_TEXT_STRIP_OVERRIDES.get(
        (entry["category"], entry["input"]), entry["input"]
    )

    upserted = await entity_store.upsert_identity(library_name=entry["input"])
    assert upserted is not None, f"{entry['category']}: upsert returned None"
    assert upserted.library_name == expected_stored, (
        f"{entry['category']}: upsert lost bytes ({entry['notes']})"
    )

    fetched = await entity_store.get_identity(entry["input"])
    assert fetched is not None, (
        f"{entry['category']}: get_identity could not locate row ({entry['notes']!r})"
    )
    assert fetched.library_name == expected_stored, (
        f"{entry['category']}: read-back lost bytes ({entry['notes']})"
    )
    assert fetched.id == upserted.id
