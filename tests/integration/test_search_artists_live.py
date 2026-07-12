"""Live Discogs smoke for ``DiscogsService.search_artists`` (LML#759).

One ``external_api``-marked test pinning the ``type=artist`` search
response-shape assumptions the bare-name resolver builds on, against a
stable overload family. If Discogs changes the payload shape (``id`` /
``title`` fields), stops returning the full same-name family on page 1,
or drops the "(N)" disambiguator convention, this is the test that goes
red — the unit/pg tiers all run against canned pages.

Run with: ``pytest -m external_api tests/integration/test_search_artists_live.py``
(requires ``DISCOGS_TOKEN``).
"""

from __future__ import annotations

import os
import re

import pytest

from artists.resolver import _fixed_point_form
from discogs.service import DiscogsService


@pytest.mark.external_api
class TestSearchArtistsLive:
    @pytest.mark.asyncio
    async def test_overload_family_shape_assumptions(self):
        """ "Popsicle" is a stable overload family on Discogs (the Swedish
        band plus at least one "(N)"-suffixed namesake — the #759 design's
        own motivating example). Pins, in one page: typed ids + raw titles
        parse, the exact-form family has >= 2 members (the resolver would
        verdict ``ambiguous``), and the disambiguator convention that the
        identity-match form strips is really there."""
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            results = await service.search_artists("Popsicle")
        finally:
            await service.close()

        if results is None:
            # "Couldn't ask" (429-exhausted / network / distrusted page) —
            # the token is shared with prod, so limiter contention is
            # realistic. A skip preserves the smoke's actual signal
            # (payload-shape drift); failing here would train operators
            # to ignore its reds.
            pytest.skip("Discogs probe unanswerable this run (rate-limit/transient)")
        assert results, "Expected at least one artist hit for 'Popsicle'"
        for r in results:
            assert isinstance(r.artist_id, int)
            assert isinstance(r.title, str) and r.title

        # Fixed-point on both sides — the same comparison the resolver
        # makes, so the family this smoke counts is the family the
        # verdict table would see.
        form = _fixed_point_form("Popsicle")
        family = [r for r in results if _fixed_point_form(r.title) == form]
        family_ids = {r.artist_id for r in family}
        assert len(family_ids) >= 2, (
            "Expected an overloaded exact-form family for 'Popsicle' on page 1; "
            f"got {[(r.artist_id, r.title) for r in family]}. If Discogs merged "
            "the namesakes, pick a new stable overload family for this smoke."
        )
        # The "(N)" disambiguator convention is load-bearing for overload
        # detection — at least one family member must carry it.
        assert any(re.search(r"\(\d+\)$", r.title) for r in family), (
            f"No '(N)'-suffixed member in the family: {[r.title for r in family]}"
        )
