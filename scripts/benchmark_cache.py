#!/usr/bin/env python3
"""Benchmark PG cache vs Discogs API response times for search().

Runs a set of representative queries through both code paths and compares timing.

Environment variables are loaded automatically:
1. From .env file (if present)
2. From Railway linked project (see `railway status`):
   - DISCOGS_TOKEN from library-metadata-lookup service
   - DATABASE_URL_DISCOGS from library-metadata-lookup service

Usage:
    .venv/bin/python scripts/benchmark_cache.py --iterations 3
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any

import asyncpg
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discogs.cache_service import DiscogsCacheService  # noqa: E402
from discogs.memory_cache import clear_all_caches  # noqa: E402
from discogs.models import DiscogsSearchRequest  # noqa: E402
from discogs.ratelimit import reset_rate_limiting  # noqa: E402
from discogs.service import DiscogsService  # noqa: E402

logger = logging.getLogger(__name__)

# Queries drawn from the test suite, covering every edge case category.
# Each mirrors a real scenario exercised by integration or unit tests.
BENCHMARK_QUERIES: list[dict[str, str | None]] = [
    # --- Artist + album (artwork discovery) ---
    {
        "label": "Autechre / Confield",
        "edge": "exact match",
        "artist": "Autechre",
        "album": "Confield",
    },
    {
        "label": "Aphex Twin / RDJ Album",
        "edge": "special chars",
        "artist": "Aphex Twin",
        "album": "Richard D. James Album",
    },
    {
        "label": "Jr Kimbrough / Meet Me",
        "edge": "exact match",
        "artist": "Junior Kimbrough",
        "album": "Meet Me in the City",
    },
    # --- Artist + track-as-album (track validation / album lookup) ---
    {
        "label": "Sneaker Pimps / 6 Und.",
        "edge": "track validation",
        "artist": "Sneaker Pimps",
        "album": "6 Underground",
    },
    {
        "label": "Lush / Thoughtforms",
        "edge": "fallback filter",
        "artist": "Lush",
        "album": "Thoughtforms",
    },
    {
        "label": "Biosphere / Things",
        "edge": "fallback filter",
        "artist": "Biosphere",
        "album": "The Things I Tell You",
    },
    {
        "label": "Sugar Plant / Simple",
        "edge": "compilation fp",
        "artist": "Sugar Plant",
        "album": "Simple",
    },
    {
        "label": "Manu Dibango / Abele",
        "edge": "compilation",
        "artist": "Manu Dibango",
        "album": "Abele Dance",
    },
    {
        "label": "NMH / Holland, 1945",
        "edge": "special chars",
        "artist": "Neutral Milk Hotel",
        "album": "Holland, 1945",
    },
    {
        "label": "Living Color / Cult",
        "edge": "spelling variant",
        "artist": "Living Color",
        "album": "Cult of Personality",
    },
    # --- Artist-only (artist release lookup) ---
    {
        "label": "Echo & the Bunnymen",
        "edge": "artist-only",
        "artist": "Echo and the Bunnymen",
        "album": None,
    },
    {"label": "Young Gov", "edge": "prefix match", "artist": "Young Gov", "album": None},
    {"label": "Toy", "edge": "short name", "artist": "Toy", "album": None},
    {"label": "Laid Back", "edge": "ambiguous name", "artist": "Laid Back", "album": None},
]


def _railway_variables(service: str) -> dict[str, Any]:
    """Fetch variables from a Railway service using the linked CLI project."""
    try:
        result = subprocess.run(
            ["railway", "variables", "--json", "--service", service],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            variables: dict[str, Any] = json.loads(result.stdout)
            return variables
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return {}


def _load_env() -> None:
    """Load environment variables from .env and Railway.

    Railway values override .env since production config is authoritative.
    Uses the Railway CLI linked project (see `railway status`) to fetch
    DISCOGS_TOKEN and DATABASE_URL_DISCOGS from the library-metadata-lookup service.
    """
    load_dotenv()

    svc_vars = _railway_variables("library-metadata-lookup")
    if "DISCOGS_TOKEN" in svc_vars:
        os.environ["DISCOGS_TOKEN"] = svc_vars["DISCOGS_TOKEN"]
    if "DATABASE_URL_DISCOGS" in svc_vars:
        os.environ["DATABASE_URL_DISCOGS"] = svc_vars["DATABASE_URL_DISCOGS"]


async def time_search(service: DiscogsService, query: dict[str, str | None]) -> tuple[float, bool]:
    """Run a single search query and return (time_ms, cached).

    Returns:
        Tuple of (elapsed_ms, was_cached)
    """
    request = DiscogsSearchRequest(artist=query["artist"], album=query.get("album"))

    start = time.perf_counter()
    result = await service.search(request)
    elapsed_ms = (time.perf_counter() - start) * 1000

    return elapsed_ms, result.cached


async def run_benchmark(iterations: int) -> None:
    """Run the full benchmark suite."""
    db_url = os.environ.get("DATABASE_URL_DISCOGS")
    token = os.environ.get("DISCOGS_TOKEN")

    if not token:
        print("ERROR: DISCOGS_TOKEN environment variable is required")
        sys.exit(1)

    # Set up services
    pool = None
    cache_service = None
    if db_url:
        try:
            pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
            cache_service = DiscogsCacheService(pool)
            available = await cache_service.is_available()
            if available:
                print(f"  PG cache connected: {db_url[:40]}...")
            else:
                print("  WARNING: PG cache connected but health check failed")
                cache_service = None
        except Exception as e:
            print(f"  WARNING: Could not connect to PG cache: {e}")
    else:
        print("  DATABASE_URL_DISCOGS not set - cache path will be skipped")

    service_with_cache = DiscogsService(token, cache_service=cache_service)
    service_api_only = DiscogsService(token, cache_service=None)

    print(f"\n  Running {len(BENCHMARK_QUERIES)} queries x {iterations} iterations\n")
    print(
        f"  {'Query':<30} {'Edge Case':<20} "
        f"{'Cache (ms)':>12} {'API (ms)':>12} {'Speedup':>10} {'Cache Hit':>10}"
    )
    print("  " + "-" * 96)

    total_cache_ms = 0.0
    total_api_ms = 0.0
    cache_hits = 0
    total_queries = 0

    for query in BENCHMARK_QUERIES:
        cache_times = []
        api_times = []
        was_cached = False

        for _ in range(iterations):
            # Clear in-memory caches between runs so we measure PG/API, not memory cache
            clear_all_caches()
            reset_rate_limiting()

            # Run with cache (PG + API fallback)
            if cache_service:
                cache_ms, was_cached = await time_search(service_with_cache, query)
                cache_times.append(cache_ms)

            # Clear in-memory caches again for fair API comparison
            clear_all_caches()
            reset_rate_limiting()

            # Run API-only
            api_ms, _ = await time_search(service_api_only, query)
            api_times.append(api_ms)

        avg_cache = sum(cache_times) / len(cache_times) if cache_times else float("nan")
        avg_api = sum(api_times) / len(api_times) if api_times else float("nan")

        if cache_times and avg_cache > 0:
            speedup = f"{avg_api / avg_cache:.1f}x"
        else:
            speedup = "N/A"

        hit_str = "yes" if was_cached else "no"
        cache_str = f"{avg_cache:.0f}" if cache_times else "N/A"

        print(
            f"  {query['label']:<30} {query['edge']:<20} "
            f"{cache_str:>12} {avg_api:>12.0f} {speedup:>10} {hit_str:>10}"
        )

        if cache_times:
            total_cache_ms += avg_cache
            if was_cached:
                cache_hits += 1
        total_api_ms += avg_api
        total_queries += 1

    # Summary
    print("  " + "-" * 96)
    if cache_service:
        avg_cache_total = total_cache_ms / total_queries
        avg_api_total = total_api_ms / total_queries
        overall_speedup = avg_api_total / avg_cache_total if avg_cache_total > 0 else float("inf")
        print(
            f"  {'AVERAGE':<30} {'':<20} "
            f"{avg_cache_total:>12.0f} {avg_api_total:>12.0f} "
            f"{overall_speedup:>9.1f}x {cache_hits}/{total_queries}{'':>5}"
        )
    else:
        avg_api_total = total_api_ms / total_queries
        print(f"  {'AVERAGE (API only)':<30} {'':<20} {'N/A':>12} {avg_api_total:>12.0f}")

    print()

    # Clean up
    await service_with_cache.close()
    await service_api_only.close()
    if pool:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description="Benchmark PG cache vs Discogs API for search()")
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Number of iterations per query (default: 3)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    _load_env()

    print("\n  Discogs Cache Benchmark: search()")
    print("  " + "=" * 40)

    asyncio.run(run_benchmark(args.iterations))


if __name__ == "__main__":
    main()
