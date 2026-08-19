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
#   RECONCILE_HEALTH_POLL_INTERVAL_SECONDS  gap between polls in seconds (default 10)
#   RECONCILE_HEALTH_CURL_TIMEOUT_SECONDS   curl --max-time per poll, in seconds (default 10)

set -uo pipefail

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
POLL_INTERVAL_SECONDS="${RECONCILE_HEALTH_POLL_INTERVAL_SECONDS:-10}"
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

# Reads /health once. Echoes the commit_sha on success; returns non-zero (and echoes nothing) on
# any failure -- network error, an unexpected HTTP status, unparsable body, or a missing/null
# commit_sha field. Mirrors sample_health_commit_sha.sh's own handling, including its reason for
# not using `curl -f` (a 503 from a degraded service still carries the real deploy identity, and
# what is being read here is identity, not health -- see that script's comment). Kept separate
# because the contracts differ: that script must never fail and prints a sentinel instead, while
# this loop needs to distinguish a failed poll (retryable) from a successful one, which a sentinel
# string would blur.
read_health_commit_sha() {
  local response http_code body sha
  response="$(curl -s -w '\n%{http_code}' --max-time "$CURL_TIMEOUT_SECONDS" "$HEALTH_URL" \
    2>/dev/null)" || return 1
  http_code="${response##*$'\n'}"
  body="${response%$'\n'*}"
  case "$http_code" in
    200 | 503) ;;
    *) return 1 ;;
  esac
  sha="$(printf '%s' "$body" | jq -r '.commit_sha // empty' 2>/dev/null)" || return 1
  if [[ -z "$sha" || "$sha" == "null" ]]; then
    return 1
  fi
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
