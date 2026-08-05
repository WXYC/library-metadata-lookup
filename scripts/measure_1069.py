"""Read-only measurement pass for LML#1069 (Bandcamp label/imprint/VA-hosted discovery).

Samples albums lacking a verified bandcamp_url from the canonical prod snapshot and probes:
  1. Mechanism (a): autocomplete API param="a" (album-type results), artist+title bound 80/80.
  2. (pending stratum only) The existing artist-first path (BandcampClient.find_album_match),
     to attribute yield between #832 (artist-first would find it) and #1069 (only album-first finds it).
Hits are auto-verified by fetching the matched album page and extracting og:title ("Title, by Artist").

Writes NOTHING to any database. Output: JSON report + console summary.

Run from the LML repo root:
    uv run --no-sync python <scratchpad>/measure_1069.py --db <snapshot.db> --out <report.json>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sqlite3
import sys
from typing import Any

sys.path.insert(0, ".")

from clients.bandcamp import AUTOCOMPLETE_URL, BandcampClient  # noqa: E402
from clients.streaming.matching import score_match  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("measure_1069")

FLOOR = 80.0
GOLDEN_REPRO_ID = 56319  # George Theodorakis — The Rules of the Game

_OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]+)"')
_DOUBLED_URL_RE = re.compile(
    r"(https://[a-z0-9-]+\.bandcamp\.com)+(https://[a-z0-9-]+\.bandcamp\.com/(?:album|track)/[^\s\"]+)"
)
_SUBDOMAIN_RE = re.compile(r"https://([a-z0-9-]+)\.bandcamp\.com")


def fix_autocomplete_url(url: str) -> str:
    """The autocomplete API returns doubled URLs like
    'https://x.bandcamp.comhttps://x.bandcamp.com/album/y' — keep the last https://... segment."""
    idx = url.rfind("https://")
    return url[idx:] if idx > 0 else url


def subdomain_of(url: str) -> str | None:
    m = _SUBDOMAIN_RE.match(url)
    return m.group(1) if m else None


async def probe_album_autocomplete(
    client: BandcampClient, artist: str, title: str
) -> dict[str, Any]:
    """Mechanism (a): autocomplete param='a', artist+title query. Returns best-scored album result."""
    q = f"{artist} {title}"[:200]
    resp = await client._request_with_retry(
        "GET", AUTOCOMPLETE_URL, params={"q": q, "param": "a"}, timeout=10.0
    )
    out: dict[str, Any] = {"n_results": 0, "hit": None, "best_below_floor": None}
    if resp is None or resp.status_code != 200:
        out["error"] = f"http {getattr(resp, 'status_code', 'none')}"
        return out
    try:
        results = resp.json().get("results", [])
    except Exception:
        out["error"] = "bad json"
        return out
    albums = [r for r in results if r.get("type") == "a"]
    out["n_results"] = len(albums)
    best = None
    for r in albums:
        a_score = score_match(artist, r.get("band_name") or "")
        t_score = score_match(title, r.get("name") or "")
        cand = {
            "matched_artist": r.get("band_name"),
            "matched_title": r.get("name"),
            "url": fix_autocomplete_url(r.get("url", "")),
            "artist_score": round(a_score, 1),
            "title_score": round(t_score, 1),
        }
        key = min(a_score, t_score)
        if best is None or key > min(best["artist_score"], best["title_score"]):
            best = cand
    if best is not None:
        if best["artist_score"] >= FLOOR and best["title_score"] >= FLOOR:
            out["hit"] = best
        else:
            out["best_below_floor"] = best
    return out


async def probe_artist_first(client: BandcampClient, artist: str, title: str) -> dict[str, Any]:
    """The existing #832 path: find_album_match (autocomplete b + own-catalog scrape)."""
    try:
        m = await client.find_album_match(artist, title)
    except Exception as exc:  # measurement must not die on one bad row
        return {"hit": None, "error": repr(exc)}
    if m is None:
        return {"hit": None}
    return {"hit": {"url": m.url, "confidence": m.confidence}}


async def verify_album_page(client: BandcampClient, url: str) -> dict[str, Any]:
    """Fetch the matched album page; extract og:title ('Title, by Artist') for precision check."""
    resp = await client._request_with_retry("GET", url, timeout=15.0, follow_redirects=True)
    if resp is None or resp.status_code != 200:
        return {"ok": False, "status": getattr(resp, "status_code", None)}
    resp.encoding = "utf-8"
    m = _OG_TITLE_RE.search(resp.text)
    return {"ok": True, "og_title": m.group(1) if m else None}


def sample_rows(db_path: str, n_notfound: int, n_pending: int, seed: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []

    def pick(where: str, n: int, stratum: str, force_ids: list[int] | None = None) -> None:
        ids = [r[0] for r in conn.execute(f"SELECT id FROM albums WHERE {where}")]
        chosen = set(rng.sample(ids, min(n, len(ids))))
        for fid in force_ids or []:
            if fid in ids:
                chosen.add(fid)
        qmarks = ",".join("?" * len(chosen))
        for r in conn.execute(
            f"SELECT id, display_artist, display_title, is_compilation, is_single, bandcamp_slug, bandcamp_status FROM albums WHERE id IN ({qmarks})",
            list(chosen),
        ):
            rows.append({**dict(r), "stratum": stratum})

    pick("bandcamp_url IS NULL AND bandcamp_status = 'not_found'", n_notfound, "not_found")
    pick(
        "bandcamp_url IS NULL AND bandcamp_status = 'pending' AND bandcamp_slug IS NULL",
        n_pending,
        "pending",
        force_ids=[GOLDEN_REPRO_ID],
    )
    conn.close()
    return rows


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-notfound", type=int, default=60)
    ap.add_argument("--n-pending", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rows = sample_rows(args.db, args.n_notfound, args.n_pending, args.seed)
    log.info(
        f"sampled {len(rows)} albums ({sum(1 for r in rows if r['stratum'] == 'not_found')} not_found, {sum(1 for r in rows if r['stratum'] == 'pending')} pending)"
    )

    client = BandcampClient()
    results: list[dict[str, Any]] = []
    try:
        for i, row in enumerate(rows, 1):
            artist, title = row["display_artist"], row["display_title"]
            rec: dict[str, Any] = {**row}
            rec["album_first"] = await probe_album_autocomplete(client, artist, title)
            if row["stratum"] == "pending":
                rec["artist_first"] = await probe_artist_first(client, artist, title)
            hit = rec["album_first"]["hit"]
            if hit:
                rec["verify"] = await verify_album_page(client, hit["url"])
                slug = subdomain_of(hit["url"]) or ""
                rec["host_kind"] = (
                    "artist-hosted"
                    if score_match(artist, slug.replace("-", " ")) >= 70
                    else "other-hosted"
                )
            results.append(rec)
            if i % 10 == 0:
                n_hits = sum(1 for r in results if r["album_first"]["hit"])
                log.info(f"{i}/{len(rows)} probed, {n_hits} album-first hits so far")
    finally:
        await client.close()

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    # ---- summary ----
    def rate(sub: list[dict], key: str) -> str:
        hits = sum(1 for r in sub if (r.get(key) or {}).get("hit"))
        return f"{hits}/{len(sub)} ({100 * hits / len(sub):.0f}%)" if sub else "n/a"

    nf = [r for r in results if r["stratum"] == "not_found"]
    pd = [r for r in results if r["stratum"] == "pending"]
    print("\n================ SUMMARY ================")
    print(f"not_found stratum: album-first hits {rate(nf, 'album_first')}")
    print(
        f"pending stratum:   album-first hits {rate(pd, 'album_first')}   artist-first hits {rate(pd, 'artist_first')}"
    )
    only_1069 = [
        r
        for r in pd
        if (r["album_first"].get("hit")) and not (r.get("artist_first") or {}).get("hit")
    ]
    print(f"pending: album-first-ONLY (the #1069 class): {len(only_1069)}/{len(pd)}")
    hosted = [r.get("host_kind") for r in results if r["album_first"].get("hit")]
    print(
        f"hit hosting split: artist-hosted={hosted.count('artist-hosted')} other-hosted={hosted.count('other-hosted')}"
    )
    golden = next((r for r in results if r["id"] == GOLDEN_REPRO_ID), None)
    if golden:
        print(
            f"golden repro (id {GOLDEN_REPRO_ID}): album_first hit = {bool(golden['album_first'].get('hit'))}"
        )


if __name__ == "__main__":
    asyncio.run(main())
