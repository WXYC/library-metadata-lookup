#!/usr/bin/env bash
#
# reconcile_deploy_via_health.sh — judge a `railway up` failure that produced no deployment id.
#
# Why this exists (LML#1231): `scripts/wait_for_railway_deployment.sh` polls the Railway
# deployment API for a real terminal status, closing the gap where a dropped build-log stream
# used to hard-exit the CLI on a deploy that actually went live. But that script needs a
# deployment id, and there is one failure mode upstream of where it can reach: if the `railway
# up` HTTP request itself fails (a client-side network timeout on the upload), the CLI never
# prints a deployment id at all. The CI job then has nothing to wait on -- the wait step is
# skipped, and the job goes red even when Railway accepted the upload and the revision went live
# a few minutes later. Observed 2026-08-18: staging was serving the new commit ~4 minutes after
# a `railway up` step failed with "operation timed out".
#
# Why /health is not a second, weaker gate than the deployment API. Railway gates traffic-
# switching on `healthcheckPath`, which railway.toml sets to /health. The public domain therefore
# only routes to a revision that has ALREADY passed Railway's healthcheck -- which is exactly
# wait_for_railway_deployment.sh's SUCCESS predicate. Observing the expected SHA at the public
# /health thus implies that predicate held; the two paths converge on the same underlying gate
# rather than substituting a softer one for it. (It is also why accepting a 503 is sound: a public
# 503 carrying the NEW sha is a revision that passed its healthcheck and degraded afterwards --
# still a landed deploy.) That chain depends on railway.toml keeping healthcheckPath = "/health",
# so a test pins it; repoint it and this gate silently weakens to "something answered with our
# SHA", with nothing else failing.
#
# This script is the reconciliation path for exactly that case: it runs only when `railway up`
# failed without producing a deployment id, and it decides whether the deploy actually landed by
# polling GET /health for a transition to the expected commit_sha.
#
# The trap this script is built around: a naive "is the expected SHA live at /health?" check
# passes trivially on any re-run where that SHA is *already* serving from an earlier, unrelated
# successful deploy -- which would turn a genuinely failed upload green. So this script requires
# a genuine TRANSITION, not just a match. The caller must sample /health's commit_sha *before*
# running `railway up` (scripts/sample_health_commit_sha.sh does that sampling) and pass it in as
# <pre-upload-sha>. Recovery is reported ONLY when that pre-upload reading differed from the
# expected SHA and a later poll observes it become the expected SHA. Two "cannot possibly prove
# recovery" cases are rejected immediately, before any polling: the pre-upload SHA already
# matched the expected one (the trap itself -- a same-SHA re-run must never pass), and the
# pre-upload SHA could not be sampled at all (sample_health_commit_sha.sh's own `unreachable`
# sentinel) -- with no known starting point, "it matches now" is unfalsifiable, not evidence.
#
# Every other failure path -- /health unreachable throughout the window, or a baseline that never
# transitions before the timeout -- also reports failure. A red deploy that should be red is far
# cheaper than a green one that isn't; this script only ever moves the needle toward green when
# it has direct, positive evidence of the specific transition this CI run caused.
#
# Usage:
#   reconcile_deploy_via_health.sh <health-url> <expected-sha> <pre-upload-sha>
#
# Exit codes:
#   0  recovered -- /health transitioned from a different SHA to <expected-sha> during this run
#   1  not recovered -- inconclusive baseline (already-matching, `unreachable`, or empty), timed
#      out, or /health never became readable
#   2  bad usage -- fewer than three arguments, or an empty <health-url> / <expected-sha>. An
#      empty <pre-upload-sha> is NOT usage: it is a runtime condition, and exits 1. See below.
#
# Environment:
#   RECONCILE_HEALTH_TIMEOUT_SECONDS        total poll budget in seconds (default 900, matching
#                                           wait_for_railway_deployment.sh -- see below)
#   RECONCILE_HEALTH_POLL_INTERVAL_SECONDS  gap between polls in seconds (default 30 -- each
#                                           poll costs a live Discogs call, see below)
#   RECONCILE_HEALTH_CURL_TIMEOUT_SECONDS   curl --max-time per poll, in seconds (default 10)

set -uo pipefail

# Resolve siblings relative to this script, not the caller's cwd: CI invokes it as
# ./scripts/reconcile_deploy_via_health.sh from the repo root, tests by absolute path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HEALTH_URL="${1:-}"
EXPECTED_SHA="${2:-}"
PRE_UPLOAD_SHA="${3:-}"

# Note which operand is checked here and which is not. The URL and the expected SHA are literals
# at the call site, so an empty one is a broken workflow -- a programming error, exit 2. The
# pre-upload SHA is different: it is produced at run time by another step, and that step
# (`echo "commit_sha=$(...)" >> "$GITHUB_OUTPUT"`) always exits 0, so it can legitimately hand us
# an empty string -- the sampler missing from the checkout, or exiting on its own usage error.
# That is a runtime condition meaning exactly what the `unreachable` sentinel means (no baseline,
# so no provable transition), and it is handled as such below. Calling it a usage error would send
# whoever reads the log hunting for a malformed workflow step instead of an unreadable /health.
if [[ $# -lt 3 || -z "$HEALTH_URL" || -z "$EXPECTED_SHA" ]]; then
  echo "usage: $(basename "$0") <health-url> <expected-sha> <pre-upload-sha>" >&2
  exit 2
fi

# 900 to match RAILWAY_DEPLOY_TIMEOUT_SECONDS in wait_for_railway_deployment.sh: both are waiting
# on the same physical event -- a revision finishing its build and starting to serve -- so they
# should agree about how long that may legitimately take. If anything this one needs the budget
# more: the wait script starts its clock once the upload has already been accepted, whereas this
# one starts from a client-side upload failure and must still cover a build that had not been
# queued yet. Under-budgeting here doesn't fail safe, it just relocates the bug: the timeout would
# be reported as "the upload never landed" on a deploy that landed a minute later.
TIMEOUT_SECONDS="${RECONCILE_HEALTH_TIMEOUT_SECONDS:-900}"
# 30, not the wait script's 10. That script polls Railway's API, which is free to us; every hit
# HERE lands on our own /health, and routers/health.py's discogs_api probe makes a live
# GET /oauth/identity per request on the raw client, outside LML's own rate gate. At 10s a full
# 900s window would spend ~90 live Discogs calls out of a 60/min budget that staging and
# production share -- during an incident, which is the worst moment to spend it. 30s costs at
# most 20s of extra detection latency on a 900s budget and cuts that to ~30 calls.
POLL_INTERVAL_SECONDS="${RECONCILE_HEALTH_POLL_INTERVAL_SECONDS:-30}"
CURL_TIMEOUT_SECONDS="${RECONCILE_HEALTH_CURL_TIMEOUT_SECONDS:-10}"

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

# The sentinel scripts/sample_health_commit_sha.sh prints when it could not read /health at all.
UNREACHABLE_SENTINEL="unreachable"

log "Reconciling a railway-up failure that produced no deployment id (LML#1231)."
log "Expected SHA ${EXPECTED_SHA}, pre-upload /health reading was '${PRE_UPLOAD_SHA}'."

# An empty baseline means the same thing as the sentinel and is handled identically -- see the
# usage check above for why it reaches this branch rather than being rejected as a usage error.
if [[ -z "$PRE_UPLOAD_SHA" || "$PRE_UPLOAD_SHA" == "$UNREACHABLE_SENTINEL" ]]; then
  log "INCONCLUSIVE: /health could not be sampled before the upload attempt; a transition cannot be proven, so this stays a failure."
  exit 1
fi

if [[ "$PRE_UPLOAD_SHA" == "$EXPECTED_SHA" ]]; then
  log "INCONCLUSIVE: /health already reported ${EXPECTED_SHA} before this deploy attempt was made. A same-SHA reading afterward would prove nothing about whether THIS upload landed, so this stays a failure rather than a trivial pass."
  exit 1
fi

# Reads /health once by delegating to sample_health_commit_sha.sh -- the SAME reader the pre-upload
# baseline came from, deliberately, so the two readings this script compares can never be produced
# by two subtly different rules. That matters more than the line count: the "accept 200 and 503,
# never `curl -f`" policy and the whitespace/null rejection are a contract *between* the baseline
# and the poll, and a second copy of it here would be a contract with itself. An earlier revision
# did keep a copy, and it had already drifted -- the copy was missing the whitespace rejection.
#
# The two callers want different things from a failed read, which is the only reason a wrapper is
# needed at all: the sampler's contract is "never fail, print the sentinel", while this loop needs
# a retryable non-zero. Converting one to the other is the two lines below.
read_health_commit_sha() {
  local sha
  sha="$(HEALTH_SAMPLE_CURL_TIMEOUT_SECONDS="$CURL_TIMEOUT_SECONDS" \
    bash "$SCRIPT_DIR/sample_health_commit_sha.sh" "$HEALTH_URL")" || return 1
  [[ -n "$sha" && "$sha" != "$UNREACHABLE_SENTINEL" ]] || return 1
  printf '%s' "$sha"
}

log "Polling ${HEALTH_URL} for up to ${TIMEOUT_SECONDS}s (every ${POLL_INTERVAL_SECONDS}s), watching for a transition to ${EXPECTED_SHA}."

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
last_seen=""

while :; do
  if sha="$(read_health_commit_sha)"; then
    if [[ "$sha" != "$last_seen" ]]; then
      log "commit_sha: ${sha}"
      last_seen="$sha"
    fi
    if [[ "$sha" == "$EXPECTED_SHA" ]]; then
      log "RECOVERED: /health now reports ${EXPECTED_SHA} (was '${PRE_UPLOAD_SHA}' before the upload attempt). The upload landed despite the client-side failure."
      exit 0
    fi
  else
    log "WARN: /health unreachable or unparsable this poll"
  fi

  if (( $(date +%s) >= deadline )); then
    log "FAILED: timed out after ${TIMEOUT_SECONDS}s without observing ${EXPECTED_SHA} at ${HEALTH_URL} (last seen: ${last_seen:-none})"
    exit 1
  fi

  sleep "$POLL_INTERVAL_SECONDS"
done
