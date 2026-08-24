"""The LML#1245 measurement harness must refuse to measure a degraded pool.

An unusable artist-name pool suppresses fuzzy correction entirely, so every
figure the harness would print reads 0.0 false corrections and 0.0% recall --
perfect-looking precision garbage -- while the script's ``logging.ERROR``
level hides the one warning that says why. A schema drift has to abort the
run, not silently skew every row of it.
"""

from pathlib import Path

import pytest

from library.db import LibraryDB, clear_library_caches
from scripts.measure_artist_correction import _connect_for_measurement, _write_catalog


@pytest.mark.asyncio
async def test_an_unusable_pool_aborts_the_run_instead_of_printing_zeros(tmp_path, monkeypatch):
    db_path = _write_catalog(["Stereolab", "Sessa"], Path(tmp_path))
    clear_library_caches()
    # The drift shape: a source column the catalog does not have. The pool
    # build catches the failed read, marks itself unusable, and correction
    # goes quiet -- which a measurement run must treat as a broken harness,
    # never as a perfectly precise variant.
    monkeypatch.setattr(
        LibraryDB,
        "_artist_name_sources",
        lambda self: [("library", "artist"), ("library", "no_such_column")],
    )
    with pytest.raises(RuntimeError, match="pool"):
        await _connect_for_measurement(db_path)


@pytest.mark.asyncio
async def test_a_healthy_pool_connects_normally(tmp_path):
    db_path = _write_catalog(["Stereolab", "Sessa"], Path(tmp_path))
    clear_library_caches()
    db = await _connect_for_measurement(db_path)
    try:
        assert db._artist_name_pool_usable is True
    finally:
        await db.close()
