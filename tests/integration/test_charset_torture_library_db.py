"""Charset-torture round-trip CI for library.db (SQLite + FTS5).

Implements WX-1.2.1 for the SQLite leg of LML's primary storage. Drives the
shared corpus from `tests/fixtures/charset-torture.json` (pinned via
`charset-torture.json.sha256`; drift-guarded by the M3.2 workflow at
`.github/workflows/charset-corpus-drift.yml`).

Two complementary checks per entry:

* **Storage round-trip** — write `entry["input"]` directly into the `library`
  table and read it back via `SELECT`. Asserts SQLite preserves the bytes.
  Expected to pass universally; failure means the SQLite write/read path is
  losing data, which would be a WX-3 storage-layer bug.

* **Search round-trip** — write the entry, then locate the row via
  `LibraryDB.search()`. Tests the FTS5 + LIKE-fallback chain. The WX-1 plan's
  expected-failure list (Greek/Cyrillic/CJK/Arabic) turns out to be overly
  pessimistic — LibraryDB's LIKE fallback handles most non-Latin scripts.
  The honest failure surface today is bare 4-byte emoji codepoints and ZWJ
  graphemes; both fail the FTS5 unicode61 tokenizer's word-boundary
  heuristics. Failures are tagged `[lml:fts5-supplementary-plane]` /
  `[lml:zwj-grapheme]` per the WX-1 tag scheme so WX-3 auditors can grep
  for them and file follow-up boundary fixes.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

from library.db import LibraryDB
from tests.charset_torture import CharsetTortureEntry, entry_id, iter_entries

CORPUS_ENTRIES = list(iter_entries())

# Inputs whose `LibraryDB.search()` round-trip fails today. Keyed by
# (category, input) so additions are explicit and grep-able. WX-3 auditors
# search the codebase by the bracketed tag prefix.
SEARCH_XFAIL_INPUTS: dict[tuple[str, str], str] = {
    ("emoji", "👋"): "[lml:fts5-supplementary-plane] bare 4-byte emoji not tokenized by unicode61",
    ("emoji", "🎵"): "[lml:fts5-supplementary-plane] bare 4-byte emoji not tokenized by unicode61",
    ("emoji", "🎸"): "[lml:fts5-supplementary-plane] bare 4-byte emoji not tokenized by unicode61",
    ("zwj", "👨‍👩‍👧‍👦"): "[lml:zwj-grapheme] ZWJ family sequence not segmented",
    ("zwj", "🇺🇸"): "[lml:zwj-grapheme] regional indicator pair not segmented",
    ("zwj", "👨‍🎤"): "[lml:zwj-grapheme] ZWJ man+microphone not segmented",
}


@pytest_asyncio.fixture
async def empty_library_db():
    """Fresh in-memory LibraryDB with FTS5 schema but no seed rows."""
    db = LibraryDB()
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("""
        CREATE TABLE library (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            call_letters TEXT,
            artist_call_number INTEGER,
            release_call_number INTEGER,
            genre TEXT,
            format TEXT
        )
    """)
    await conn.execute("""
        CREATE VIRTUAL TABLE library_fts USING fts5(
            title, artist,
            content=library,
            content_rowid=id
        )
    """)
    await conn.commit()
    db._conn = conn
    yield db
    await conn.close()


async def _insert_entry(conn: aiosqlite.Connection, row_id: int, value: str) -> None:
    """Insert one corpus entry into the library table and sync FTS index."""
    await conn.execute(
        "INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (row_id, value, value, "ZZ", row_id, 1, "Test", "CD"),
    )
    await conn.execute("INSERT INTO library_fts(library_fts) VALUES('rebuild')")
    await conn.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", CORPUS_ENTRIES, ids=entry_id)
async def test_sqlite_storage_roundtrip(
    empty_library_db: LibraryDB, entry: CharsetTortureEntry
) -> None:
    """SQLite must preserve every corpus entry byte-for-byte on write+read."""
    conn = empty_library_db._conn
    assert conn is not None
    await _insert_entry(conn, row_id=1, value=entry["input"])

    cursor = await conn.execute("SELECT artist, title FROM library WHERE id = ?", (1,))
    row = await cursor.fetchone()
    assert row is not None, f"{entry['category']}: row not found after insert"
    assert row["artist"] == entry["input"], (
        f"{entry['category']}: artist round-trip lost bytes ({entry['notes']})"
    )
    assert row["title"] == entry["input"], (
        f"{entry['category']}: title round-trip lost bytes ({entry['notes']})"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", CORPUS_ENTRIES, ids=entry_id)
async def test_search_roundtrip(
    empty_library_db: LibraryDB, entry: CharsetTortureEntry, request: pytest.FixtureRequest
) -> None:
    """LibraryDB.search() must locate every corpus entry by exact string."""
    xfail_reason = SEARCH_XFAIL_INPUTS.get((entry["category"], entry["input"]))
    if xfail_reason is not None:
        request.applymarker(pytest.mark.xfail(reason=xfail_reason, strict=True))

    conn = empty_library_db._conn
    assert conn is not None
    await _insert_entry(conn, row_id=1, value=entry["input"])

    results = await empty_library_db.search(query=entry["input"], limit=10)
    assert any(r.id == 1 for r in results), (
        f"{entry['category']}: search() did not find row "
        f"({entry['notes']!r}); got {[r.artist for r in results]}"
    )
