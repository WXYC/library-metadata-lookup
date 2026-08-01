-- Shared Discogs rate token bucket schema for LML#841.
--
-- Canonical DDL reference for the single-row `lml_cache.discogs_rate_bucket`
-- table. The per-process `AsyncLimiter` bounds each LML process to
-- `DISCOGS_RATE_LIMIT`/min, but prod + staging share ONE Discogs token
-- against ONE upstream 60/min bucket -- this table holds one row per token so
-- every process draws rate permits from a single lazily-refilled token
-- bucket (`PgTokenBucket.try_acquire` in the `.py` spends it atomically via
-- one `UPDATE ... RETURNING` with a `FOR UPDATE` CTE).
--
-- It lives in the LML-owned `lml_cache.*` schema (per WXYC/discogs-etl#288,
-- Option 3) -- not the discogs-cache-owned `entity.*` identity contract,
-- which is created and migrated only via discogs-cache alembic migrations --
-- and is bootstrapped from LML's own FastAPI lifespan; discogs-cache tooling
-- never touches `lml_cache.*`.
--
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without booting the full LML app.
--
-- GENERATED FILE -- regenerate via:
--   uv run python -m scripts.regenerate_lml_cache_sql
-- Statements come verbatim from `entity/discogs_rate_bucket.py`; do not hand-edit
-- this file -- a per-module unit test (`tests/unit/test_discogs_rate_bucket_schema.py`)
-- pins the statement text. The runtime source of truth is
-- `entity/discogs_rate_bucket.py`'s `set_up_discogs_rate_bucket_schema`, which issues these statements
-- (`IF NOT EXISTS` forms) on every boot.

CREATE SCHEMA IF NOT EXISTS lml_cache;

-- `bucket_key` -- one row per shared Discogs token ('discogs' is the default
-- key every process sharing the station's single Discogs application uses).
-- `tokens` -- currently-available permits (fractional: refill accrues
-- continuously). `capacity` -- burst ceiling, seeded from
-- `DISCOGS_RATE_LIMIT`; `tokens` never exceeds it. `refill_per_sec` --
-- steady-state refill in permits/second (`DISCOGS_RATE_LIMIT` / 60).
-- `last_refill` -- wall-clock of the last refill+spend; elapsed time since
-- drives the lazy refill.

CREATE TABLE IF NOT EXISTS lml_cache.discogs_rate_bucket (
    bucket_key TEXT PRIMARY KEY,
    tokens DOUBLE PRECISION NOT NULL,
    capacity DOUBLE PRECISION NOT NULL,
    refill_per_sec DOUBLE PRECISION NOT NULL,
    last_refill TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent seed (issued by `set_up_discogs_rate_bucket_schema`, not shown
-- as a top-level statement above since it is bound from the live
-- `discogs_rate_limit` setting, not a static literal). `ON CONFLICT DO
-- NOTHING` means the FIRST process to boot wins the shared budget; a later
-- boot with a different `DISCOGS_RATE_LIMIT` (e.g. staging) must NOT
-- oscillate the row. Seeded full so a fresh bucket starts with a burst
-- allowance. The literals below illustrate the DEFAULT budget
-- (`DISCOGS_RATE_LIMIT=50` -> capacity 50, refill 50/60 ~= 0.8333/s) for a
-- copy-paste apply; an operator overriding that env var makes the app's own
-- seed -- not this illustration -- authoritative:
--
--   INSERT INTO lml_cache.discogs_rate_bucket
--       (bucket_key, tokens, capacity, refill_per_sec)
--   VALUES ('discogs', 50, 50, 0.8333333333333334)
--   ON CONFLICT (bucket_key) DO NOTHING;
