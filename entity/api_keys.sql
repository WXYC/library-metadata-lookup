-- Per-consumer LML API key schema (LML per-consumer API keys plan).
--
-- Canonical DDL reference for `lml_cache.api_keys`: one row per consumer
-- (tubafrenzy, backend-service, wxyc-canary, the operator drain script, ...),
-- each holding the SHA-256 hash of a distinct `lml_<random>` token.
-- `caller_name` carries deliberately no per-caller uniqueness constraint: a
-- rotation in progress means two live rows for the same caller (old
-- not-yet-revoked, new not-yet-confirmed), the intended state, not a bug.
-- `key_hash` is the only credential-bearing
-- column ever persisted; the plaintext token is shown exactly once, at mint
-- time, by `scripts/api_keys`.
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
-- Statements come verbatim from `entity/api_keys.py`; do not hand-edit
-- this file -- a per-module unit test (`tests/unit/test_api_keys_schema.py`)
-- pins the statement text. The runtime source of truth is
-- `entity/api_keys.py`'s `set_up_api_keys_schema`, which issues these statements
-- (`IF NOT EXISTS` forms) on every boot.

CREATE SCHEMA IF NOT EXISTS lml_cache;

CREATE TABLE IF NOT EXISTS lml_cache.api_keys (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    caller_name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    note TEXT
);
