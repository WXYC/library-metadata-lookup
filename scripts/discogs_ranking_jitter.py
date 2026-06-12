"""LML#537 probe #3 — Discogs ranking-jitter test.

For a ``(track, artist)`` query shape, issue two identical
``/database/search`` calls separated by a short delay and measure how
stable Discogs's ranking is. The shape of the result determines whether
the seam's per-call warm-up of ``release_track`` can structurally
benefit the *next* identical query:

* High overlap (jaccard ~ 1.0) → warming this call's release IDs warms
  the next one too. Cache warm-up is mechanically effective.
* Low overlap (jaccard < 0.5) → Discogs returns different IDs across
  calls. Warming the prior set doesn't help the next one. H2 confirmed.

The script issues queries against ``api.discogs.com`` directly rather
than through ``DiscogsService``; it deliberately bypasses the L1 LRU
and the rate-limiter semaphore so the result reflects Discogs-side
behavior, not LML-side caching. The default delay (30s) is well within
Discogs's 60req/60s window — concurrent runs against the same token
will eat into prod's budget; rerun a few times rather than in a tight
loop.

Usage::

    DISCOGS_TOKEN=... python scripts/discogs_ranking_jitter.py \\
        --track "Moments of Soft Persuasion" --artist "Yoshimura"
    DISCOGS_TOKEN=... python scripts/discogs_ranking_jitter.py \\
        --track Coastin --artist "Quiet Force" --repeat 3 --delay-seconds 45
    DISCOGS_TOKEN=... python scripts/discogs_ranking_jitter.py --track ... --artist ... --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass

import httpx

from discogs.service import DISCOGS_API_BASE

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 20


@dataclass
class JitterPair:
    """A single pair of identical-query results captured back-to-back."""

    call_a_ids: list[int]
    call_b_ids: list[int]
    delay_seconds: float

    def stats(self) -> tuple[int, int, float]:
        """Return ``(overlap, union, jaccard)`` computed in a single pass."""
        set_a = set(self.call_a_ids)
        set_b = set(self.call_b_ids)
        overlap = len(set_a & set_b)
        union = len(set_a | set_b)
        jaccard = overlap / union if union else 0.0
        return overlap, union, jaccard

    def position_stability(self) -> float | None:
        """Average |rank_a - rank_b| for IDs present in both result lists.

        Lower is more stable. ``None`` when the lists share no IDs.
        """
        rank_a = {rid: idx for idx, rid in enumerate(self.call_a_ids)}
        rank_b = {rid: idx for idx, rid in enumerate(self.call_b_ids)}
        shared = set(rank_a) & set(rank_b)
        if not shared:
            return None
        return sum(abs(rank_a[r] - rank_b[r]) for r in shared) / len(shared)

    def to_record(self) -> dict[str, object]:
        """Serializable shape for ``--json`` output. Keys:

        ``call_a_ids`` (list[int]), ``call_b_ids`` (list[int]),
        ``delay_seconds`` (float), ``overlap`` (int), ``union`` (int),
        ``jaccard`` (float), ``position_stability`` (float | None — ``None``
        when the two result lists share no IDs).
        """
        overlap, union, jaccard = self.stats()
        return {
            "call_a_ids": self.call_a_ids,
            "call_b_ids": self.call_b_ids,
            "delay_seconds": self.delay_seconds,
            "overlap": overlap,
            "union": union,
            "jaccard": jaccard,
            "position_stability": self.position_stability(),
        }


def _build_params(track: str, artist: str, limit: int) -> dict[str, str | int]:
    return {
        "type": "release",
        "track": track,
        "artist": artist,
        "per_page": limit,
    }


def _extract_release_ids(payload: dict) -> list[int]:
    """Return the ``id`` from each ``results[*]`` in Discogs's response."""
    return [int(r["id"]) for r in payload.get("results", []) if "id" in r]


async def _search_once(
    client: httpx.AsyncClient, params: dict, token: str
) -> tuple[list[int], int | None]:
    """One ``/database/search`` request; returns (release_ids, ratelimit_remaining)."""
    headers = {"Authorization": f"Discogs token={token}", "User-Agent": "LML/537-jitter"}
    resp = await client.get(f"{DISCOGS_API_BASE}/database/search", params=params, headers=headers)
    resp.raise_for_status()
    remaining = resp.headers.get("X-Discogs-Ratelimit-Remaining")
    return _extract_release_ids(resp.json()), int(remaining) if remaining else None


async def run_pair(
    client: httpx.AsyncClient,
    track: str,
    artist: str,
    delay_seconds: float,
    token: str,
    limit: int,
) -> JitterPair:
    """Issue two identical queries separated by ``delay_seconds``."""
    params = _build_params(track, artist, limit)
    ids_a, rem_a = await _search_once(client, params, token)
    await asyncio.sleep(delay_seconds)
    ids_b, rem_b = await _search_once(client, params, token)
    logger.info(
        "ratelimit_remaining: call_a=%s call_b=%s",
        rem_a if rem_a is not None else "?",
        rem_b if rem_b is not None else "?",
    )
    return JitterPair(call_a_ids=ids_a, call_b_ids=ids_b, delay_seconds=delay_seconds)


def aggregate(pairs: list[JitterPair]) -> dict[str, float | int | None]:
    """Aggregate stats across N pairs.

    ``position_stability_mean`` is ``None`` when no pair has shared IDs —
    JSON-serializes as ``null`` (strict-parser-safe), unlike ``float('nan')``
    which Python's ``json.dumps`` emits as literal ``NaN``.
    """
    if not pairs:
        return {"pairs": 0}
    jaccards = [p.stats()[2] for p in pairs]
    stabilities = [s for p in pairs if (s := p.position_stability()) is not None]
    return {
        "pairs": len(pairs),
        "jaccard_mean": sum(jaccards) / len(jaccards),
        "jaccard_min": min(jaccards),
        "jaccard_max": max(jaccards),
        "position_stability_mean": (sum(stabilities) / len(stabilities) if stabilities else None),
    }


def print_pair(pair: JitterPair, index: int) -> None:
    overlap, union, jaccard = pair.stats()
    print(
        f"  pair {index}: "
        f"|A|={len(pair.call_a_ids)} |B|={len(pair.call_b_ids)} "
        f"overlap={overlap} union={union} "
        f"jaccard={jaccard:.3f} "
        f"stability={pair.position_stability()}"
    )


def print_summary(agg: dict[str, float | int | None]) -> None:
    if not agg.get("pairs"):
        return
    print()
    print(f"pairs:                {agg['pairs']}")
    print(
        f"jaccard mean / min / max:   "
        f"{agg['jaccard_mean']:.3f} / {agg['jaccard_min']:.3f} / {agg['jaccard_max']:.3f}"
    )
    stability = agg["position_stability_mean"]
    if stability is None:
        print("position_stability_mean:  (no shared IDs across any pair)")
    else:
        print(f"position_stability_mean:  {stability:.2f}")
    print()
    print("Interpretation per LML#537 (H2):")
    print("  jaccard ~ 1.0 → warming this call's IDs warms the next; cache warm-up effective.")
    print(
        "  jaccard < 0.5 → Discogs returns different IDs across calls; "
        "warm-up is structurally ineffective for repeat lookups."
    )


async def _main_async(args: argparse.Namespace, token: str) -> int:
    pairs: list[JitterPair] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i in range(args.repeat):
            pair = await run_pair(
                client,
                track=args.track,
                artist=args.artist,
                delay_seconds=args.delay_seconds,
                token=token,
                limit=args.limit,
            )
            pairs.append(pair)
            if not args.json:
                print(f"track='{args.track}' artist='{args.artist}'")
                print_pair(pair, i + 1)

    agg = aggregate(pairs)
    if args.json:
        print(json.dumps({"pairs": [p.to_record() for p in pairs], "aggregate": agg}))
    else:
        print_summary(agg)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--track", required=True, help="Track title (queried as ?track=)")
    parser.add_argument("--artist", required=True, help="Artist name (queried as ?artist=)")
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=30.0,
        help="Seconds between the two calls of each pair (default: 30)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Number of pairs to issue (default: 1)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=f"per_page on the Discogs query (default: {_DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable output instead of a summary"
    )
    args = parser.parse_args(argv)

    token = os.environ.get("DISCOGS_TOKEN")
    if not token:
        print("error: DISCOGS_TOKEN env var required", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(_main_async(args, token))


if __name__ == "__main__":
    raise SystemExit(main())
