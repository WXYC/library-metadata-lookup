"""Payload-shape stability for the Wikipedia-preferred-bio counters
(LML#513/#1192 Phase B), mirroring ``tests/unit/test_rowless_flag_observability.py``'s
``TestPayloadShapeStability``.

``wikipedia_bio_fetch_ok`` / ``wikipedia_bio_fetch_reject`` are deliberately
NOT seeded here — they fire from ``lookup/enrichment/wikipedia_warm.py``'s
background task, which runs detached from any request's cache_stats
context, via a direct (unsampled) PostHog capture instead. See that
module's docstring.
"""

from __future__ import annotations

from wxyc_fastapi.observability import get_cache_stats, init_cache_stats

from lookup.enrichment.wikipedia_bio import (
    CACHE_HIT_STAT_KEY,
    CACHE_MISS_WARM_SCHEDULED_STAT_KEY,
    CACHE_NEGATIVE_STAT_KEY,
    FALLBACK_DISCOGS_STAT_KEY,
    SERVED_STAT_KEY,
)
from lookup.enrichment.wikipedia_warm import WARM_SHED_STAT_KEY
from lookup.router import _LML_CACHE_STATS_EXTRA_KEYS


class TestPayloadShapeStability:
    def test_all_new_keys_seeded(self):
        for key in (
            CACHE_HIT_STAT_KEY,
            CACHE_NEGATIVE_STAT_KEY,
            CACHE_MISS_WARM_SCHEDULED_STAT_KEY,
            SERVED_STAT_KEY,
            FALLBACK_DISCOGS_STAT_KEY,
            WARM_SHED_STAT_KEY,
        ):
            assert key in _LML_CACHE_STATS_EXTRA_KEYS

    def test_seeded_keys_default_to_zero(self):
        init_cache_stats(extra_keys=_LML_CACHE_STATS_EXTRA_KEYS)
        stats = get_cache_stats()
        assert stats[CACHE_HIT_STAT_KEY] == 0
        assert stats[WARM_SHED_STAT_KEY] == 0
