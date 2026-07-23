-- Per-consumer LML API key schema (LML per-consumer API keys plan).
--
-- Canonical DDL reference for the `lml_cache.api_keys` table. Like the other
-- `lml_cache.*` tables, it lives in the LML-owned application-cache schema
-- (per WXYC/discogs-etl#288, Option 3) and is bootstrapped from LML's own
-- FastAPI lifespan -- no discogs-cache coordination, no alembic.
--
-- One row per consumer credential (tubafrenzy, backend-service, wxyc-canary,
-- the operator drain script, ...), each holding the SHA-256 hash of a
-- distinct `lml_<random>` bearer token minted by `scripts/api_keys`. Deliberately
-- NO uniqueness constraint on caller_name: a rotation in progress means two
-- live rows for the same caller (old not-yet-revoked, new not-yet-confirmed)
-- -- that is the intended state, not a bug.
--
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without booting the full LML app.
--
-- The runtime source of truth is `entity/api_keys.py` (`set_up_api_keys_schema`),
-- which issues these statements via the `IF NOT EXISTS` forms on every boot.
-- If the canonical shape changes, update both places.

CREATE SCHEMA IF NOT EXISTS lml_cache;

CREATE TABLE IF NOT EXISTS lml_cache.api_keys (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    caller_name TEXT NOT NULL,
    -- SHA-256 hex digest of the plaintext token. The plaintext is never
    -- persisted anywhere; it is shown exactly once, at mint time, by
    -- `scripts/api_keys seed`.
    key_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Touched (throttled to roughly hourly) by core/api_key_cache.py's
    -- fire-and-forget write on every successful request-path hit. NULL means
    -- never used -- useful for "can we delete this key yet."
    last_used_at TIMESTAMPTZ,
    -- NULL = active. A revoked row is excluded from the active-key SELECT
    -- (entity/api_keys.py's `_SELECT_ACTIVE_SQL`), so a revoked key and a key
    -- that never existed are indistinguishable to the request path.
    revoked_at TIMESTAMPTZ,
    note TEXT
);
