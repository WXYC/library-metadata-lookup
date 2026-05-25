"""Source transport protocols for entity resolution.

These are thin protocol definitions mirroring the interface from
``wxyc_catalog.sources.PgSource`` and ``wxyc_catalog.sources.SparqlSource``.
When wxyc_catalog is published, these can be replaced with direct imports.

The implementations here are concrete classes wrapping asyncpg and httpx
for PostgreSQL and Wikidata SPARQL access respectively.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Protocol, runtime_checkable

import asyncpg
import httpx

logger = logging.getLogger(__name__)

_QID_PATTERN = re.compile(r"^Q\d+$")
_RATE_INTERVAL = 1.0  # seconds between Wikidata requests


@runtime_checkable
class PgSourceProtocol(Protocol):
    """Protocol for PostgreSQL source transport."""

    async def fetchall(self, query: str, *args: Any) -> list[dict[str, Any]]: ...
    async def fetchone(self, query: str, *args: Any) -> dict[str, Any] | None: ...
    async def execute(self, query: str, *args: Any) -> str: ...
    def acquire(self) -> Any: ...
    async def close(self) -> None: ...


@runtime_checkable
class SparqlSourceProtocol(Protocol):
    """Protocol for Wikidata SPARQL source transport."""

    async def query(self, sparql: str) -> list[dict[str, Any]]: ...
    async def query_batched(
        self, sparql_template: str, items: list[str]
    ) -> list[dict[str, Any]]: ...
    def binding_value(self, binding: dict, key: str) -> str | None: ...
    def extract_qid(self, uri: str) -> str: ...


class PgSource:
    """PostgreSQL source transport wrapping asyncpg.

    Args:
        dsn: PostgreSQL connection string.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        # Serializes concurrent first-callers to ``_get_pool``. Without it,
        # both callers pass the ``self._pool is None`` check and each await
        # ``asyncpg.create_pool``, orphaning all but one pool with up to 5
        # open connections (FDs). See issue #241.
        self._pool_lock: asyncio.Lock = asyncio.Lock()

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            return self._pool

    async def fetchall(self, query: str, *args: Any) -> list[dict[str, Any]]:
        """Execute a query and return all rows as dicts."""
        pool = await self._get_pool()
        records = await pool.fetch(query, *args)
        return [dict(r) for r in records]

    async def fetchone(self, query: str, *args: Any) -> dict[str, Any] | None:
        """Execute a query and return the first row as a dict, or None."""
        pool = await self._get_pool()
        record = await pool.fetchrow(query, *args)
        if record is None:
            return None
        return dict(record)

    async def execute(self, query: str, *args: Any) -> str:
        """Execute a query and return the status string."""
        pool = await self._get_pool()
        return await pool.execute(query, *args)

    def acquire(self) -> Any:
        """Return an async context manager yielding a pooled connection.

        Use this when a caller needs to run multiple statements inside a
        ``conn.transaction()`` (e.g., ``EntityDeduplicator.merge_group``). The
        pool-level ``fetchall`` / ``fetchone`` / ``execute`` methods autocommit
        each call and can't be grouped into a single transaction.

        Returns an awaitable wrapper because ``_get_pool`` is async. Callers
        use it as::

            async with pg.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(...)
        """

        class _AcquireContext:
            def __init__(self, source: PgSource) -> None:
                self._source = source
                self._ctx: Any = None

            async def __aenter__(self) -> asyncpg.Connection:
                pool = await self._source._get_pool()
                self._ctx = pool.acquire()
                return await self._ctx.__aenter__()

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
                return await self._ctx.__aexit__(exc_type, exc, tb)

        return _AcquireContext(self)

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


class SparqlSource:
    """Wikidata SPARQL source transport.

    Wraps httpx for the Wikidata Query Service SPARQL endpoint.
    Respects rate limits (~1 req/s) and retries on 429/403.

    Args:
        endpoint: SPARQL endpoint URL.
        user_agent: User-Agent header (required by Wikidata).
        batch_size: Max items per VALUES clause in batched queries.
    """

    _DEFAULT_ENDPOINT = "https://query.wikidata.org/sparql"
    _DEFAULT_USER_AGENT = "WXYCEntityResolution/0.1 (https://wxyc.org; engineering@wxyc.org)"

    def __init__(
        self,
        endpoint: str | None = None,
        user_agent: str | None = None,
        batch_size: int = 50,
    ) -> None:
        self._endpoint = endpoint or self._DEFAULT_ENDPOINT
        self._user_agent = user_agent or self._DEFAULT_USER_AGENT
        self._batch_size = batch_size
        self._last_request: float = 0

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < _RATE_INTERVAL:
            time.sleep(_RATE_INTERVAL - elapsed)
        self._last_request = time.time()

    async def query(self, sparql: str) -> list[dict[str, Any]]:
        """Execute a SPARQL query and return result bindings."""
        max_retries = 3
        for attempt in range(max_retries):
            self._rate_limit()
            async with httpx.AsyncClient(
                timeout=60,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "application/sparql-results+json",
                },
            ) as client:
                resp = await client.get(self._endpoint, params={"query": sparql})
                if resp.status_code in (403, 429):
                    backoff = 2 ** (attempt + 1)
                    logger.warning(
                        "Wikidata rate limited (HTTP %d), backing off %ds",
                        resp.status_code,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                resp.raise_for_status()
                return resp.json()["results"]["bindings"]
        logger.warning("SPARQL query failed after %d retries", max_retries)
        return []

    async def query_batched(self, sparql_template: str, items: list[str]) -> list[dict[str, Any]]:
        """Execute a SPARQL query in batches over a VALUES list.

        The template should contain a ``{values}`` placeholder that will be
        replaced with space-separated items for each batch. Substitution uses
        :py:meth:`str.replace` rather than :py:meth:`str.format` because
        SPARQL block syntax uses literal ``{`` and ``}`` everywhere — those
        would crash ``str.format`` with ``ValueError: unexpected '{' in field
        name``. Templates therefore stay readable as raw SPARQL with no brace
        doubling.

        Args:
            sparql_template: SPARQL template with ``{values}`` placeholder.
            items: Items to batch into the VALUES clause.

        Returns:
            Combined bindings from all batches.
        """
        all_bindings: list[dict[str, Any]] = []
        for i in range(0, len(items), self._batch_size):
            batch = items[i : i + self._batch_size]
            values_str = " ".join(batch)
            sparql = sparql_template.replace("{values}", values_str)
            bindings = await self.query(sparql)
            all_bindings.extend(bindings)
        return all_bindings

    @staticmethod
    def binding_value(binding: dict, key: str) -> str | None:
        """Extract a string value from a SPARQL result binding."""
        if key in binding and isinstance(binding[key], dict):
            return binding[key].get("value")
        return None

    @staticmethod
    def extract_qid(uri: str) -> str:
        """Extract QID from a Wikidata entity URI."""
        return uri.rsplit("/", 1)[-1]
