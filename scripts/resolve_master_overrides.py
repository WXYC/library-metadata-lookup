"""Resolve Alex L.'s verified Discogs *master* IDs to concrete *release* IDs (LML#858).

Phase 1 (#850) pinned the card-catalog entries whose verified Discogs link was
already a *release* ID. This Phase-2 pre-step converts the larger slice whose
link is a *master* ID — a master groups every version of an album and so yields
no single tracklist — into a concrete release ID, then emits a CSV that the
existing ``scripts/seed_library_release_overrides.py`` consumes unchanged.

The choice among a master's cached versions is tiered (see ``resolve_master``):

* **Tier A** (high) — one cached version, or several with identical tracklists.
* **Tier B** (high) — divergent tracklists, disambiguated by a unique
  strict-superset winner on flowsheet played-title coverage.
* **Tier C** (low) — divergent, no distinguishing signal: ``main_release``
  fallback (or a deterministic pick) under a low-confidence source tag.
* **no_cached** (low) — no cached release; pin ``main_release_id`` if known.
* **unresolved** — nothing to pin (no cached version, no ``main_release_id``);
  the API tail, reported but never silently dropped.

Only the pure decision core lives near the top and is import-light so the unit
suite needs no DB. PG querying, flowsheet loading, and CLI plumbing are added
below and import their dependencies lazily.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from wxyc_etl.text import to_match_form

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("resolve_master_overrides")

# Batch size for the ``master_id = ANY($1)`` candidate query. ~46K masters is
# fine in one array, but chunking bounds peak memory of the joined rows.
_QUERY_CHUNK = 5000

# Default source tags for the two confidence buckets. The seeder's
# ``ON CONFLICT ... WHERE source = EXCLUDED.source`` guard keeps each bucket its
# own separable set (and a re-pin pass MUST reuse the same tag or it no-ops).
SOURCE_HIGH = "alex-l-2026-masters"
SOURCE_LOW = "alex-l-2026-masters-lowconf"

# Tier + confidence labels. Kept as module constants so the seeder's source tag
# and the report can reference them without magic strings.
TIER_A = "A"
TIER_B = "B"
TIER_C = "C"
NO_CACHED = "no_cached"
UNRESOLVED = "unresolved"

CONF_HIGH = "high"
CONF_LOW = "low"


@dataclass(frozen=True)
class CandidateRelease:
    """One cached Discogs release under a master, with its normalized tracklist.

    ``normalized_titles`` is an order-insensitive set of ``to_match_form`` titles
    so two pressings with the same songs in a different order compare equal.
    ``format`` is the raw Discogs format string (e.g. ``"Vinyl, LP, Album"``),
    used only as a deterministic tiebreaker.
    """

    release_id: int
    normalized_titles: frozenset[str]
    format: str | None = None


@dataclass(frozen=True)
class MasterResolution:
    """The resolver's decision for one (card_catalog_id, master_id) pair."""

    card_catalog_id: int
    master_id: int
    chosen_release_id: int | None
    tier: str
    confidence: str
    reason: str


def normalize_titles(titles: Iterable[str]) -> frozenset[str]:
    """Normalize an iterable of track titles into an order-insensitive set.

    Uses ``wxyc_etl.text.to_match_form`` — the same normalizer the lookup path
    uses for comparison — and drops titles that normalize to empty.
    """
    out: set[str] = set()
    for t in titles:
        form = to_match_form(t or "")
        if form:
            out.add(form)
    return frozenset(out)


def _format_tokens(fmt: str | None) -> frozenset[str]:
    """Lowercased alphanumeric tokens of a Discogs format string."""
    if not fmt:
        return frozenset()
    token = ""
    tokens: set[str] = set()
    for ch in fmt.lower():
        if ch.isalnum():
            token += ch
        elif token:
            tokens.add(token)
            token = ""
    if token:
        tokens.add(token)
    return frozenset(tokens)


def _choose_cached(
    candidates: list[CandidateRelease],
    main_release_id: int | None,
    csv_format: str | None,
) -> int:
    """Deterministically pick one cached release.

    Preference order: the master's ``main_release`` when it is itself cached,
    then a release whose format matches the CSV's ``format`` column, then the
    lowest release ID (stable across reruns). Callers guarantee a non-empty
    ``candidates``.
    """
    ids = {c.release_id for c in candidates}
    if main_release_id is not None and main_release_id in ids:
        return main_release_id
    wanted = to_match_form(csv_format or "")
    if wanted:
        fmt_matches = [c.release_id for c in candidates if wanted in _format_tokens(c.format)]
        if fmt_matches:
            return min(fmt_matches)
    return min(ids)


def _flowsheet_winner(
    candidates: list[CandidateRelease],
    played_titles: frozenset[str],
) -> CandidateRelease | None:
    """Return the unique strict-superset winner on played-title coverage, else None.

    A candidate wins only if the set of played titles it covers contains every
    other candidate's covered set (dominance) *and* strictly exceeds at least
    one of them (a real margin, not an all-equal / all-zero tie). Crossing
    evidence — each version covering a played title the other lacks — yields no
    dominant candidate and returns None. This encodes the flowsheet's
    asymmetry: a play is positive evidence, its absence is not.
    """
    if not played_titles:
        return None
    covered = {c.release_id: played_titles & c.normalized_titles for c in candidates}
    dominant = [
        c
        for c in candidates
        if all(covered[c.release_id] >= covered[o.release_id] for o in candidates)
    ]
    if len(dominant) != 1:
        return None
    winner = dominant[0]
    if any(covered[winner.release_id] > covered[o.release_id] for o in candidates):
        return winner
    return None


def resolve_master(
    *,
    card_catalog_id: int,
    master_id: int,
    candidates: list[CandidateRelease],
    main_release_id: int | None,
    played_titles: frozenset[str],
    csv_format: str | None = None,
) -> MasterResolution:
    """Resolve one master to a concrete release ID via the tiered policy.

    ``candidates`` are the master's cached releases (via ``release.master_id``);
    ``main_release_id`` comes from the ``master`` table (may be ``None`` until it
    is populated — see WXYC/discogs-etl#317); ``played_titles`` is the set of
    normalized flowsheet titles attributed to this card. Never raises and never
    drops a pair — an unresolvable master returns ``tier=unresolved``.
    """

    def _mk(chosen: int | None, tier: str, conf: str, reason: str) -> MasterResolution:
        return MasterResolution(
            card_catalog_id=card_catalog_id,
            master_id=master_id,
            chosen_release_id=chosen,
            tier=tier,
            confidence=conf,
            reason=reason,
        )

    if not candidates:
        if main_release_id is not None:
            return _mk(
                main_release_id,
                NO_CACHED,
                CONF_LOW,
                "no cached version; pinned master main_release",
            )
        return _mk(
            None, UNRESOLVED, CONF_LOW, "no cached version and no main_release_id (API/master tail)"
        )

    distinct_tracklists = {c.normalized_titles for c in candidates}
    if len(distinct_tracklists) == 1:
        chosen = _choose_cached(candidates, main_release_id, csv_format)
        return _mk(
            chosen,
            TIER_A,
            CONF_HIGH,
            f"single tracklist across {len(candidates)} cached version(s)",
        )

    winner = _flowsheet_winner(candidates, played_titles)
    if winner is not None:
        n = len(played_titles & winner.normalized_titles)
        return _mk(
            winner.release_id,
            TIER_B,
            CONF_HIGH,
            f"flowsheet strict-superset winner (covers {n} played title(s))",
        )

    # Divergent tracklists, no distinguishing flowsheet signal: pin a *cached*
    # version so a tracklist is guaranteed — the master's main_release when it is
    # itself cached (canonical), else a format-matched / lowest-id cached edition.
    # An uncached main_release is deliberately NOT pinned here: it carries no
    # cached tracklist, and a format-matched cached version is the better
    # shelf-copy approximation. (An uncached main_release is pinned only in the
    # no_cached tier, where there is no cached alternative.)
    chosen = _choose_cached(candidates, main_release_id, csv_format)
    origin = "main_release (cached)" if chosen == main_release_id else "format/id fallback"
    return _mk(
        chosen,
        TIER_C,
        CONF_LOW,
        f"divergent tracklists, no flowsheet signal; {origin}",
    )


# ---------------------------------------------------------------------------
# Input parsing: master-linked card entries from the verified dataset
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MasterLink:
    """One card-catalog entry whose verified Discogs link is a *master* id."""

    card_catalog_id: int
    master_id: int
    csv_format: str | None


def load_master_links(path: Path) -> list[MasterLink]:
    """Parse the master-typed rows of ``merged_discogs_links.csv``.

    Keeps rows with ``discogs_type == 'master'`` and a positive integer
    ``card_catalog_id`` + ``discogs_id`` (the master id). Dedupes by
    ``card_catalog_id`` (last row wins, matching the seeder's contract). Opens
    with ``utf-8-sig`` so a BOM-prefixed Excel export still matches the header.
    """
    by_card: dict[int, MasterLink] = {}
    parsed = skipped = 0
    with path.open(newline="", encoding="utf-8-sig") as f:
        for record in csv.DictReader(f):
            if (record.get("discogs_type") or "").strip().lower() != "master":
                continue
            parsed += 1
            card = _parse_positive_int(record.get("card_catalog_id"))
            master = _parse_positive_int(record.get("discogs_id"))
            if card is None or master is None:
                skipped += 1
                continue
            fmt = (record.get("format") or "").strip() or None
            by_card[card] = MasterLink(card_catalog_id=card, master_id=master, csv_format=fmt)
    log.info(
        "loaded %d master-typed rows: %d valid links (%d distinct cards), %d skipped",
        parsed,
        parsed - skipped,
        len(by_card),
        skipped,
    )
    return list(by_card.values())


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


def load_flowsheet_titles(path: Path) -> dict[int, frozenset[str]]:
    """Load per-card played titles from ``flowsheet_titles_per_card.csv``.

    Expects columns ``card_catalog_id,title`` (raw titles, one per row) and
    normalizes them with the resolver's own ``to_match_form`` so Tier-B matching
    compares candidate tracklists and flowsheet plays on identical footing.
    Returns ``{card_catalog_id: frozenset(normalized titles)}``.
    """
    raw: dict[int, set[str]] = defaultdict(set)
    with path.open(newline="", encoding="utf-8-sig") as f:
        for record in csv.DictReader(f):
            card = _parse_positive_int(record.get("card_catalog_id"))
            if card is None:
                continue
            form = to_match_form(record.get("title") or "")
            if form:
                raw[card].add(form)
    out = {card: frozenset(titles) for card, titles in raw.items()}
    log.info("loaded flowsheet titles for %d cards", len(out))
    return out


# ---------------------------------------------------------------------------
# discogs-cache queries (read-only)
# ---------------------------------------------------------------------------


async def fetch_candidates(conn, master_ids: Iterable[int]) -> dict[int, list[CandidateRelease]]:
    """Fetch cached releases (with tracklists) grouped by master id.

    One row per (release, track) via ``release JOIN release_track``; a release
    with no tracks is absent (it can't be tracklist-compared anyway). Chunks the
    ``master_id = ANY($1)`` predicate to bound the joined result size.
    """
    ids = sorted({int(m) for m in master_ids})
    # release_id -> {master_id, format, titles: list[str]}
    rel: dict[int, dict] = {}
    for start in range(0, len(ids), _QUERY_CHUNK):
        chunk = ids[start : start + _QUERY_CHUNK]
        rows = await conn.fetch(
            """
            SELECT r.master_id AS master_id, r.id AS release_id,
                   r.format AS format, rt.title AS title
            FROM release r
            JOIN release_track rt ON rt.release_id = r.id
            WHERE r.master_id = ANY($1::int[])
            """,
            chunk,
        )
        for row in rows:
            entry = rel.setdefault(
                row["release_id"],
                {"master_id": row["master_id"], "format": row["format"], "titles": []},
            )
            if row["title"]:
                entry["titles"].append(row["title"])
    by_master: dict[int, list[CandidateRelease]] = defaultdict(list)
    for release_id, entry in rel.items():
        by_master[entry["master_id"]].append(
            CandidateRelease(
                release_id=release_id,
                normalized_titles=normalize_titles(entry["titles"]),
                format=entry["format"],
            )
        )
    return dict(by_master)


async def fetch_main_releases(conn, master_ids: Iterable[int]) -> dict[int, int]:
    """Fetch ``master.main_release_id`` for the given masters (skips NULLs).

    Returns ``{}`` while the ``master`` table is unpopulated (WXYC/discogs-etl#317
    prerequisite) — the resolver then falls back to cached-version selection.
    """
    ids = sorted({int(m) for m in master_ids})
    out: dict[int, int] = {}
    for start in range(0, len(ids), _QUERY_CHUNK):
        chunk = ids[start : start + _QUERY_CHUNK]
        rows = await conn.fetch(
            """
            SELECT id, main_release_id
            FROM master
            WHERE id = ANY($1::int[]) AND main_release_id IS NOT NULL
            """,
            chunk,
        )
        for row in rows:
            out[row["id"]] = row["main_release_id"]
    return out


# ---------------------------------------------------------------------------
# Driver + output
# ---------------------------------------------------------------------------


def resolve_all(
    links: list[MasterLink],
    candidates_by_master: dict[int, list[CandidateRelease]],
    main_release_by_master: dict[int, int],
    titles_by_card: dict[int, frozenset[str]],
) -> list[MasterResolution]:
    """Resolve every master link into a ``MasterResolution`` (pure, no I/O)."""
    resolutions = []
    for link in links:
        resolutions.append(
            resolve_master(
                card_catalog_id=link.card_catalog_id,
                master_id=link.master_id,
                candidates=candidates_by_master.get(link.master_id, []),
                main_release_id=main_release_by_master.get(link.master_id),
                played_titles=titles_by_card.get(link.card_catalog_id, frozenset()),
                csv_format=link.csv_format,
            )
        )
    return resolutions


def tier_report(resolutions: list[MasterResolution]) -> dict:
    """Per-tier + per-confidence counts, plus seedable/unresolved totals."""
    by_tier: dict[str, int] = defaultdict(int)
    by_conf: dict[str, int] = defaultdict(int)
    seedable = unresolved = 0
    for r in resolutions:
        by_tier[r.tier] += 1
        by_conf[r.confidence] += 1
        if r.chosen_release_id is None:
            unresolved += 1
        else:
            seedable += 1
    return {
        "total": len(resolutions),
        "by_tier": dict(sorted(by_tier.items())),
        "by_confidence": dict(sorted(by_conf.items())),
        "seedable": seedable,
        "unresolved": unresolved,
    }


_SEED_HEADER = [
    "card_catalog_id",
    "discogs_release_id",
    "master_id",
    "tier",
    "confidence",
    "reason",
]


def write_seed_csv(path: Path, resolutions: list[MasterResolution]) -> int:
    """Write resolvable rows (``chosen_release_id`` set) in the seeder's schema.

    The seeder reads ``card_catalog_id`` + ``discogs_release_id`` by name and
    ignores the trailing audit columns. Rows with no pin are skipped (they are
    still counted in the report). Returns the number of rows written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_SEED_HEADER)
        for r in resolutions:
            if r.chosen_release_id is None:
                continue
            w.writerow(
                [
                    r.card_catalog_id,
                    r.chosen_release_id,
                    r.master_id,
                    r.tier,
                    r.confidence,
                    r.reason,
                ]
            )
            written += 1
    return written


def _split_by_confidence(
    resolutions: list[MasterResolution],
) -> tuple[list[MasterResolution], list[MasterResolution]]:
    high = [r for r in resolutions if r.confidence == CONF_HIGH]
    low = [r for r in resolutions if r.confidence == CONF_LOW]
    return high, low


async def _run(args: argparse.Namespace) -> None:
    links = load_master_links(Path(args.input))
    if not links:
        log.warning("no master links loaded from %s — nothing to do", args.input)
        return
    titles_by_card = load_flowsheet_titles(Path(args.flowsheet)) if args.flowsheet else {}

    from config.settings import get_settings
    from entity.sources import PgSource

    db_url = get_settings().database_url_discogs
    if not db_url:
        raise SystemExit("DATABASE_URL_DISCOGS is not configured — cannot query the cache")

    master_ids = {link.master_id for link in links}
    pg = PgSource(dsn=db_url)
    try:
        async with pg.acquire() as conn:
            log.info("querying %d distinct masters for cached versions", len(master_ids))
            candidates = await fetch_candidates(conn, master_ids)
            main_releases = await fetch_main_releases(conn, master_ids)
        log.info(
            "cache: %d masters with >=1 cached version; %d masters with a main_release_id",
            len(candidates),
            len(main_releases),
        )
    finally:
        await pg.close()

    resolutions = resolve_all(links, candidates, main_releases, titles_by_card)
    report = tier_report(resolutions)
    log.info("resolution report: %s", json.dumps(report))

    high, low = _split_by_confidence(resolutions)
    n_high = write_seed_csv(Path(args.out_high), high)
    n_low = write_seed_csv(Path(args.out_low), low)
    log.info(
        "wrote %d high-confidence pins -> %s (seed as --source %s)",
        n_high,
        args.out_high,
        SOURCE_HIGH,
    )
    log.info(
        "wrote %d low-confidence pins -> %s (seed as --source %s)",
        n_low,
        args.out_low,
        SOURCE_LOW,
    )
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2))
        log.info("wrote report -> %s", args.report)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        required=True,
        help="Path to merged_discogs_links.csv (master-typed rows are selected)",
    )
    parser.add_argument(
        "--flowsheet",
        help="Path to flowsheet_titles_per_card.csv (card,title) for Tier-B scoring",
    )
    parser.add_argument(
        "--out-high",
        default="phase2_master_links_highconf.csv",
        help="Output CSV for Tier A/B pins (default: %(default)s)",
    )
    parser.add_argument(
        "--out-low",
        default="phase2_master_links_lowconf.csv",
        help="Output CSV for Tier C / no_cached pins (default: %(default)s)",
    )
    parser.add_argument(
        "--report",
        help="Optional path to write the per-tier JSON report (also logged)",
    )
    return parser


if __name__ == "__main__":
    asyncio.run(_run(_build_arg_parser().parse_args()))
