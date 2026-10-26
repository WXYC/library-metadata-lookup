"""``tests/factories.py::library_catalog``: the catalog is closed however the block ends."""

import pytest

from tests.factories import library_catalog


class TestLibraryCatalog:
    @pytest.mark.asyncio
    async def test_yields_a_connected_catalog_and_closes_it_on_exit(self, tmp_path):
        async with library_catalog(tmp_path, [("Stereolab", "Aluminum Tunes")]) as db:
            assert db._conn is not None
        assert db._conn is None

    @pytest.mark.asyncio
    async def test_closes_the_catalog_when_the_block_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="boom"):
            async with library_catalog(tmp_path, [("Stereolab", "Aluminum Tunes")]) as db:
                raise RuntimeError("boom")
        assert db._conn is None

    @pytest.mark.asyncio
    async def test_passes_call_letters_through(self, tmp_path):
        async with library_catalog(
            tmp_path, [("Stereolab", "Aluminum Tunes")], call_letters={"Stereolab": "R"}
        ) as db:
            cursor = await db._conn.execute("SELECT call_letters FROM library")
            assert [row[0] for row in await cursor.fetchall()] == ["R"]
