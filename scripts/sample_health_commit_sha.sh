#!/usr/bin/env bash
#
# sample_health_commit_sha.sh — best-effort read of /health's commit_sha.
#
# Why this exists: the LML#1231 reconciliation path (scripts/reconcile_deploy_via_health.sh)
# must not judge a `railway up` failure as "recovered" just because the expected SHA happens to
# be live at /health -- that passes trivially on a re-run where the SHA was already serving from
# an earlier, unrelated deploy. The check that closes that hole needs a *pre-upload* baseline:
# what commit_sha did /health report right before `railway up` ran? This script takes that one
# reading, once, from a CI step that runs before the Railway CLI.
#
# It is deliberately never allowed to fail its calling step: an unreachable or malformed /health
# here is not a bug in this script, it is simply "no usable baseline this time" -- and it is the
# reconciliation script's job to decide what that means for the deploy job's outcome (see its own
# header: an unreadable baseline makes recovery unprovable, so it fails closed). Every failure
# path below prints the sentinel `unreachable` and exits 0; the sole exception is a missing URL
# argument, which is a call-site programming error rather than a runtime condition.
#
# Usage:
#   sample_health_commit_sha.sh <health-url>
#
# Prints to stdout: the commit_sha string, or the literal `unreachable`.
#
# Exit codes:
#   0  always, except for bad usage
#   2  bad usage
#
# Environment:
#   HEALTH_SAMPLE_CURL_TIMEOUT_SECONDS  curl --max-time budget in seconds (default 10)

# No -e: every failure path here is handled explicitly and must still print the sentinel and
# exit 0, so letting a failed command kill the script outright would defeat the point.
set -uo pipefail

URL="${1:-}"

if [[ -z "$URL" ]]; then
  echo "usage: $(basename "$0") <health-url>" >&2
  exit 2
fi

TIMEOUT_SECONDS="${HEALTH_SAMPLE_CURL_TIMEOUT_SECONDS:-10}"

unreachable() {
  echo "unreachable"
  exit 0
}

body="$(curl -sf --max-time "$TIMEOUT_SECONDS" "$URL" 2>/dev/null)" || unreachable

sha="$(printf '%s' "$body" | jq -r '.commit_sha // empty' 2>/dev/null)" || unreachable

if [[ -z "$sha" || "$sha" == "null" ]]; then
  unreachable
fi

echo "$sha"
