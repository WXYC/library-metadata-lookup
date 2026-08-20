"""Lookup outcome taxonomy for miss telemetry (LML#1233).

Production `lookup_completed` carried `results_count` but none of the fields
that say *why* a response was empty, so a recall regression and a Discogs
outage produced an identical zero-result series. `derive_miss_kind` collapses
five fields already on `LookupResponse` into one label so the two can be told
apart, and so an alert can watch only the slice the matching algorithm is
actually responsible for.

| value           | condition                                   | attributable? |
|-----------------|---------------------------------------------|---------------|
| `hit`           | `results` non-empty                         | --            |
| `miss_timeout`  | empty and `timeout`                         | no            |
| `miss_degraded` | empty and `degraded`                        | no            |
| `miss_clean`    | empty, not degraded, not timed out          | **yes**       |

`miss_clean` is the only algorithm-attributable outcome and the only series
the LML#1233 Layer 2 alert fires on. Precedence is
`hit` -> `timeout` -> `degraded` -> `clean`: whenever an infrastructure cause
IS signalled it outranks the residual bucket, because the alternative is
`miss_clean` absorbing sheds and the alert paging for upstream outages
instead of recall regressions.

**Known gap -- `miss_clean` is not yet a pure signal (LML#1236).** This
function can only classify what the response tells it, and `degraded` /
`timeout` are narrower than "something upstream failed". They are set by
exactly two producers:

- `timeout` -- the LML#370 hard cap / spine-deadline trip.
- `degraded` -- `state.upstream_shed`, which `core/search.py`'s single
  catch boundary sets on `BreakerOpenError` only.

An ordinary Discogs failure -- 5xx, reset connection, read timeout below the
breaker's trip threshold -- sets neither. `discogs/service.py` swallows it and
returns `None`/empty, and `lookup/orchestrator.py`'s step-2 track lookup
catches bare `Exception` into `return [], True`, which additionally means a
`BreakerOpenError` raised in step 2 never reaches step 3's boundary. All of
those land here as `miss_clean`.

The saturation breaker limits the blast radius -- a sustained outage trips it,
and from then on the sheds *are* marked -- but the pre-trip window and
sub-threshold failures are misattributed. Propagating `degraded=True` at those
swallow sites changes the wire response, so it is tracked separately in
LML#1236 rather than smuggled in here. Until it lands, read a `miss_clean`
spike alongside the Discogs health signals (`cache.api_calls`, the LML#683
alerts) rather than as proof of a recall regression.

`search_type` is deliberately not read here. It is a lane-derived label:
`core.search.get_search_type_from_state` returns the *last strategy
attempted* with no reference to result count, so `alternative` and
`compilation` both appear on zero-result responses in production (LML#1233
Finding 1, and the same trap BS#1359 fell into). Anything keying off it to
define a miss is reading which lane ran, not what was found.

Split out of `lookup/router.py` rather than written at the emit site: that
module sits at 1,223 of its 1,250-line budget ceiling
(`tests/unit/test_module_budgets.py`), and the established response to a
router addition is a carve-out module -- the same rationale as
`lookup/endpoint_family.py` and `lookup/server_timing_legs.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lookup.models import LookupResponse

OUTCOME_HIT = "hit"
"""At least one result was returned. Not a miss, whatever else degraded."""

MISS_CLEAN = "miss_clean"
"""Empty results with the pipeline intact -- the algorithm did not find it.

The only value the LML#1233 miss-rate alert counts. Matches the contract
stated on `LookupResponse.degraded`: a genuine no-match is empty `results`
with `degraded: false` and `timeout: false`.
"""

MISS_TIMEOUT = "miss_timeout"
"""Empty results because LML's hard cap / spine deadline fired (LML#370).

Infrastructure, not recall. `_build_timed_out_response` also sets
`song_not_found=True` on this path, which is why the partial-recall series
must exclude it rather than read that field raw.
"""

MISS_DEGRADED = "miss_degraded"
"""Empty results after the enrichment tail was deliberately shed (LML#930).

Infrastructure, not recall -- a caller deadline, admission pressure, or an
unavailable upstream. Carries `degraded_reason` alongside for the split.
"""


def derive_miss_kind(response: LookupResponse) -> str:
    """Classify a completed lookup into the outcome taxonomy above.

    Args:
        response: The `LookupResponse` about to be returned to the caller.
            Read for `results`, `timeout`, and `degraded` only.

    Returns:
        One of `OUTCOME_HIT`, `MISS_TIMEOUT`, `MISS_DEGRADED`, `MISS_CLEAN`.

    The `hit` check comes first so a degraded-but-non-empty response is never
    counted as a miss: `degraded` means the tail was shed, not that the search
    failed, so the rows that did come back are trustworthy. Counting those
    would inflate the numerator every time a caller budget got tight.
    """
    if response.results:
        return OUTCOME_HIT
    if response.timeout:
        return MISS_TIMEOUT
    if response.degraded:
        return MISS_DEGRADED
    return MISS_CLEAN


def miss_telemetry_properties(
    response: LookupResponse, *, commit_sha: str | None
) -> dict[str, object]:
    """Build the LML#1233 property fragment for `lookup_completed`.

    Args:
        response: The `LookupResponse` about to be returned.
        commit_sha: The deployed SHA, bound once at the caller's import.

    Returns:
        The seven keys the miss-rate alert and its runbook read, ready to be
        splatted into the `send_to_posthog` property dict.

    Lives here rather than at the emit site because `lookup/router.py` sits
    at its module-budget ceiling and this repo's policy is to extract rather
    than raise it. Keeping the key names in one place also makes them
    greppable: they are a telemetry contract that PostHog insights filter on,
    so renaming one silently breaks an alert's `WHERE` clause.

    `commit_sha` is keyword-only and always emitted, even as `None`. A key
    present on only some deploys makes the per-commit breakdown unreadable,
    whereas a null one reads identically to absent in a PostHog filter.
    """
    return {
        "miss_kind": derive_miss_kind(response),
        "timeout": response.timeout,
        "degraded": response.degraded,
        # `.value`, not the enum itself: an insight filters on the wire
        # string. `DegradedReason` is a StrEnum so a bare pass-through happens
        # to serialize correctly today, but that is incidental.
        "degraded_reason": (response.degraded_reason.value if response.degraded_reason else None),
        "song_not_found": response.song_not_found,
        "found_on_compilation": response.found_on_compilation,
        "commit_sha": commit_sha,
    }
