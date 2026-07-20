-- Shared Discogs rate token bucket schema for LML#841.
--
-- This file is the canonical DDL reference for the single-row
-- `lml_cache.discogs_rate_bucket` table. Like `lml_cache.album_streaming_url_cache`
-- (and unlike the discogs-cache-owned `entity.*` identity contract, migrated via
-- alembic), this table lives in the LML-owned `lml_cache.*` schema (per
-- WXYC/discogs-etl#288, Option 3) and is bootstrapped from LML's own FastAPI
-- lifespan — no discogs-cache coordination. discogs-cache tooling never touches
-- `lml_cache.*`.
--
-- Purpose: the per-process `AsyncLimiter` bounds each LML process to
-- `DISCOGS_RATE_LIMIT`/min, but prod + staging share one Discogs token against
-- one upstream 60/min bucket. This table holds ONE row per Discogs token so every
-- process draws rate permits from a single lazily-refilled token bucket — exact
-- global enforcement. Enforcement is atomic in the single `UPDATE … RETURNING`
-- with a `FOR UPDATE` CTE (see `PgTokenBucket.try_acquire` in the `.py`).
--
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without booting the full LML app.
--
-- The runtime source of truth is `entity/discogs_rate_bucket.py`
-- (`set_up_discogs_rate_bucket_schema`), which issues these statements via the
-- `IF NOT EXISTS` / `ON CONFLICT DO NOTHING` forms on every boot. If the canonical
-- shape changes, update both places.

CREATE SCHEMA IF NOT EXISTS lml_cache;

CREATE TABLE IF NOT EXISTS lml_cache.discogs_rate_bucket (
    -- One row per shared Discogs token. `'discogs'` is the default key used by
    -- every process that shares the station's single Discogs application.
    bucket_key TEXT PRIMARY KEY,
    -- Currently-available permits (fractional: refill accrues continuously).
    tokens DOUBLE PRECISION NOT NULL,
    -- Burst ceiling; seeded from `DISCOGS_RATE_LIMIT`. `tokens` never exceeds it.
    capacity DOUBLE PRECISION NOT NULL,
    -- Steady-state refill in permits/second (`DISCOGS_RATE_LIMIT` / 60).
    refill_per_sec DOUBLE PRECISION NOT NULL,
    -- Wall-clock of the last refill+spend; elapsed time since drives lazy refill.
    last_refill TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent seed. `ON CONFLICT DO NOTHING` means the FIRST process to boot wins
-- the budget; a later boot with a different `DISCOGS_RATE_LIMIT` (e.g. staging)
-- must NOT oscillate the shared row. Seeded full so a fresh bucket starts with a
-- burst allowance rather than cold-starting empty.
INSERT INTO lml_cache.discogs_rate_bucket (bucket_key, tokens, capacity, refill_per_sec)
VALUES ('discogs', 50, 50, 0.8333333333333334)
ON CONFLICT (bucket_key) DO NOTHING;
