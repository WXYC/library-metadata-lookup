"""Integration test for the trio-compilation acceptance criterion from #237.

See WXYC/library-metadata-lookup#319.

The motivating case: ``Orcutt Shelley Miller`` / ``Orcutt Shelley Miller`` /
``A Star Is Born`` should return release 34993109 in ``results`` after the
album-title fallback lands. The two existing parallel probes in
``search_compilations_for_track`` can't return this release on their own
(no single canonical artist entity for the trio).

Marked ``external_api`` — hits the real Discogs API; self-skips if
``DISCOGS_TOKEN`` is unset.
"""

from __future__ import annotations

import os

import pytest

from discogs.service import DiscogsService

DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN")

pytestmark = [
    pytest.mark.external_api,
    pytest.mark.skipif(not DISCOGS_TOKEN, reason="DISCOGS_TOKEN not set"),
]


@pytest.mark.asyncio
async def test_search_releases_by_album_title_finds_orcutt_shelley_miller():
    """The new service method must surface release 34993109 when queried with
    its album title alone."""
    service = DiscogsService(token=DISCOGS_TOKEN)
    response = await service.search_releases_by_album_title("Orcutt Shelley Miller", limit=20)

    release_ids = [r.release_id for r in (response.releases or [])]
    assert 34993109 in release_ids, (
        "Expected release 34993109 in title-only compilation search; "
        f"got {len(release_ids)} releases: {release_ids[:10]}"
    )
