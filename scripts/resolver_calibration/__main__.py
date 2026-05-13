"""Calibration harness for the canonical-artist resolver pre-pass.

See WXYC/library-metadata-lookup#318.

Sweeps the trigram-similarity floor used by ``resolve_canonical_artist`` to
decide whether to swap an inbound artist name for its Discogs-canonical form.
The chosen floor is then committed to ``CANONICAL_ARTIST_SIMILARITY_FLOOR``
in ``lookup/orchestrator.py``.

Output files in ``--output-dir``:

* ``calibration_sweep.csv`` — for every candidate threshold in ``[0.30, 0.95]``
  step 0.05, the true-positive rate, false-positive rate, and the number of
  swaps the resolver would emit at that threshold.
* ``borderline.csv`` — every positive / negative pair whose score falls inside
  ``[chosen_floor - 0.05, chosen_floor + 0.05]``, for eyeball QA of the
  decision boundary.

The harness pulls ground truth from three sources:

1. **Discogs ``artist_name_variation``** — each row is a (variation,
   canonical_artist_id) pair. Positive class: the resolver should return
   ``canonical_artist_id`` as its top hit.
2. **``entity.identity``** — resolved rows for WXYC-specific name shapes that
   don't make it back to Discogs's variation table.
3. **Sampled close-but-distinct artists** — pairs from the ``artist`` table
   where ``name_a`` and ``name_b`` share a trigram-similarity ≥ 0.4 but have
   different ids. These are the cases where the resolver might wrongly swap.
   The score distribution here defines the false-positive curve.

Usage::

    DATABASE_URL_DISCOGS=postgresql://... \
        uv run python -m scripts.resolver_calibration \
            --output-dir docs/resolver-calibration/ \
            --negative-sample-size 5000 \
            --positive-sample-size 5000

By default the chosen floor is reported as the smallest threshold whose
empirical false-positive rate is ≤ the ``--fp-rate-target`` (default 0.005);
override with ``--fixed-floor`` to skip the optimization and just emit the
borderline file for that floor.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from lookup.orchestrator import CANONICAL_ARTIST_SIMILARITY_FLOOR

logger = logging.getLogger("resolver_calibration")

THRESHOLDS = [round(0.30 + 0.05 * i, 2) for i in range(14)]  # 0.30 .. 0.95


@dataclass
class CandidateScore:
    """A single calibration sample: query name, top-result name, score, label."""

    query_name: str
    top_name: str
    top_id: int | None
    score: float
    is_positive: bool


async def fetch_positive_class_variations(pool: asyncpg.Pool, limit: int) -> list[CandidateScore]:
    """Pull (variation_name, canonical_artist_id) pairs and run them through
    the same fuzzy trigram match the resolver uses.

    A pair is a true positive when the top match's ``artist.id`` equals the
    pair's canonical id. We record the actual score either way so the
    threshold sweep sees the full distribution.
    """
    query = """
        WITH sample AS (
            SELECT anv.name AS variation, a.id AS canonical_id, a.name AS canonical_name
            FROM artist_name_variation anv
            JOIN artist a ON a.id = anv.artist_id
            WHERE anv.name IS NOT NULL AND length(anv.name) >= 3
            ORDER BY random()
            LIMIT $1
        )
        SELECT
            s.variation,
            s.canonical_id,
            s.canonical_name,
            top.id AS top_id,
            top.name AS top_name,
            top.score AS top_score
        FROM sample s
        LEFT JOIN LATERAL (
            SELECT a2.id, a2.name,
                   similarity(lower(f_unaccent(a2.name)), lower(f_unaccent(s.variation))) AS score
            FROM artist a2
            WHERE lower(f_unaccent(a2.name)) % lower(f_unaccent(s.variation))
            ORDER BY score DESC
            LIMIT 1
        ) top ON true
    """
    rows = await pool.fetch(query, limit)
    out: list[CandidateScore] = []
    for r in rows:
        top_id = r["top_id"]
        top_name = r["top_name"] or r["variation"]
        score = float(r["top_score"] or 0.0)
        is_positive = top_id == r["canonical_id"]
        out.append(
            CandidateScore(
                query_name=r["variation"],
                top_name=top_name,
                top_id=top_id,
                score=score,
                is_positive=is_positive,
            )
        )
    return out


async def fetch_negative_class_close_pairs(pool: asyncpg.Pool, limit: int) -> list[CandidateScore]:
    """Sample close-but-distinct ``artist`` pairs.

    These are the pairs the resolver might mistakenly swap. The score column
    is the trigram similarity between the two distinct artists' names — the
    false-positive curve at a given threshold is the share of these whose
    score exceeds the threshold.
    """
    query = """
        WITH sample AS (
            SELECT id, name FROM artist
            WHERE name IS NOT NULL AND length(name) >= 3
            ORDER BY random()
            LIMIT $1
        )
        SELECT
            s.id AS id_a,
            s.name AS name_a,
            a.id AS id_b,
            a.name AS name_b,
            similarity(lower(f_unaccent(s.name)), lower(f_unaccent(a.name))) AS score
        FROM sample s
        JOIN artist a
          ON a.id != s.id
         AND lower(f_unaccent(a.name)) % lower(f_unaccent(s.name))
         AND similarity(lower(f_unaccent(s.name)), lower(f_unaccent(a.name))) >= 0.4
    """
    rows = await pool.fetch(query, limit)
    return [
        CandidateScore(
            query_name=r["name_a"],
            top_name=r["name_b"],
            top_id=r["id_b"],
            score=float(r["score"]),
            is_positive=False,
        )
        for r in rows
    ]


async def fetch_entity_identity_positives(pool: asyncpg.Pool, limit: int) -> list[CandidateScore]:
    """Second positive class: resolved rows from ``entity.identity`` whose
    ``library_name`` differs from the canonical Discogs ``artist.name``.

    Only rows with a known ``discogs_artist_id`` are usable as ground truth.
    Silently empty when the entity schema isn't present.
    """
    try:
        await pool.fetchval("SELECT 1 FROM entity.identity LIMIT 1")
    except Exception as e:
        logger.info("Skipping entity.identity positives — schema not present (%s)", e)
        return []

    query = """
        SELECT
            ei.library_name,
            ei.discogs_artist_id,
            a.name AS canonical_name,
            top.id AS top_id,
            top.name AS top_name,
            top.score AS top_score
        FROM entity.identity ei
        JOIN artist a ON a.id = ei.discogs_artist_id
        LEFT JOIN LATERAL (
            SELECT a2.id, a2.name,
                   similarity(lower(f_unaccent(a2.name)), lower(f_unaccent(ei.library_name))) AS score
            FROM artist a2
            WHERE lower(f_unaccent(a2.name)) % lower(f_unaccent(ei.library_name))
            ORDER BY score DESC
            LIMIT 1
        ) top ON true
        WHERE ei.discogs_artist_id IS NOT NULL
          AND lower(ei.library_name) != lower(a.name)
        ORDER BY random()
        LIMIT $1
    """
    rows = await pool.fetch(query, limit)
    return [
        CandidateScore(
            query_name=r["library_name"],
            top_name=r["top_name"] or r["library_name"],
            top_id=r["top_id"],
            score=float(r["top_score"] or 0.0),
            is_positive=r["top_id"] == r["discogs_artist_id"],
        )
        for r in rows
    ]


def sweep_thresholds(
    positives: list[CandidateScore], negatives: list[CandidateScore]
) -> list[dict]:
    """For each threshold in ``THRESHOLDS``, compute the empirical TP/FP rates.

    TPR = share of positive samples whose score >= threshold AND whose top id
    is the canonical id.
    FPR = share of negative samples whose score >= threshold.
    """
    rows = []
    n_pos = len(positives)
    n_neg = len(negatives)
    for t in THRESHOLDS:
        tp = sum(1 for p in positives if p.is_positive and p.score >= t)
        fp = sum(1 for n in negatives if n.score >= t)
        rows.append(
            {
                "threshold": t,
                "true_positive_rate": tp / n_pos if n_pos else 0.0,
                "false_positive_rate": fp / n_neg if n_neg else 0.0,
                "n_positives": n_pos,
                "n_negatives": n_neg,
                "n_swaps_at_threshold": tp + fp,
            }
        )
    return rows


def pick_floor(sweep_rows: list[dict], fp_rate_target: float) -> float:
    """Smallest threshold whose empirical FP rate is ≤ the target.

    Falls back to the highest threshold (most conservative) when no row meets
    the target.
    """
    qualifying = [r for r in sweep_rows if r["false_positive_rate"] <= fp_rate_target]
    if not qualifying:
        return max(r["threshold"] for r in sweep_rows)
    return min(r["threshold"] for r in qualifying)


def write_csv(rows: list[dict], path: Path, fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def borderline_rows(
    positives: list[CandidateScore],
    negatives: list[CandidateScore],
    floor: float,
    band: float = 0.05,
) -> list[dict]:
    """Pairs whose score sits inside ``[floor - band, floor + band]``.

    Listed for human QA; the calibration sweep alone isn't enough when the
    boundary cases are domain-specific (e.g. ambiguous trios).
    """
    lo, hi = floor - band, floor + band
    out = []
    for sample, klass in [(positives, "positive"), (negatives, "negative")]:
        for c in sample:
            if lo <= c.score <= hi:
                out.append(
                    {
                        "class": klass,
                        "score": round(c.score, 4),
                        "query_name": c.query_name,
                        "top_name": c.top_name,
                        "top_id": c.top_id,
                        "is_positive_match": c.is_positive,
                    }
                )
    out.sort(key=lambda r: float(r["score"]))  # type: ignore[arg-type]
    return out


async def main_async(args: argparse.Namespace) -> int:
    dsn = os.environ.get("DATABASE_URL_DISCOGS")
    if not dsn:
        logger.error("DATABASE_URL_DISCOGS must be set")
        return 1

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        logger.info(
            "Pulling positive class from artist_name_variation (limit=%d)",
            args.positive_sample_size,
        )
        positives_variation = await fetch_positive_class_variations(pool, args.positive_sample_size)
        logger.info("Pulling positive class from entity.identity (limit=%d)", args.identity_limit)
        positives_identity = await fetch_entity_identity_positives(pool, args.identity_limit)
        positives = positives_variation + positives_identity
        logger.info(
            "Pulling negative class from close-but-distinct artist pairs (limit=%d)",
            args.negative_sample_size,
        )
        negatives = await fetch_negative_class_close_pairs(pool, args.negative_sample_size)
    finally:
        await pool.close()

    logger.info(
        "Sweeping %d thresholds over %d positives + %d negatives",
        len(THRESHOLDS),
        len(positives),
        len(negatives),
    )
    sweep = sweep_thresholds(positives, negatives)

    output_dir = Path(args.output_dir)
    write_csv(
        sweep,
        output_dir / "calibration_sweep.csv",
        fieldnames=[
            "threshold",
            "true_positive_rate",
            "false_positive_rate",
            "n_positives",
            "n_negatives",
            "n_swaps_at_threshold",
        ],
    )

    if args.fixed_floor is not None:
        chosen_floor = args.fixed_floor
        logger.info("Using fixed floor %.2f (skipping FP-rate optimization)", chosen_floor)
    else:
        chosen_floor = pick_floor(sweep, args.fp_rate_target)
        logger.info(
            "Chosen floor %.2f (smallest threshold with FP rate <= %.4f)",
            chosen_floor,
            args.fp_rate_target,
        )

    write_csv(
        borderline_rows(positives, negatives, chosen_floor, band=args.borderline_band),
        output_dir / "borderline.csv",
        fieldnames=[
            "class",
            "score",
            "query_name",
            "top_name",
            "top_id",
            "is_positive_match",
        ],
    )

    logger.info("Wrote %s", output_dir / "calibration_sweep.csv")
    logger.info("Wrote %s", output_dir / "borderline.csv")
    logger.info(
        "Current code-level CANONICAL_ARTIST_SIMILARITY_FLOOR = %.2f",
        CANONICAL_ARTIST_SIMILARITY_FLOOR,
    )
    logger.info("Chosen empirical floor = %.2f", chosen_floor)
    if abs(chosen_floor - CANONICAL_ARTIST_SIMILARITY_FLOOR) > 0.01:
        logger.warning(
            "Empirical floor differs from code-level constant — update "
            "CANONICAL_ARTIST_SIMILARITY_FLOOR in lookup/orchestrator.py"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default="docs/resolver-calibration",
        help="Directory for CSV outputs (default: docs/resolver-calibration)",
    )
    parser.add_argument(
        "--positive-sample-size",
        type=int,
        default=5000,
        help="Random rows pulled from artist_name_variation (default: 5000)",
    )
    parser.add_argument(
        "--negative-sample-size",
        type=int,
        default=5000,
        help="Seed rows for the close-but-distinct-pair sampler (default: 5000)",
    )
    parser.add_argument(
        "--identity-limit",
        type=int,
        default=1000,
        help="Random rows pulled from entity.identity (default: 1000)",
    )
    parser.add_argument(
        "--fp-rate-target",
        type=float,
        default=0.005,
        help="False-positive rate ceiling for the chosen floor (default: 0.005)",
    )
    parser.add_argument(
        "--fixed-floor",
        type=float,
        default=None,
        help="Skip FP-rate optimization and dump borderline.csv for this floor",
    )
    parser.add_argument(
        "--borderline-band",
        type=float,
        default=0.05,
        help="Half-width of the borderline band around the chosen floor (default: 0.05)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
