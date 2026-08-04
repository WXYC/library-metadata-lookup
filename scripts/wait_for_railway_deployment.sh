#!/usr/bin/env bash
#
# wait_for_railway_deployment.sh — block until a Railway deployment reaches a terminal state.
#
# Why this exists: `railway up` streams build logs, and the CLI hard-exits 1 when that stream
# drops -- but only in "CI mode", which is auto-enabled by the $CI variable that every GitHub
# Actions runner sets. A transient log-transport hiccup ("Failed to stream build logs: Failed to
# retrieve build log") therefore failed the deploy job while the deployment itself went live and
# healthy. See railwayapp/cli v4.58.0, src/commands/up.rs:240 (ci_mode) and :275-279 (exit 1).
#
# CI now deploys with `railway up --detach --json`, which returns as soon as the upload is
# accepted, never opens a log stream, and prints {"deploymentId": ..., "logsUrl": ...} on stdout
# (up.rs:230-238). This script does the waiting instead, polling the deployment API for the real
# terminal status -- which is a stronger gate than the old one: `railway up` returned when the
# *build* finished, whereas SUCCESS here means Railway's healthcheck passed and the revision is
# actually serving.
#
# Usage:
#   wait_for_railway_deployment.sh <service> <deployment-id> [logs-url]
#
# Exit codes:
#   0  deployment reached SUCCESS, or was superseded by a newer deployment
#   1  deployment reached FAILED/CRASHED, timed out, or polling kept erroring
#   2  bad usage
#
# Environment:
#   RAILWAY_TOKEN                         required -- project-scoped deploy token
#   RAILWAY_DEPLOY_TIMEOUT_SECONDS        total budget in seconds (default 900)
#   RAILWAY_DEPLOY_POLL_INTERVAL_SECONDS  gap between polls in seconds (default 10)
#   RAILWAY_DEPLOY_MAX_POLL_ERRORS        consecutive API errors tolerated (default 10)

set -euo pipefail

SERVICE="${1:-}"
DEPLOYMENT_ID="${2:-}"
LOGS_URL="${3:-}"

if [[ -z "$SERVICE" || -z "$DEPLOYMENT_ID" ]]; then
  echo "usage: $(basename "$0") <service> <deployment-id> [logs-url]" >&2
  exit 2
fi

TIMEOUT_SECONDS="${RAILWAY_DEPLOY_TIMEOUT_SECONDS:-900}"
POLL_INTERVAL_SECONDS="${RAILWAY_DEPLOY_POLL_INTERVAL_SECONDS:-10}"
MAX_POLL_ERRORS="${RAILWAY_DEPLOY_MAX_POLL_ERRORS:-10}"

# Timestamps here are UTC on purpose: these lines interleave with GitHub Actions' own UTC log
# timestamps, and the deployment `createdAt` values from the Railway API are UTC too.
log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

stderr_file="$(mktemp)"
cleanup() { rm -f "$stderr_file"; }
trap cleanup EXIT

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
consecutive_errors=0
last_status=""

log "Waiting for Railway deployment ${DEPLOYMENT_ID} (service: ${SERVICE})"
log "Timeout ${TIMEOUT_SECONDS}s, polling every ${POLL_INTERVAL_SECONDS}s"
if [[ -n "$LOGS_URL" ]]; then
  log "Build logs: ${LOGS_URL}"
fi

while :; do
  status=""
  if payload="$(railway deployment list --service "$SERVICE" --limit 50 --json 2>"$stderr_file")"; then
    consecutive_errors=0
    # `.[0].status // "UNKNOWN"` also covers the brief window right after upload where the
    # deployment has not yet appeared in the list.
    status="$(printf '%s' "$payload" | jq -r --arg id "$DEPLOYMENT_ID" \
      'map(select(.id == $id)) | .[0].status // "UNKNOWN"')"
  else
    consecutive_errors=$(( consecutive_errors + 1 ))
    log "WARN: 'railway deployment list' failed (${consecutive_errors}/${MAX_POLL_ERRORS}): $(tr '\n' ' ' <"$stderr_file")"
    if (( consecutive_errors >= MAX_POLL_ERRORS )); then
      log "ERROR: giving up after ${consecutive_errors} consecutive polling failures"
      exit 1
    fi
  fi

  if [[ -n "$status" && "$status" != "$last_status" ]]; then
    log "status: ${status}"
    last_status="$status"
  fi

  case "$status" in
    SUCCESS)
      log "Deployment ${DEPLOYMENT_ID} is live."
      exit 0
      ;;
    FAILED | CRASHED)
      log "ERROR: deployment ${DEPLOYMENT_ID} reported ${status}"
      if [[ -n "$LOGS_URL" ]]; then
        log "Inspect the build at ${LOGS_URL}"
      fi
      exit 1
      ;;
    SKIPPED | REMOVED)
      # A newer deployment superseded this one -- normally a second push to the same branch while
      # this run was still deploying. That newer run gates its own deploy, so failing here would
      # just be the false red this script exists to remove.
      log "Deployment ${DEPLOYMENT_ID} was ${status} (superseded by a newer deployment); not failing."
      exit 0
      ;;
  esac

  if (( $(date +%s) >= deadline )); then
    log "ERROR: timed out after ${TIMEOUT_SECONDS}s (last status: ${last_status:-none})"
    if [[ -n "$LOGS_URL" ]]; then
      log "Inspect the build at ${LOGS_URL}"
    fi
    exit 1
  fi

  sleep "$POLL_INTERVAL_SECONDS"
done
