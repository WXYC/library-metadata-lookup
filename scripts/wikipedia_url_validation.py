"""LML#513 empirical gate — Wikipedia URL extractor validation.

Samples artists from the discogs-cache ``artist``/``artist_url`` tables that
carry at least one ``wikipedia.org`` URL, runs both the legacy first-match
heuristic and the new slug-scored extractor
(``lookup.wikipedia_url.compare_wikipedia_extractors``) over each, and writes
a CSV for hand classification. A second pass (``--report``) reads a
hand-classified CSV back and computes the agreement / regression / improvement
rates the plan's Phase-A gate requires: the regression rate (heuristic right,
slug wrong) must clear < 2% before ``LML_WIKIPEDIA_SLUG_MATCH`` flips.

Two-phase design, mirroring ``scripts/resolver_calibration``'s
sweep-then-hand-review split:

1. Sample phase (default): queries the discogs-cache, writes
   ``--out`` with blank ``heuristic_correct``/``slug_correct`` columns.
2. Report phase (``--report PATH``): reads a filled-in CSV (a human has
   opened each URL and marked TRUE/FALSE in both columns) and prints the
   summary — no DB connection needed.

Read-only. Never writes to PG.

Usage::

    DATABASE_URL_DISCOGS=postgresql://... uv run python -m scripts.wikipedia_url_validation \\
        --sample-size 300 --seed 513 --out lml-513-sample.csv

    # after hand-classifying lml-513-sample.csv's heuristic_correct/slug_correct columns:
    uv run python -m scripts.wikipedia_url_validation --report lml-513-sample.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lookup.wikipedia_url import compare_wikipedia_extractors

if TYPE_CHECKING:
    import psycopg

# Candidate query: distinct artist ids carrying at least one wikipedia.org
# artist_url row. Bounded to a generous cap so the candidate fetch itself
# stays cheap even against a large cache; the actual sample is drawn
# in Python (sample_artist_ids) so it is reproducible via --seed.
_CANDIDATE_IDS_SQL = """
    SELECT DISTINCT artist_id
    FROM artist_url
    WHERE url ILIKE %s
    LIMIT 50000
"""

_ARTIST_SAMPLE_SQL = """
    SELECT au.artist_id, a.name, au.url
    FROM artist_url au
    JOIN artist a ON a.id = au.artist_id
    WHERE au.artist_id = ANY(%s)
    ORDER BY au.artist_id, au.url
"""

_CSV_FIELDS = [
    "artist_id",
    "artist_name",
    "heuristic_pick",
    "slug_pick",
    "slug_score",
    "slug_lang",
    "agreement",
    "clears_floor",
    "heuristic_correct",
    "slug_correct",
]


@dataclass(frozen=True)
class ArtistSample:
    """One artist's id, name, and every ``artist_url`` row (not just the
    wikipedia.org ones — ``compare_wikipedia_extractors`` needs the full
    list, same as the runtime ``ArtistDetails.urls`` it consumes)."""

    artist_id: int
    artist_name: str
    urls: list[str]


def fetch_candidate_artist_ids(conn: psycopg.Connection) -> list[int]:
    """Distinct artist ids with at least one ``wikipedia.org`` URL."""
    with conn.cursor() as cur:
        cur.execute(_CANDIDATE_IDS_SQL, ("%wikipedia.org%",))
        return [row[0] for row in cur.fetchall()]


def sample_artist_ids(candidate_ids: list[int], sample_size: int, seed: int | None) -> list[int]:
    """Deterministic (seeded) sample of ``sample_size`` ids from ``candidate_ids``.

    Returns the full population unchanged when it is already at or under
    ``sample_size`` — matches ``resolver_calibration``'s "don't oversample a
    small population" posture.
    """
    if len(candidate_ids) <= sample_size:
        return list(candidate_ids)
    return random.Random(seed).sample(candidate_ids, sample_size)


def fetch_artist_samples(conn: psycopg.Connection, artist_ids: list[int]) -> list[ArtistSample]:
    """Batch-fetch ``(name, all urls)`` for ``artist_ids``, grouped per artist."""
    if not artist_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(_ARTIST_SAMPLE_SQL, (artist_ids,))
        rows = cur.fetchall()
    by_artist: dict[int, ArtistSample] = {}
    for artist_id, name, url in rows:
        existing = by_artist.get(artist_id)
        if existing is None:
            by_artist[artist_id] = ArtistSample(artist_id=artist_id, artist_name=name, urls=[url])
        else:
            existing.urls.append(url)
    return list(by_artist.values())


def build_comparison_rows(samples: list[ArtistSample]) -> list[dict[str, Any]]:
    """Run both extractors over every sample and shape one CSV row each."""
    rows: list[dict[str, Any]] = []
    for sample in samples:
        comparison = compare_wikipedia_extractors(sample.urls, sample.artist_name)
        rows.append(
            {
                "artist_id": sample.artist_id,
                "artist_name": sample.artist_name,
                "heuristic_pick": comparison.heuristic_pick,
                "slug_pick": comparison.slug_pick,
                "slug_score": round(comparison.slug_score, 2),
                "slug_lang": comparison.slug_lang,
                # LML#1192 review round 6, pass 3, A4: read the single-owner
                # properties (round 3, finding 9) rather than hand-rewriting
                # the same predicates here -- this script computes the
                # <2%-divergence gate that authorizes the prod
                # LML_WIKIPEDIA_SLUG_MATCH flip, so it must measure exactly
                # what the shipped code means by "clears the floor" /
                # "agrees," not a separately-maintained copy that could
                # silently drift from it.
                "agreement": comparison.agreement,
                "clears_floor": comparison.clears_floor,
                # Blank: filled in by hand during ground-truth classification.
                "heuristic_correct": "",
                "slug_correct": "",
            }
        )
    return rows


def emit_csv(rows: list[dict[str, Any]], out: Any) -> None:
    writer = csv.DictWriter(out, fieldnames=_CSV_FIELDS)
    writer.writeheader()
    writer.writerows(rows)


def print_sample_summary(rows: list[dict[str, Any]]) -> None:
    total = len(rows)
    if not total:
        print("(no candidate artists found)")
        return
    agree = sum(1 for r in rows if r["agreement"])
    clears = sum(1 for r in rows if r["clears_floor"])

    def pct(n: int) -> str:
        return f"({n / total:.1%})"

    print(f"sampled artists:                {total:>5}")
    print(f"  heuristic == slug pick:       {agree:>5} {pct(agree)}")
    print(f"  slug pick clears the floor:   {clears:>5} {pct(clears)}")


@dataclass(frozen=True)
class GroundTruthSummary:
    """Rates computed from a hand-classified sample CSV."""

    classified: int
    regressions: int
    improvements: int

    @property
    def regression_rate(self) -> float:
        return self.regressions / self.classified if self.classified else 0.0

    @property
    def improvement_rate(self) -> float:
        return self.improvements / self.classified if self.classified else 0.0


def _is_true(value: str) -> bool:
    return value.strip().upper() in ("TRUE", "1", "YES")


def compute_ground_truth_summary(rows: list[dict[str, Any]]) -> GroundTruthSummary:
    """LML#513's gate: regression = heuristic right, slug wrong; improvement
    = heuristic wrong, slug right. Rows with a blank ``heuristic_correct`` or
    ``slug_correct`` are excluded from the denominator (not yet classified).

    LML#1192 review, A6: a row whose ``clears_floor`` is False is ALSO
    excluded, regardless of classification -- a below-floor slug pick is
    never actually served once ``LML_WIKIPEDIA_SLUG_MATCH`` flips (the
    heuristic wins the served ``url`` either way; see
    ``lookup.wikipedia_url.pick_artist_wikipedia_url``), so marking it
    "wrong" is not a real regression, and counting it dilutes the
    denominator with a row the flip provably can't affect. A missing or
    unparseable ``clears_floor`` value fails safe to excluded, not included.
    """
    classified = 0
    regressions = 0
    improvements = 0
    for row in rows:
        heuristic_raw = row.get("heuristic_correct", "")
        slug_raw = row.get("slug_correct", "")
        if not heuristic_raw.strip() or not slug_raw.strip():
            continue
        if not _is_true(row.get("clears_floor", "")):
            continue
        classified += 1
        heuristic_correct = _is_true(heuristic_raw)
        slug_correct = _is_true(slug_raw)
        if heuristic_correct and not slug_correct:
            regressions += 1
        elif not heuristic_correct and slug_correct:
            improvements += 1
    return GroundTruthSummary(
        classified=classified, regressions=regressions, improvements=improvements
    )


def print_ground_truth_summary(rows: list[dict[str, Any]]) -> None:
    summary = compute_ground_truth_summary(rows)
    if not summary.classified:
        print(
            "(no rows classified — fill in heuristic_correct/slug_correct "
            "with TRUE/FALSE for each row first)"
        )
        return
    print(f"hand-classified rows:           {summary.classified:>5}")
    print(
        f"  regressions (heuristic right, slug wrong):  {summary.regressions:>5} "
        f"({summary.regression_rate:.1%})"
    )
    print(
        f"  improvements (heuristic wrong, slug right): {summary.improvements:>5} "
        f"({summary.improvement_rate:.1%})"
    )
    gate = "PASS" if summary.regression_rate < 0.02 else "FAIL"
    print(f"\nLML#513 gate (regression rate < 2%): {gate}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=300)
    parser.add_argument("--seed", type=int, default=513)
    parser.add_argument("--out", type=Path, default=Path("wikipedia_url_validation_sample.csv"))
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Compute the regression/improvement rates from an already "
        "hand-classified CSV instead of sampling (no DB connection).",
    )
    args = parser.parse_args(argv)

    if args.report is not None:
        with args.report.open(newline="") as f:
            rows = list(csv.DictReader(f))
        print_ground_truth_summary(rows)
        return 0

    dsn = os.environ.get("DATABASE_URL_DISCOGS")
    if not dsn:
        print("error: DATABASE_URL_DISCOGS env var required", file=sys.stderr)
        return 1

    import psycopg

    with psycopg.connect(dsn) as conn:
        candidate_ids = fetch_candidate_artist_ids(conn)
        sampled_ids = sample_artist_ids(candidate_ids, args.sample_size, args.seed)
        samples = fetch_artist_samples(conn, sampled_ids)

    rows = build_comparison_rows(samples)
    with args.out.open("w", newline="") as f:
        emit_csv(rows, f)

    print_sample_summary(rows)
    print(f"\nWrote {len(rows)} rows to {args.out}")
    print(
        "Hand-classify heuristic_correct / slug_correct (TRUE/FALSE) for each "
        "row, then re-run with --report to compute the regression rate."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
