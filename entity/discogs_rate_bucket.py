"""Shared cross-process Discogs rate limiter: a PG-backed token bucket (LML#841).

Each LML process bounds its own Discogs traffic with an in-memory
``AsyncLimiter`` (``discogs/ratelimit.py``). That is per-process, but prod and
staging share ONE Discogs application token against ONE upstream 60/min bucket
(``reference_lml_staging_shares_prod``). N uncoordinated limiters can therefore
collectively exceed the shared budget, 429-tripping the LML#755 saturation
breaker. This module moves the *rate* dimension to a single shared row in the
LML-owned ``lml_cache.discogs_rate_bucket`` table so every process draws permits
from one lazily-refilled token bucket — N processes meter against a single shared
budget (one global burst envelope) instead of N uncoordinated ones. (The envelope
is the same as a single ``AsyncLimiter(rate, 60)``: seeded full, a cold bucket can
still burst up to ``capacity`` immediately; the win is that the burst is shared
globally, not multiplied per process — not a strict rate-in-any-window cap.)

Design split (deliberate):

* ``PgTokenBucket.try_acquire`` is a thin, pure PG primitive: ONE atomic
  ``UPDATE … RETURNING`` that refills-by-elapsed-time and spends a token in a
  single statement. It either grants (``allowed=True``) or reports how long
  until the next token (``retry_after_s``). It raises on any PG error or missing
  row — it has no opinion about retries, timeouts, or fallback.
* All resilience *policy* — the enable flag, per-round-trip timeout, fail-open
  to the local limiter, and the queue-until-a-token loop — lives one layer up in
  ``DiscogsRateGate`` (``discogs/ratelimit.py``). Keeping the timeout at the
  round-trip granularity (not around the whole queue wait) means a legitimately
  empty bucket queues normally while a *slow/unreachable* discogs-cache PG fails
  open fast.

Atomicity: the ``SELECT … FOR UPDATE`` CTE locks the single row for the duration
of the (implicitly-transactional) statement, so concurrent acquirers serialize
on the row lock and can never collectively over-issue beyond ``capacity`` — the
invariant covered by ``tests/integration/test_discogs_rate_bucket.py``.

Schema ownership: ``lml_cache.*`` is LML-owned (discogs-etl#288, Option 3),
lifespan-bootstrapped via ``IF NOT EXISTS`` / ``ON CONFLICT DO NOTHING`` — no
alembic, no discogs-cache coordination. Canonical DDL reference lives in the
sibling ``discogs_rate_bucket.sql``; this module is the runtime source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from entity.sources import PgSource

logger = logging.getLogger(__name__)

_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS lml_cache"

_DDL_TABLE = """\
CREATE TABLE IF NOT EXISTS lml_cache.discogs_rate_bucket (
    bucket_key TEXT PRIMARY KEY,
    tokens DOUBLE PRECISION NOT NULL,
    capacity DOUBLE PRECISION NOT NULL,
    refill_per_sec DOUBLE PRECISION NOT NULL,
    last_refill TIMESTAMPTZ NOT NULL DEFAULT now()
)\
"""

# Idempotent seed. ``ON CONFLICT DO NOTHING`` means the first process to boot
# wins the budget; a later boot with a different ``DISCOGS_RATE_LIMIT`` (e.g.
# staging) must NOT oscillate the shared row. Seeded full (``tokens = capacity``)
# so a fresh bucket starts with a burst allowance rather than cold-empty.
_SEED_SQL = """\
INSERT INTO lml_cache.discogs_rate_bucket (bucket_key, tokens, capacity, refill_per_sec)
VALUES ($1, $2, $2, $3)
ON CONFLICT (bucket_key) DO NOTHING\
"""

# The whole limiter, in one atomic statement:
#   1. CTE reads the row FOR UPDATE (locks it), computing the CURRENT available
#      tokens as the stored balance plus whatever accrued since ``last_refill``,
#      capped at ``capacity`` (lazy refill — no background ticker needed).
#   2. The UPDATE spends one token iff at least one is available, and always
#      resets ``last_refill = now()`` so the elapsed-time credit is banked into
#      ``tokens`` exactly once (no double-credit on a denied call).
#   3. RETURNING reports whether a token was granted and, if not, how long until
#      the balance reaches 1 at the steady refill rate.
# Concurrency safety comes entirely from the row lock: two acquirers cannot both
# read the same pre-spend balance.
#
# ``avail`` is clamped to ``[0, capacity]`` via ``GREATEST(0.0, …)`` inside the
# ``LEAST``: capping at ``capacity`` is the lazy-refill burst ceiling, and the
# zero floor keeps a pathological negative balance — a backward wall-clock jump
# making the elapsed-refill term negative — from producing an unbounded
# ``retry_after_s`` or persisting a deeper-negative ``tokens`` (LML#841 review).
_ACQUIRE_SQL = """\
WITH refreshed AS (
    SELECT
        LEAST(
            capacity,
            GREATEST(
                0.0,
                tokens + EXTRACT(EPOCH FROM (now() - last_refill)) * refill_per_sec
            )
        ) AS avail,
        refill_per_sec
    FROM lml_cache.discogs_rate_bucket
    WHERE bucket_key = $1
    FOR UPDATE
)
UPDATE lml_cache.discogs_rate_bucket AS b
SET tokens = CASE WHEN r.avail >= 1 THEN r.avail - 1 ELSE r.avail END,
    last_refill = now()
FROM refreshed r
WHERE b.bucket_key = $1
RETURNING
    (r.avail >= 1) AS allowed,
    CASE
        WHEN r.avail >= 1 THEN 0.0
        ELSE (1 - r.avail) / r.refill_per_sec
    END AS retry_after_s\
"""


class RateBucketMissingRowError(RuntimeError):
    """Raised when the bucket row is absent (never seeded / wrong key).

    Surfaces as a normal exception so ``DiscogsRateGate`` fails open to the
    local limiter rather than hard-erroring the live-probe path.
    """


@dataclass(frozen=True)
class TokenAcquisition:
    """Outcome of a single ``try_acquire`` round-trip.

    ``allowed`` — a token was granted (spend it, proceed).
    ``retry_after_s`` — when denied, seconds until the bucket next holds a full
    token at the steady refill rate; ``0.0`` when allowed.
    """

    allowed: bool
    retry_after_s: float


async def set_up_discogs_rate_bucket_schema(
    pg: PgSource,
    *,
    bucket_key: str,
    capacity: float,
    refill_per_sec: float,
) -> None:
    """Apply the idempotent bucket-schema DDL and seed the row.

    Called once from ``main.py`` lifespan. Schema + table are ``IF NOT EXISTS``
    and the seed is ``ON CONFLICT DO NOTHING``, so re-running on every boot is
    safe and a second process with a different ``capacity``/``refill_per_sec``
    does not clobber the shared budget. If the discogs-cache PG is unreachable
    at startup the caller logs and continues; the gate degrades to the local
    limiter (flag OFF or fail-open) until the next boot.
    """
    await pg.execute(_DDL_SCHEMA)
    await pg.execute(_DDL_TABLE)
    await pg.execute(_SEED_SQL, bucket_key, capacity, refill_per_sec)


class PgTokenBucket:
    """A lazily-refilled token bucket stored in one ``lml_cache`` row.

    Thin PG primitive: construct with the shared source and the bucket key,
    then ``await try_acquire()`` per permit. All retry/timeout/fallback policy
    lives in ``DiscogsRateGate`` — this class only does the atomic spend.
    """

    def __init__(self, pg: PgSource, *, bucket_key: str) -> None:
        self._pg = pg
        self._bucket_key = bucket_key

    async def try_acquire(self) -> TokenAcquisition:
        """Attempt to spend one token in a single atomic PG round-trip.

        Returns a ``TokenAcquisition``. Raises ``RateBucketMissingRowError`` if
        the row is absent, or propagates any asyncpg error — either way the gate
        catches it and fails open to the local limiter.
        """
        row = await self._pg.fetchone(_ACQUIRE_SQL, self._bucket_key)
        if row is None:
            raise RateBucketMissingRowError(self._bucket_key)
        return TokenAcquisition(
            allowed=bool(row["allowed"]),
            retry_after_s=float(row["retry_after_s"]),
        )
