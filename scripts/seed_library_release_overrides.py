"""Seed ``lml_cache.library_release_override`` from a verified CSV (LML#850).

WXYC DJ Alex L. hand-verified the correct Discogs release for each card
catalog entry. This seeder loads the Phase-1 slice (the rows that are Discogs
*release* ids) into the LML-owned override table, so a library-album lookup
returns Alex's pinned release — and its correct tracklist — instead of the
per-request fuzzy pick.

The verified CSV lives in the workspace meta-repo
(``plans/alex-discogs-import/data/phase1_release_links.csv``) and is **not**
committed to this repo — pass its path via ``--input``. Its columns are
``card_catalog_id, discogs_release_id, artist, title, wxyc_genre, format,
source_file``; ``card_catalog_id`` == tubafrenzy ``LIBRARY_RELEASE.ID`` ==
``LibraryItem.id`` (the exact key of the override table).

Safety:
  * **Dry-run by default** — parses + validates the CSV and reports, and makes
    **no database connection at all**. Pass ``--execute`` to bootstrap the
    schema (idempotent ``IF NOT EXISTS``) and perform the upsert.
  * **Reversible** — every row is stamped ``source`` (default ``alex-l-2026``);
    rollback is ``DELETE FROM lml_cache.library_release_override WHERE source =
    '<source>'``.
  * **Idempotent + non-clobbering** — ``ON CONFLICT (library_id) DO UPDATE``
    guarded by ``source = EXCLUDED.source``: a re-run refreshes only rows this
    seeder owns and never overwrites a pin later hand-corrected under a
    different ``source``.
  * Targets the shared discogs-cache PG via ``DATABASE_URL_DISCOGS``. **Prod
    writes require explicit authorization and run staging-first.**

Scope: this seeds the override table only. Minting ``entity.release_identity``
for the pinned releases and warming the release cache are separate, optional
operational steps handled by the existing HTTP surfaces
(``POST /api/v1/identity/resolve`` and ``POST /api/v1/cache/refresh-for-identities``)
against a running service — they are intentionally not bolted onto this
table-writer.

Usage:
    # dry-run (default; no DB connection)
    uv run python -m scripts.seed_library_release_overrides \\
        --input ../plans/alex-discogs-import/data/phase1_release_links.csv

    # perform the upsert (staging first)
    uv run python -m scripts.seed_library_release_overrides \\
        --input <path> --execute
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from dataclasses import dataclass
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seed_library_release_overrides")

DEFAULT_SOURCE = "alex-l-2026"

_LIBRARY_ID_COL = "card_catalog_id"
_RELEASE_ID_COL = "discogs_release_id"

# Data safety: the conflict UPDATE is guarded by ``source = EXCLUDED.source`` so a
# re-run only touches rows THIS seeder owns. A pin later hand-corrected under a
# different ``source`` (e.g. a Music-Director fix) is left untouched — a
# re-applied CSV can never silently clobber a newer correction, and the scoped
# rollback DELETE stays truthful. (Per-CLAUDE.md "never overwrite successfully
# collected data".)
_UPSERT_SQL = """\
INSERT INTO lml_cache.library_release_override
    (library_id, discogs_release_id, source, updated_at)
VALUES ($1, $2, $3, now())
ON CONFLICT (library_id) DO UPDATE
SET discogs_release_id = EXCLUDED.discogs_release_id,
    updated_at = now()
WHERE library_release_override.source = EXCLUDED.source\
"""


@dataclass(frozen=True)
class OverrideRow:
    """One verified pin: a WXYC library release id -> a Discogs release id."""

    library_id: int
    discogs_release_id: int


def load_rows(path: Path) -> list[OverrideRow]:
    """Parse the verified CSV into valid, deduped ``OverrideRow``s.

    Drops rows whose ``card_catalog_id`` / ``discogs_release_id`` is missing,
    non-integer, or non-positive (a ``discogs_release_id <= 0`` can never be a
    real release — it maps to the ``release_id=0`` sentinel and the DB CHECK
    would reject it anyway). Dedupes by ``library_id``, **last row wins** (a
    later hand-correction supersedes an earlier automated entry). Opens with
    ``utf-8-sig`` so an Excel "CSV UTF-8" (BOM-prefixed) export still matches
    the first header. Logs the counts so a run is auditable.
    """
    parsed = 0
    skipped = 0
    by_library_id: dict[int, int] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for record in reader:
            parsed += 1
            library_id = _parse_positive_int(record.get(_LIBRARY_ID_COL))
            release_id = _parse_positive_int(record.get(_RELEASE_ID_COL))
            if library_id is None or release_id is None:
                skipped += 1
                continue
            by_library_id[library_id] = release_id  # last wins
    log.info(
        "loaded %d CSV rows: %d valid pins (%d distinct library ids), %d skipped",
        parsed,
        parsed - skipped,
        len(by_library_id),
        skipped,
    )
    return [OverrideRow(library_id=k, discogs_release_id=v) for k, v in by_library_id.items()]


def _parse_positive_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


async def seed_overrides(conn, rows: list[OverrideRow], *, execute: bool, source: str) -> int:
    """Upsert the override rows. Dry-run (``execute=False``) writes nothing.

    Returns the number of rows sent to the upsert (0 on a dry-run or an empty
    set). Uses a single ``executemany`` with the source-guarded ``ON CONFLICT
    (library_id) DO UPDATE`` so a re-run is idempotent and non-clobbering.
    """
    if not rows:
        log.info("no rows to seed")
        return 0
    if not execute:
        log.info("[dry-run] would upsert %d override rows (source=%s)", len(rows), source)
        return 0
    params = [(r.library_id, r.discogs_release_id, source) for r in rows]
    await conn.executemany(_UPSERT_SQL, params)
    log.info("upserted %d override rows (source=%s)", len(rows), source)
    return len(rows)


async def main(args: argparse.Namespace) -> None:
    rows = load_rows(Path(args.input))
    if not rows:
        log.warning("no valid rows loaded from %s — nothing to do", args.input)
        return

    if not args.execute:
        # Truly write-nothing: a dry-run makes no DB connection, so it can't
        # create the schema/table either. It validates the CSV and reports.
        log.info(
            "[dry-run] %d valid pins would be upserted (source=%s). Pass --execute "
            "to bootstrap the schema and write. No database connection was made.",
            len(rows),
            args.source,
        )
        return

    from config.settings import get_settings
    from entity.library_release_override import set_up_library_release_override_schema
    from entity.sources import PgSource

    db_url = get_settings().database_url_discogs
    if not db_url:
        raise SystemExit("DATABASE_URL_DISCOGS is not configured — cannot seed overrides")

    pg = PgSource(dsn=db_url)
    try:
        # Bootstrap the schema (idempotent IF NOT EXISTS; a no-op on an already-
        # migrated prod) so the seeder works against a fresh staging PG too.
        await set_up_library_release_override_schema(pg)
        # A bare pooled connection for the multi-row ``executemany`` upsert.
        async with pg.acquire() as conn:
            written = await seed_overrides(conn, rows, execute=True, source=args.source)
        log.info("seed complete: %d rows written (source=%s)", written, args.source)
    finally:
        await pg.close()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        required=True,
        help="Path to the verified CSV (plans/alex-discogs-import/data/phase1_release_links.csv)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the upsert (default: dry-run, makes no DB connection)",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help=f"Provenance stamp for rollback (default: {DEFAULT_SOURCE})",
    )
    return parser


if __name__ == "__main__":
    asyncio.run(main(_build_arg_parser().parse_args()))
