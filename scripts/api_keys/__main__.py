"""CLI entrypoint for lml_cache.api_keys seed / revoke / list (LML per-consumer API keys plan).

Packaged as ``scripts/api_keys/__main__.py`` -- the same ``scripts/<name>/__main__.py``
single-purpose-module shape as ``scripts/artist_resolve_drain``. The PG-writing
mechanic itself follows this codebase's real precedent for a script that writes
directly to an ``lml_cache.*`` table, ``scripts/seed_library_release_overrides.py``:
a ``PgSource(dsn=...)`` built off ``DATABASE_URL_DISCOGS``, a schema bootstrap
before any write (idempotent ``IF NOT EXISTS`` -- a no-op against an
already-migrated prod), and a ``try/finally`` that closes the pool.

Usage::

    # mint a new key for a consumer -- prints the plaintext EXACTLY ONCE
    uv run python -m scripts.api_keys seed --caller wxyc-canary --note "initial rollout"

    # revoke by id (idempotent -- a second revoke of the same id is a no-op)
    uv run python -m scripts.api_keys revoke --id 4

    # list operational metadata (never key_hash or plaintext)
    uv run python -m scripts.api_keys list

Safety: the plaintext token is generated here and printed to stdout exactly
once, at mint time, with a "will not be shown again" warning -- it is never
logged and never written anywhere but stdout. Only its SHA-256 hash
(``core.api_key_cache.hash_token``) reaches ``lml_cache.api_keys``. Hand the
plaintext to the consumer's own secret store immediately.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from config.settings import get_settings
from core.api_key_cache import generate_token, hash_token
from entity.api_keys import (
    insert_api_key,
    list_api_keys,
    revoke_api_key,
    set_up_api_keys_schema,
)
from entity.sources import PgSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("api_keys")


def _require_pg() -> PgSource:
    """Build a ``PgSource`` off ``DATABASE_URL_DISCOGS``, or exit loudly if unset."""
    db_url = get_settings().database_url_discogs
    if not db_url:
        raise SystemExit(
            "DATABASE_URL_DISCOGS is not configured -- cannot reach lml_cache.api_keys"
        )
    return PgSource(dsn=db_url)


async def seed(*, caller_name: str, note: str | None) -> None:
    """Mint a fresh token for ``caller_name``, insert its hash, print it once.

    The plaintext is generated and printed here and is NEVER logged -- once
    this prints, the only durable record is the SHA-256 hash in
    ``lml_cache.api_keys``.
    """
    token = generate_token()
    pg = _require_pg()
    try:
        await set_up_api_keys_schema(pg)
        row = await insert_api_key(
            pg, caller_name=caller_name, key_hash=hash_token(token), note=note
        )
    finally:
        await pg.close()
    log.info(
        "seeded key id=%s for caller=%s (created_at=%s)", row["id"], caller_name, row["created_at"]
    )
    print(f"\nToken for {caller_name} (id={row['id']}) -- shown once, will not be shown again:\n")
    print(f"  {token}\n")


async def revoke(*, key_id: int) -> None:
    """Revoke the row with primary key ``key_id``.

    Idempotent: revoking an already-revoked or nonexistent id logs a WARNING
    (not an error) and returns -- matching ``entity.api_keys.revoke_api_key``'s
    no-op-on-miss contract.
    """
    pg = _require_pg()
    try:
        await set_up_api_keys_schema(pg)
        caller_name = await revoke_api_key(pg, key_id=key_id)
    finally:
        await pg.close()
    if caller_name is None:
        log.warning("no active key with id=%d (already revoked, or never existed)", key_id)
    else:
        log.info("revoked key id=%d for caller=%s", key_id, caller_name)


async def list_keys() -> None:
    """Print every row's operational metadata. Never key_hash or plaintext."""
    pg = _require_pg()
    try:
        await set_up_api_keys_schema(pg)
        rows = await list_api_keys(pg)
    finally:
        await pg.close()
    if not rows:
        print("no keys seeded")
        return
    print(f"{'id':>4}  {'caller_name':<24}  {'created_at':<26}  {'last_used_at':<26}  revoked_at")
    for row in rows:
        print(
            f"{row['id']:>4}  {row['caller_name']:<24}  {str(row['created_at']):<26}  "
            f"{str(row['last_used_at']):<26}  {row['revoked_at'] or ''}"
        )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.api_keys", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    seed_parser = sub.add_parser("seed", help="Mint a new per-consumer key.")
    seed_parser.add_argument("--caller", required=True, help="Consumer name, e.g. wxyc-canary.")
    seed_parser.add_argument("--note", default=None, help="Free-text note (rotation context, etc).")

    revoke_parser = sub.add_parser("revoke", help="Revoke a key by id.")
    revoke_parser.add_argument("--id", type=int, required=True, dest="key_id")

    sub.add_parser("list", help="List operational metadata for every key.")

    return parser


async def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "seed":
        await seed(caller_name=args.caller, note=args.note)
    elif args.command == "revoke":
        await revoke(key_id=args.key_id)
    elif args.command == "list":
        await list_keys()
    else:  # pragma: no cover -- argparse required=True on the subparsers makes this unreachable
        raise SystemExit(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    asyncio.run(_dispatch(args))


if __name__ == "__main__":
    main()
