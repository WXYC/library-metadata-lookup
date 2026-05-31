"""Re-check streaming availability for releases whose ALBUM_ARTIST was previously mojibake'd.

V012 (`/tmp/V012__fix-mojibake-extended.sql`) corrects double-encoded UTF-8 artist
names in tubafrenzy that V003 missed (e.g. `Î¼-ziq` → `μ-ziq`). When the
streaming-availability pipeline ran with the corrupted strings, Spotify and
Deezer didn't recognize them and the rows were recorded as `not_found`. After
V012 lands and library.db is re-synced, those releases need a fresh check with
the correct artist name.

This script is read-only by default: it queries library.db, calls LML's
streaming-check orchestrator for each affected release, and writes a CSV report.
The actual `on_streaming` propagation runs through the existing
streaming-status webhook (admin upload diff), not this script.

Usage:
    uv run python -m scripts.recheck_streaming_after_mojibake \\
        --library-db library.db [--output recheck_report.csv] [--limit N]
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import sqlite3
import sys
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.deezer import DeezerClient
from clients.streaming.spotify import SpotifyClient
from streaming.orchestrator import check_streaming_availability

logger = logging.getLogger(__name__)


# Round-trippable corrections from V012's FLOWSHEET_ENTRY_PROD.ARTIST_NAME block.
# Source: /tmp/V012__fix-mojibake-extended.sql.
MOJIBAKE_CORRECTED_ARTISTS: tuple[str, ...] = (
    "μ-ziq",
    "GrOun士 + Kabamix",
    "Luboš Fišer",
    "Mustafah 'Abd Al'-Azīz",
    "Σtella, Las Palabras",
)


@dataclass(frozen=True)
class AffectedRelease:
    library_id: int
    artist: str
    title: str


@dataclass(frozen=True)
class RecheckResult:
    library_id: int
    artist: str
    title: str
    on_streaming: bool | None
    sources: dict[str, str | None]


def find_affected_releases(
    library_db_path: Path,
    corrected_artists: list[str] | tuple[str, ...] = MOJIBAKE_CORRECTED_ARTISTS,
) -> list[AffectedRelease]:
    """Return library rows whose `artist` or `album_artist` matches a corrected name."""
    if not corrected_artists:
        return []

    conn = sqlite3.connect(str(library_db_path))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(library)").fetchall()}
        has_album_artist = "album_artist" in cols

        placeholders = ",".join("?" for _ in corrected_artists)
        if has_album_artist:
            sql = (
                f"SELECT id, artist, title, album_artist FROM library "
                f"WHERE artist IN ({placeholders}) OR album_artist IN ({placeholders})"
            )
            params: list[str] = list(corrected_artists) + list(corrected_artists)
        else:
            sql = (
                f"SELECT id, artist, title, NULL AS album_artist FROM library "
                f"WHERE artist IN ({placeholders})"
            )
            params = list(corrected_artists)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    corrected_set = set(corrected_artists)
    seen: set[int] = set()
    affected: list[AffectedRelease] = []
    for lid, artist, title, album_artist in rows:
        if lid in seen:
            continue
        seen.add(lid)
        # Prefer the album_artist value when it's the column that matched —
        # that's the canonical credit for VA / compilation releases.
        chosen = album_artist if album_artist in corrected_set else artist
        affected.append(AffectedRelease(library_id=lid, artist=chosen or "", title=title or ""))
    return affected


async def recheck_releases(
    releases: list[AffectedRelease],
    *,
    spotify: SpotifyClient | None = None,
    deezer: DeezerClient | None = None,
    apple_music: AppleMusicClient | None = None,
    bandcamp: BandcampClient | None = None,
) -> list[RecheckResult]:
    """Re-run the streaming-check orchestrator for each release, sequentially."""
    out: list[RecheckResult] = []
    for release in releases:
        resp = await check_streaming_availability(
            release.artist,
            release.title,
            spotify=spotify,
            deezer=deezer,
            apple_music=apple_music,
            bandcamp=bandcamp,
        )
        sources: dict[str, str | None] = {
            "spotify": resp.sources.spotify.url if resp.sources.spotify else None,
            "deezer": resp.sources.deezer.url if resp.sources.deezer else None,
            "apple_music": resp.sources.apple_music.url if resp.sources.apple_music else None,
            "bandcamp": resp.sources.bandcamp.url if resp.sources.bandcamp else None,
        }
        out.append(
            RecheckResult(
                library_id=release.library_id,
                artist=release.artist,
                title=release.title,
                on_streaming=resp.on_streaming,
                sources=sources,
            )
        )
    return out


def write_report(results: list[RecheckResult], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "library_id",
                "artist",
                "title",
                "on_streaming",
                "spotify_url",
                "deezer_url",
                "apple_music_url",
                "bandcamp_url",
            ]
        )
        for r in results:
            writer.writerow(
                [
                    r.library_id,
                    r.artist,
                    r.title,
                    "" if r.on_streaming is None else int(r.on_streaming),
                    r.sources.get("spotify") or "",
                    r.sources.get("deezer") or "",
                    r.sources.get("apple_music") or "",
                    r.sources.get("bandcamp") or "",
                ]
            )


def _build_clients() -> tuple[
    SpotifyClient | None, DeezerClient, AppleMusicClient | None, BandcampClient
]:
    spotify: SpotifyClient | None = None
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    csec = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if cid and csec:
        spotify = SpotifyClient(cid, csec)
    else:
        logger.warning("SPOTIFY_CLIENT_ID/SECRET not set — skipping Spotify check")

    apple_music: AppleMusicClient | None = None
    am_team = os.environ.get("APPLE_MUSIC_TEAM_ID")
    am_key = os.environ.get("APPLE_MUSIC_KEY_ID")
    am_priv = os.environ.get("APPLE_MUSIC_PRIVATE_KEY")
    if am_team and am_key and am_priv:
        apple_music = AppleMusicClient(team_id=am_team, key_id=am_key, private_key=am_priv)
    else:
        logger.warning(
            "APPLE_MUSIC_TEAM_ID/KEY_ID/PRIVATE_KEY not all set — skipping Apple Music check"
        )

    return spotify, DeezerClient(), apple_music, BandcampClient()


async def _close_clients(*clients) -> None:
    for c in clients:
        if c is not None:
            try:
                await c.close()
            except Exception:
                logger.exception("Failed to close client %r", c)


async def run(args) -> None:
    affected = find_affected_releases(args.library_db, MOJIBAKE_CORRECTED_ARTISTS)
    if args.limit:
        affected = affected[: args.limit]

    logger.info(
        "Found %d affected release(s) for %d corrected artist(s)",
        len(affected),
        len(MOJIBAKE_CORRECTED_ARTISTS),
    )

    if not affected:
        write_report([], args.output)
        return

    spotify, deezer, apple, bandcamp = _build_clients()
    try:
        results = await recheck_releases(
            affected, spotify=spotify, deezer=deezer, apple_music=apple, bandcamp=bandcamp
        )
    finally:
        await _close_clients(spotify, deezer, apple, bandcamp)

    write_report(results, args.output)
    flips_true = sum(1 for r in results if r.on_streaming is True)
    flips_false = sum(1 for r in results if r.on_streaming is False)
    inconclusive = sum(1 for r in results if r.on_streaming is None)
    logger.info(
        "Recheck complete: on_streaming=true:%d false:%d inconclusive:%d. Report: %s",
        flips_true,
        flips_false,
        inconclusive,
        args.output,
    )


def main() -> int:
    load_dotenv()
    parser = ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--library-db", type=Path, default=Path("library.db"))
    parser.add_argument("--output", type=Path, default=Path("recheck_streaming_report.csv"))
    parser.add_argument("--limit", type=int, default=0, help="Cap the number of releases (0 = all)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.library_db.exists():
        logger.error("Library DB not found: %s", args.library_db)
        return 1

    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
