#!/usr/bin/env python3
"""Gate 0 burst harness for LML#983 (cross-worker in-memory cache fragmentation).

``UVICORN_WORKERS=3`` (LML#747) gives each uvicorn worker process its own
in-memory ``TTLCache`` heap (``discogs/memory_cache.py``) with no cross-process
sharing and no connection stickiness, so the *same* ``/lookup`` query oscillates
warm (~67 ms) <-> cold (~2.4 s) depending purely on which worker the kernel
hands the connection. #747 added the multi-worker headroom to drain event-loop
starvation, but #949/PR#899 (the synchronous PostHog-flush fix) removed its
main p50 justification — #747's own framing was "burst headroom, not p50".

Gate 0 is the cheapest possible resolution: confirm, with a real measurement,
whether the burst headroom is still needed. This script fires a concurrent
``/lookup`` burst at a target host (staging, under supervision) and reports
p50/p95/p99 wall time plus ``event_loop_lag``, so a human can run it once
against ``UVICORN_WORKERS=1`` and once against ``=3`` and compare. See the
Gate 0 runbook in ``docs/deployment.md`` (Horizontal-scaling runbook section)
for the full procedure, including the ``DISCOGS_RATE_LIMIT`` coupled-knob
unwind that reverting to a single worker requires.

SAFETY. Staging shares prod's Discogs token and the LML#755 saturation
breaker/LML#841 shared rate bucket. A burst against the real query set issues
live Discogs ``/database/search`` calls. This script:

- Defaults ``--concurrency``/``--total`` to modest values and refuses to run a
  larger burst against ``/lookup`` without an explicit ``--force``
  (``check_burst_size_within_safe_bounds``).
- Aborts the run (stops issuing new requests, reports what it has, exits
  non-zero) the moment it observes a shed response -- HTTP 429/5xx, or a 200
  with ``degraded: true`` and ``degraded_reason: "upstream_unavailable"`` (the
  client-visible signal for a Discogs-breaker shed or rate-limit condition;
  see ``generated/api_models.py::DegradedReason``). ``deadline_exceeded`` is a
  caller-budget shed, not Discogs saturation, and does not trip this rail.
- Supports ``--smoke``, which points the burst at ``GET /health`` instead of
  ``POST /api/v1/lookup`` -- zero Discogs risk, no API key required -- to
  validate the concurrency + request-firing plumbing before ever touching the
  real query set. Note ``/health`` emits no ``Server-Timing`` header, so a
  smoke run does NOT exercise the header parser and reports
  ``server_timing_present: false`` by design.

Usage::

    # Plumbing smoke test against staging /health (no Discogs risk, no auth)
    python scripts/gate0_burst.py --host https://<staging-domain> --smoke

    # The real Gate 0 measurement (human-supervised; run once per worker count)
    LML_API_KEY=... python scripts/gate0_burst.py \\
        --host https://<staging-domain> --concurrency 3 --total 12 --warm --json

Environment variables are loaded automatically from ``.env`` if present.
``--api-key`` defaults to ``$LML_API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

LOOKUP_PATH = "/api/v1/lookup"
HEALTH_PATH = "/health"

# Burst-size safety ceiling for the REAL /lookup query set (not --smoke).
# Staging shares prod's Discogs token/breaker/rate bucket; a burst larger than
# this risks tripping the LML#755 saturation breaker or the LML#841 shared
# rate bucket for live traffic. Raise only with --force, and only under
# supervision -- see the Gate 0 runbook in docs/deployment.md.
_MAX_SAFE_CONCURRENCY = 5
_MAX_SAFE_TOTAL = 30

# Representative WXYC query set for Gate 0 (LML#983). The first entry is the
# exact compilation-track query from the issue trace -- the Wave B
# (`search_releases_by_track`, artist_as_keyword=True) path, which has no PG
# tier (LML#393) and costs 2 live Discogs calls when cold, making it the
# highest-value probe for the warm/cold oscillation Gate 0 is measuring. The
# other two are ordinary artist+album queries (this repo's standard example
# fixtures) so the burst isn't 100% worst-case.
GATE0_QUERIES: list[dict[str, str | None]] = [
    {
        "label": "C. Spencer Yeh / In The Blink Of An Eye (compilation, Wave B)",
        "artist": "c spencer yeh",
        "song": "in the blink of an eye",
    },
    {
        "label": "Stereolab / Aluminum Tunes",
        "artist": "Stereolab",
        "album": "Aluminum Tunes",
    },
    {
        "label": "Jessica Pratt / On Your Own Love Again",
        "artist": "Jessica Pratt",
        "album": "On Your Own Love Again",
    },
]

# The two Server-Timing legs Gate 0 cares about (LML#907/#946): the
# middleware-appended total wall clock and the process-global event-loop-lag
# sample. See core/server_timing_middleware.py and lookup/server_timing_legs.py.
LML_WALL_LEG = "lml_wall"
EVENT_LOOP_LAG_LEG = "event_loop_lag"


def parse_server_timing(header: str | None) -> dict[str, float]:
    """Parse a ``Server-Timing`` response-header value into ``{name: dur_ms}``.

    Follows the wire format LML actually emits (``wxyc_fastapi``'s
    ``RequestTelemetry.as_server_timing`` plus ``LmlWallTimingMiddleware``):
    comma-separated ``name;dur=<ms>`` entries, optionally carrying other
    ``;key=value`` params (e.g. a spec-legal ``desc="..."``) in any order.
    Unknown params are ignored; a leg with no parseable ``dur`` is simply
    absent from the result (never zeroed -- a caller must be able to tell
    "leg omitted" from "leg measured zero").

    Never raises: a malformed header, entry, or ``dur`` value is skipped, not
    propagated. Returns ``{}`` for ``None``, empty, or whitespace-only input.
    """
    if not header or not header.strip():
        return {}

    legs: dict[str, float] = {}
    for raw_entry in header.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        parts = entry.split(";")
        name = parts[0].strip()
        if not name:
            continue
        dur: float | None = None
        for raw_param in parts[1:]:
            param = raw_param.strip()
            if not param or "=" not in param:
                continue
            key, _, value = param.partition("=")
            key = key.strip().lower()
            if key != "dur":
                continue
            value = value.strip().strip('"')
            try:
                dur = float(value)
            except ValueError:
                logger.warning("Malformed Server-Timing dur for leg %r: %r", name, value)
                dur = None
        if dur is not None:
            legs[name] = dur
    return legs


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (numpy's default ``'linear'`` method).

    ``pct`` in ``[0, 100]``. Raises ``ValueError`` on an empty ``values`` --
    callers with a possibly-empty series should go through
    :func:`summarize_durations`, which handles that case explicitly.
    """
    if not values:
        raise ValueError("percentile() requires at least one value")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (pct / 100.0) * (len(ordered) - 1)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


@dataclass(frozen=True)
class DurationSummary:
    """p50/p95/p99 + bounds for a series of millisecond durations.

    All fields are ``None`` when the source series is empty (e.g. every
    request in a run failed before a Server-Timing header was captured) --
    distinct from a real ``0.0`` measurement.
    """

    count: int
    p50: float | None
    p95: float | None
    p99: float | None
    min: float | None
    max: float | None
    mean: float | None

    def to_dict(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
            "min": self.min,
            "max": self.max,
            "mean": self.mean,
        }


def summarize_durations(values: list[float]) -> DurationSummary:
    """Aggregate a series of millisecond durations into a :class:`DurationSummary`."""
    if not values:
        return DurationSummary(count=0, p50=None, p95=None, p99=None, min=None, max=None, mean=None)
    return DurationSummary(
        count=len(values),
        p50=percentile(values, 50),
        p95=percentile(values, 95),
        p99=percentile(values, 99),
        min=min(values),
        max=max(values),
        mean=sum(values) / len(values),
    )


def classify_warm_cold(values: list[float], threshold_ms: float) -> tuple[int, int]:
    """Split a duration series into (warm, cold) counts at ``threshold_ms``.

    A value exactly at the threshold counts as cold (fail toward flagging
    fragmentation, not hiding it). This is the Gate 0 bimodality signal: under
    healthy single-tier caching, one pass should collapse to ~all-warm; a
    persistent warm/cold split under a burst is the fragmentation the issue
    describes.
    """
    warm = sum(1 for v in values if v < threshold_ms)
    return warm, len(values) - warm


def is_shed_response(status_code: int, body: dict[str, Any] | None) -> bool:
    """True if a ``/lookup`` response indicates the shared Discogs breaker or
    rate bucket sheds/degraded traffic -- the Gate 0 safety-rail trigger.

    Two independent signals, either sufficient:

    - A blunt HTTP 429 or any 5xx. LML's own admission-control paths (the
      in-flight semaphore, the bulk-global permit) queue rather than shed, so
      a 429/5xx here means something upstream of LML (or LML's own uncaught
      exception handler) rejected the request outright.
    - A 200 response body carrying ``degraded: true`` with
      ``degraded_reason: "upstream_unavailable"`` -- the client-visible signal
      wired by LML#930/#931 for "an upstream (Discogs, streaming providers)
      was rate limited or down and its contribution was omitted" (see
      ``generated/api_models.py::DegradedReason``). This is how a
      LML#755-breaker shed or an exhausted Discogs rate budget actually
      surfaces to a ``/lookup`` caller -- it is swallowed into a degraded 200,
      not a distinct HTTP status.

    ``degraded_reason: "deadline_exceeded"`` is deliberately excluded: it
    means the *caller's* budget (``X-Caller-Budget-Ms``) or LML's own hard
    cap elapsed, which is unrelated to Discogs saturation and must not abort
    a Gate 0 run just because a single slow cold-tail request ran past its
    budget.
    """
    if status_code == 429 or status_code >= 500:
        return True
    if body is None:
        return False
    return bool(body.get("degraded")) and body.get("degraded_reason") == "upstream_unavailable"


def check_burst_size_within_safe_bounds(
    *, concurrency: int, total: int, smoke: bool, warm: bool
) -> str | None:
    """Return an error message if the requested burst risks the shared Discogs
    budget, or ``None`` if it's within the safe default envelope.

    ``--smoke`` targets ``GET /health`` (no Discogs, no auth) so it is exempt
    -- the rail exists to protect the shared token, not to cap request volume
    in the abstract. A caller can override with ``--force``, but the default
    posture is to refuse silently-large bursts rather than let a typo or a
    copy-pasted flag fire an oversized wave at the prod-shared breaker.

    The ``--warm`` prewarm pass fires one extra live ``/lookup`` per distinct
    query (``len(GATE0_QUERIES)``) *before* the ``total``-sized burst, so the
    ceiling is checked against the true live-request count (``total`` +
    prewarm) -- otherwise the rail would understate the real Discogs load by
    that many calls.
    """
    if smoke:
        return None
    if concurrency > _MAX_SAFE_CONCURRENCY:
        return (
            f"--concurrency {concurrency} exceeds the safe default ceiling "
            f"({_MAX_SAFE_CONCURRENCY}); staging shares prod's Discogs token and "
            "breaker. Pass --force to override, under supervision."
        )
    prewarm = len(GATE0_QUERIES) if warm else 0
    effective_total = total + prewarm
    if effective_total > _MAX_SAFE_TOTAL:
        prewarm_note = f" (+{prewarm} prewarm)" if prewarm else ""
        return (
            f"--total {total}{prewarm_note} = {effective_total} live /lookup calls "
            f"exceeds the safe default ceiling ({_MAX_SAFE_TOTAL}); staging shares "
            "prod's Discogs token and breaker. Pass --force to override, under "
            "supervision."
        )
    return None


@dataclass
class RequestOutcome:
    """One fired request's result, whatever path it took."""

    label: str
    status_code: int | None
    client_wall_ms: float
    lml_wall_ms: float | None = None
    event_loop_lag_ms: float | None = None
    shed: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "status_code": self.status_code,
            "client_wall_ms": self.client_wall_ms,
            "lml_wall_ms": self.lml_wall_ms,
            "event_loop_lag_ms": self.event_loop_lag_ms,
            "shed": self.shed,
            "error": self.error,
        }


async def _fire_lookup(
    client: httpx.AsyncClient,
    host: str,
    api_key: str,
    query: dict[str, str | None],
    timeout: float,
) -> RequestOutcome:
    label = str(query.get("label", "lookup"))
    payload = {k: v for k, v in query.items() if k != "label"}
    start = time.perf_counter()
    try:
        resp = await client.post(
            host.rstrip("/") + LOOKUP_PATH,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        client_wall_ms = (time.perf_counter() - start) * 1000.0
        return RequestOutcome(
            label=label, status_code=None, client_wall_ms=client_wall_ms, error=str(exc)
        )

    client_wall_ms = (time.perf_counter() - start) * 1000.0
    body: dict[str, Any] | None
    try:
        body = resp.json()
    except ValueError:
        body = None

    legs = parse_server_timing(resp.headers.get("server-timing"))
    return RequestOutcome(
        label=label,
        status_code=resp.status_code,
        client_wall_ms=client_wall_ms,
        lml_wall_ms=legs.get(LML_WALL_LEG),
        event_loop_lag_ms=legs.get(EVENT_LOOP_LAG_LEG),
        shed=is_shed_response(resp.status_code, body),
    )


async def _fire_health(client: httpx.AsyncClient, host: str, timeout: float) -> RequestOutcome:
    start = time.perf_counter()
    try:
        resp = await client.get(host.rstrip("/") + HEALTH_PATH, timeout=timeout)
    except httpx.HTTPError as exc:
        client_wall_ms = (time.perf_counter() - start) * 1000.0
        return RequestOutcome(
            label="health", status_code=None, client_wall_ms=client_wall_ms, error=str(exc)
        )

    client_wall_ms = (time.perf_counter() - start) * 1000.0
    legs = parse_server_timing(resp.headers.get("server-timing"))
    return RequestOutcome(
        label="health",
        status_code=resp.status_code,
        client_wall_ms=client_wall_ms,
        lml_wall_ms=legs.get(LML_WALL_LEG),
        event_loop_lag_ms=legs.get(EVENT_LOOP_LAG_LEG),
        # No response body on /health, so only the status-code arm can fire;
        # route through is_shed_response so the shed predicate stays single-sourced.
        shed=is_shed_response(resp.status_code, None),
    )


@dataclass
class BurstResult:
    # Concurrent-burst outcomes -- the measurement series build_report aggregates.
    outcomes: list[RequestOutcome] = field(default_factory=list)
    # Sequential --warm prewarm outcomes, kept OUT of the aggregate: they are
    # deliberately cold (they exist to warm one worker's heap) and would inflate
    # the burst's p95/p99/max + warm-cold split if folded in (LML#983 review
    # finding 1). Retained here for transparency / shed-abort accounting only.
    prewarm_outcomes: list[RequestOutcome] = field(default_factory=list)
    aborted_on_shed: bool = False


async def run_burst(
    *,
    host: str,
    api_key: str | None,
    concurrency: int,
    total: int,
    warm: bool,
    smoke: bool,
    timeout: float,
) -> BurstResult:
    """Fire the Gate 0 burst and return every :class:`RequestOutcome`.

    Prewarm pass (``--warm``, skipped under ``--smoke``): one sequential hit
    per distinct query before the concurrent burst -- mirrors the issue
    trace's "run 1" (the request that warms exactly one worker's heap), so the
    concurrent burst that follows reproduces the same warm-one/cold-others
    shape rather than starting every worker stone cold.

    The concurrent burst then fires ``total`` requests (round-robin over
    :data:`GATE0_QUERIES`, or all ``GET /health`` under ``--smoke``) through a
    pool of ``concurrency`` workers pulling from a shared queue. The pool
    stops pulling new work the instant any outcome is a shed response
    (:func:`is_shed_response`) -- already in-flight requests are allowed to
    finish (their data is still useful), but no new request is issued.
    """
    result = BurstResult()
    abort_event = asyncio.Event()

    # Narrowed once, up front: mypy can't see through the closure below that
    # `api_key` is non-None just from an `assert` inside this scope. At
    # runtime `main()` already refuses to call in without one unless --smoke;
    # the assert re-states that contract for any other caller (e.g. a test).
    if not smoke:
        assert api_key is not None, "api_key is required unless smoke=True"
    resolved_api_key = api_key if api_key is not None else ""

    async with httpx.AsyncClient() as client:
        if warm and not smoke:
            for query in GATE0_QUERIES:
                outcome = await _fire_lookup(client, host, resolved_api_key, query, timeout)
                result.prewarm_outcomes.append(outcome)
                if outcome.shed:
                    result.aborted_on_shed = True
                    logger.error(
                        "Gate 0 ABORTED during prewarm: shed response on %r (status=%s)",
                        outcome.label,
                        outcome.status_code,
                    )
                    return result

        work_items: list[dict[str, str | None]] = []
        if smoke:
            work_items = [{"label": "health"}] * total
        else:
            for i in range(total):
                work_items.append(GATE0_QUERIES[i % len(GATE0_QUERIES)])

        queue: asyncio.Queue[dict[str, str | None]] = asyncio.Queue()
        for item in work_items:
            queue.put_nowait(item)

        async def worker() -> None:
            while not queue.empty() and not abort_event.is_set():
                try:
                    query = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if smoke:
                    outcome = await _fire_health(client, host, timeout)
                else:
                    outcome = await _fire_lookup(client, host, resolved_api_key, query, timeout)
                result.outcomes.append(outcome)
                if outcome.shed:
                    abort_event.set()
                    logger.error(
                        "Gate 0 ABORTED: shed response on %r (status=%s). "
                        "Stopping -- staging shares prod's Discogs breaker/rate bucket.",
                        outcome.label,
                        outcome.status_code,
                    )

        workers = [asyncio.create_task(worker()) for _ in range(max(1, concurrency))]
        await asyncio.gather(*workers)
        result.aborted_on_shed = result.aborted_on_shed or abort_event.is_set()

    return result


def build_report(
    outcomes: list[RequestOutcome], warm_threshold_ms: float, smoke: bool = False
) -> dict[str, Any]:
    """Aggregate the concurrent-burst outcomes into the human/JSON report shape.

    ``outcomes`` is the *burst* series only -- the ``--warm`` prewarm outcomes
    are deliberately excluded upstream (see :class:`BurstResult`) so they don't
    skew these percentiles. ``smoke`` records whether the run targeted
    ``GET /health`` (which emits no ``Server-Timing`` header), so the renderer
    can tell an expected-absent-header smoke run apart from a real
    ``LML_EMIT_SERVER_TIMING``-is-off fault.
    """
    client_wall = [o.client_wall_ms for o in outcomes]
    lml_wall = [o.lml_wall_ms for o in outcomes if o.lml_wall_ms is not None]
    event_loop_lag = [o.event_loop_lag_ms for o in outcomes if o.event_loop_lag_ms is not None]
    errors = sum(1 for o in outcomes if o.error is not None)
    shed_count = sum(1 for o in outcomes if o.shed)
    warm_count, cold_count = classify_warm_cold(lml_wall, warm_threshold_ms)

    return {
        "total_requests": len(outcomes),
        "smoke": smoke,
        "errors": errors,
        "shed_count": shed_count,
        "server_timing_present": bool(lml_wall),
        "warm_threshold_ms": warm_threshold_ms,
        "warm_count": warm_count,
        "cold_count": cold_count,
        "client_wall_ms": summarize_durations(client_wall).to_dict(),
        "lml_wall_ms": summarize_durations(lml_wall).to_dict(),
        "event_loop_lag_ms": summarize_durations(event_loop_lag).to_dict(),
    }


def render_human(report: dict[str, Any]) -> str:
    lines = []
    lines.append(
        f"Total requests: {report['total_requests']}  errors: {report['errors']}  "
        f"shed: {report['shed_count']}"
    )
    prewarm_requests = report.get("prewarm_requests", 0)
    if prewarm_requests:
        lines.append(
            f"(+{prewarm_requests} prewarm requests fired and excluded from the measurement below)"
        )
    if not report["server_timing_present"]:
        if report.get("smoke"):
            lines.append(
                "Note: --smoke targets GET /health, which emits no Server-Timing "
                "header, so lml_wall/event_loop_lag are N/A by design. This run "
                "validated the concurrency + request-firing plumbing only, not the "
                "Server-Timing parser."
            )
        else:
            lines.append(
                "WARNING: no Server-Timing header observed on any response -- "
                "LML_EMIT_SERVER_TIMING may be off on the target, or every request "
                "errored before a header arrived. Falling back to client-measured "
                "wall time only for the table below."
            )
    lines.append(
        f"Warm/cold split (lml_wall < {report['warm_threshold_ms']:.0f}ms = warm): "
        f"{report['warm_count']} warm / {report['cold_count']} cold"
    )
    lines.append("")
    header = f"{'metric':<16} {'count':>6} {'p50':>10} {'p95':>10} {'p99':>10} {'max':>10}"
    lines.append(header)
    lines.append("-" * len(header))
    for name, key in (
        ("client_wall_ms", "client_wall_ms"),
        ("lml_wall_ms", "lml_wall_ms"),
        ("event_loop_lag_ms", "event_loop_lag_ms"),
    ):
        s = report[key]

        def fmt(v: float | None) -> str:
            return f"{v:.1f}" if v is not None else "N/A"

        lines.append(
            f"{name:<16} {s['count']:>6} {fmt(s['p50']):>10} {fmt(s['p95']):>10} "
            f"{fmt(s['p99']):>10} {fmt(s['max']):>10}"
        )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", required=True, help="Target LML base URL (e.g. staging domain)")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("LML_API_KEY"),
        help="Bearer token; defaults to $LML_API_KEY",
    )
    parser.add_argument("--concurrency", type=int, default=3, help="Concurrent workers (default 3)")
    parser.add_argument("--total", type=int, default=12, help="Total requests to fire (default 12)")
    parser.add_argument(
        "--warm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Sequential prewarm pass (one hit per query) before the concurrent burst (default on)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Hit GET /health instead of POST /api/v1/lookup -- zero Discogs risk, no auth needed. "
        "Use this to validate the harness plumbing before ever touching the real query set.",
    )
    parser.add_argument(
        "--warm-threshold-ms",
        type=float,
        default=500.0,
        help="lml_wall below this is 'warm', at/above is 'cold' (default 500.0)",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout, seconds")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Override the burst-size safety rail (see check_burst_size_within_safe_bounds)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of a table"
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def main() -> None:
    load_dotenv()
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if not args.smoke and not args.api_key:
        print("ERROR: --api-key or $LML_API_KEY is required (unless --smoke)", file=sys.stderr)
        sys.exit(1)

    if not args.force:
        bounds_error = check_burst_size_within_safe_bounds(
            concurrency=args.concurrency, total=args.total, smoke=args.smoke, warm=args.warm
        )
        if bounds_error:
            print(f"ERROR: {bounds_error}", file=sys.stderr)
            sys.exit(1)

    result = asyncio.run(
        run_burst(
            host=args.host,
            api_key=args.api_key,
            concurrency=args.concurrency,
            total=args.total,
            warm=args.warm,
            smoke=args.smoke,
            timeout=args.timeout,
        )
    )

    report = build_report(result.outcomes, args.warm_threshold_ms, smoke=args.smoke)
    report["prewarm_requests"] = len(result.prewarm_outcomes)
    report["aborted_on_shed"] = result.aborted_on_shed

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_human(report))
        if result.aborted_on_shed:
            print(
                "\nABORTED: a shed/degraded response was observed mid-burst. "
                "Report above reflects only the requests fired before the abort.",
                file=sys.stderr,
            )

    if result.aborted_on_shed:
        sys.exit(2)


if __name__ == "__main__":
    main()
