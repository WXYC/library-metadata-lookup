"""Core drain loop for the bulk artist-resolve endpoint (LML#759 PR D).

Drives prod `POST /api/v1/artists/resolve/bulk` over a Backend-Service-exported
name set (BS#1614), paging at the endpoint's 25-name cap. Every verdict is
appended to a JSONL log so a crash mid-drain resumes without re-paying the
API-verification cost of the batches already settled. The single retryable
verdict — `escalation_unavailable` (LML#755 breaker open / Discogs outage /
429 / 5xx-after-retries) — is re-paged after a cool-down, bounded by
`max_retries`, then reported as residual.

The loop is network- and clock-free at its core: `run_drain` takes an injected
`post_batch` coroutine (built by `make_post_batch` around an `httpx.AsyncClient`
in `__main__`) plus injectable `sleep`/`clock`, so the resume/retry logic is
unit-testable without touching Discogs or waiting out a cool-down.

Report aggregation and the spot-check sampler live in the sibling
:mod:`scripts.artist_resolve_drain.report`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

logger = logging.getLogger("artist_resolve_drain")

# The endpoint's `maxItems` cap; a fully-escalating page of 25 ≈ 25 API calls
# ≈ 30s at the shared 50/min Discogs budget (LML#759 design).
PAGE_SIZE = 25
RESOLVE_PATH = "/api/v1/artists/resolve/bulk"

# `escalation_unavailable` is the retryable verdict; 2 retries (3 attempts total)
# matches the "bounded retries (2-3)" the design specifies.
DEFAULT_MAX_RETRIES = 2
DEFAULT_COOLDOWN_SECONDS = 60

# Verdict kinds that never change on a re-page — the drain settles them once.
# `escalation_unavailable` is deliberately excluded: it is the one retryable
# verdict (re-paged after a cool-down, up to max_retries).
_TERMINAL_REASONS = frozenset({"not_found", "ambiguous"})


class DrainError(RuntimeError):
    """A contract break from the resolve endpoint (bad length / index misalignment)."""


class _ShutdownFlag(Protocol):
    @property
    def requested(self) -> bool: ...


class _PostBatch(Protocol):
    async def __call__(self, names: list[str], dry_run: bool) -> list[dict[str, Any]]: ...


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #
def parse_names_file(text: str) -> list[str]:
    """Parse a names handoff file into an ordered, de-duplicated name list.

    Accepts either a JSON array of strings or a newline-delimited list. Blank
    lines are dropped and surrounding whitespace trimmed; exact duplicates are
    collapsed preserving first-occurrence order (the endpoint dedupes on the
    identity-match form internally, but collapsing verbatim repeats here avoids
    wasting page slots).
    """
    stripped = text.strip()
    if stripped:
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return _dedupe([item.strip() for item in parsed])
    return _dedupe(line.strip() for line in text.splitlines())


def _dedupe(names: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def chunk(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    """Yield successive ``size``-length pages from ``items``."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


# --------------------------------------------------------------------------- #
# Verdict records
# --------------------------------------------------------------------------- #
def record_from_verdict(
    verdict: dict[str, Any], *, dry_run: bool, attempt: int, ts: str
) -> dict[str, Any]:
    """Project an ``ArtistResolveResult`` verdict onto a JSONL drain record."""
    return {
        "name": verdict["name"],
        "discogs_artist_id": verdict.get("discogs_artist_id"),
        "canonical_name": verdict.get("canonical_name"),
        "method": verdict.get("method"),
        "cache_corroboration": list(verdict.get("cache_corroboration") or []),
        "unresolved_reason": verdict.get("unresolved_reason"),
        "candidate_count": verdict.get("candidate_count"),
        "dry_run": dry_run,
        "attempt": attempt,
        "ts": ts,
    }


def is_terminal(rec: dict[str, Any]) -> bool:
    """Whether a verdict will not change on a re-page (resolved / not_found / ambiguous)."""
    if rec.get("discogs_artist_id") is not None:
        return True
    return rec.get("unresolved_reason") in _TERMINAL_REASONS


def latest_by_name(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Latest (last-appended) record per name — append order is attempt order."""
    latest: dict[str, dict[str, Any]] = {}
    for rec in records:
        latest[rec["name"]] = rec
    return latest


def attempt_counts(records: Sequence[dict[str, Any]]) -> dict[str, int]:
    """How many times each name has been paged (one record per attempt)."""
    counts: dict[str, int] = {}
    for rec in records:
        counts[rec["name"]] = counts.get(rec["name"], 0) + 1
    return counts


def filter_records_for_mode(
    records: Sequence[dict[str, Any]], *, dry_run: bool
) -> list[dict[str, Any]]:
    """Keep only records written in the current mode.

    Dry and live records may share one JSONL file; resume and reporting are
    mode-scoped so a prior dry drain never makes a `--live` run skip the mint.
    """
    return [rec for rec in records if bool(rec.get("dry_run")) is dry_run]


def compute_pending(
    all_names: Sequence[str], records: Sequence[dict[str, Any]], max_attempts: int
) -> list[str]:
    """The names still needing a page: unseen, plus retryable escalations under cap.

    ``records`` must already be scoped to the current mode
    (see :func:`filter_records_for_mode`).
    """
    latest = latest_by_name(records)
    counts = attempt_counts(records)
    pending: list[str] = []
    seen: set[str] = set()
    for name in all_names:
        if name in seen:
            continue
        seen.add(name)
        rec = latest.get(name)
        if rec is None:
            pending.append(name)
        elif is_terminal(rec):
            continue
        elif counts.get(name, 0) < max_attempts:
            # escalation_unavailable, retries remaining
            pending.append(name)
    return pending


# --------------------------------------------------------------------------- #
# JSONL persistence
# --------------------------------------------------------------------------- #
def load_records(path: str | Path) -> list[dict[str, Any]]:
    """Load all JSONL drain records; an absent file is an empty run."""
    p = Path(path)
    if not p.exists():
        return []
    records: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def append_record(path: str | Path, rec: dict[str, Any]) -> None:
    """Append one record as a JSON line, flushing so a crash keeps prior lines."""
    p = Path(path)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()


# --------------------------------------------------------------------------- #
# HTTP envelope
# --------------------------------------------------------------------------- #
async def resolve_batch(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    names: list[str],
    dry_run: bool,
) -> list[dict[str, Any]]:
    """POST one page to the resolve endpoint and return its index-aligned verdicts.

    Raises :class:`DrainError` on a length or index-alignment break (the endpoint
    contract is to echo each input ``name`` verbatim, index-aligned), and
    ``httpx.HTTPStatusError`` on a non-2xx response.
    """
    url = base_url.rstrip("/") + RESOLVE_PATH
    resp = await client.post(
        url,
        json={"names": names, "dry_run": dry_run},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    resp.raise_for_status()
    results = resp.json()["results"]
    if len(results) != len(names):
        raise DrainError(f"resolve returned {len(results)} results for {len(names)} names")
    for sent, got in zip(names, results, strict=True):
        if got.get("name") != sent:
            raise DrainError(f"resolve index misalignment: sent {sent!r}, got {got.get('name')!r}")
    return results


def make_post_batch(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    *,
    max_http_retries: int = 3,
    backoff: float = 2.0,
    sleep=asyncio.sleep,
) -> _PostBatch:
    """Build a `post_batch` coroutine that retries transient transport/5xx errors.

    Per-name `escalation_unavailable` is a 200-response verdict handled by the
    drain loop's cool-down, not here; this retry only covers HTTP-layer flakes
    (timeouts, connection resets, edge 5xx). A 4xx (401/413/400) is a misconfig
    and is not retried.
    """

    async def post_batch(names: list[str], dry_run: bool) -> list[dict[str, Any]]:
        last_exc: Exception | None = None
        for attempt in range(1, max_http_retries + 1):
            try:
                return await resolve_batch(client, base_url, api_key, names, dry_run)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise  # client error — misconfig, do not retry
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            if attempt < max_http_retries:
                wait = backoff * attempt
                logger.warning(
                    "batch HTTP error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt,
                    max_http_retries,
                    wait,
                    last_exc,
                )
                await sleep(wait)
        assert last_exc is not None
        raise last_exc

    return post_batch


# --------------------------------------------------------------------------- #
# The drain loop
# --------------------------------------------------------------------------- #
async def run_drain(
    *,
    all_names: Sequence[str],
    dry_run: bool,
    out_path: str | Path,
    post_batch: _PostBatch,
    page_size: int = PAGE_SIZE,
    max_retries: int = DEFAULT_MAX_RETRIES,
    cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    shutdown: _ShutdownFlag | None = None,
    sleep=asyncio.sleep,
    clock=_utc_now_iso,
) -> list[dict[str, Any]]:
    """Drive the drain to completion (or shutdown), returning the mode's records.

    Resumes from any existing JSONL at ``out_path`` (mode-scoped), pages the
    pending set at ``page_size``, and after each round sleeps ``cooldown`` and
    re-pages the `escalation_unavailable` residue up to ``max_retries`` times.
    """
    max_attempts = max_retries + 1
    records = filter_records_for_mode(load_records(out_path), dry_run=dry_run)
    round_idx = 0
    while True:
        if shutdown is not None and shutdown.requested:
            logger.info("shutdown requested; stopping drain")
            break
        pending = compute_pending(all_names, records, max_attempts)
        if not pending:
            break
        if round_idx > 0:
            logger.info(
                "cool-down %ss before retry round %d over %d escalation_unavailable name(s)",
                cooldown,
                round_idx,
                len(pending),
            )
            await sleep(cooldown)
            if shutdown is not None and shutdown.requested:
                logger.info("shutdown requested during cool-down; stopping drain")
                break
        counts = attempt_counts(records)
        for batch in chunk(pending, page_size):
            if shutdown is not None and shutdown.requested:
                logger.info("shutdown requested; stopping mid-round")
                return records
            verdicts = await post_batch(batch, dry_run)
            ts = clock()
            for name, verdict in zip(batch, verdicts, strict=True):
                attempt = counts.get(name, 0) + 1
                rec = record_from_verdict(verdict, dry_run=dry_run, attempt=attempt, ts=ts)
                append_record(out_path, rec)
                records.append(rec)
            logger.info(
                "paged %d name(s) [round %d]; %d records total",
                len(batch),
                round_idx,
                len(records),
            )
        round_idx += 1
    return records
