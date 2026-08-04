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
# terminal status -- which is at least as strong a gate as the old one, and usually stronger:
# `railway up` raced a build-log stream against a deploy-status subscription and normally
# returned when the *build* finished, whereas SUCCESS here means Railway's healthcheck passed
# (the services set `healthcheckPath` in railway.toml) and the revision is actually serving.
#
# Every failure mode here is designed to fail *closed* and to say why. A transient error must
# never fail a healthy deploy -- that is the bug this script exists to remove -- but a deployment
# that genuinely did not land must never report success either.
#
# Usage:
#   wait_for_railway_deployment.sh <service> <deployment-id> [logs-url]
#
# Exit codes:
#   0  deployment reached SUCCESS/SLEEPING, or was verifiably superseded by a newer deployment
#   1  deployment reached FAILED/CRASHED/NEEDS_APPROVAL, was removed without a successor,
#      timed out, or polling kept erroring
#   2  bad usage
#
# Environment:
#   RAILWAY_TOKEN                         required -- project-scoped deploy token
#   RAILWAY_DEPLOY_TIMEOUT_SECONDS        total budget in seconds (default 900)
#   RAILWAY_DEPLOY_POLL_INTERVAL_SECONDS  gap between polls in seconds (default 10)
#   RAILWAY_DEPLOY_MAX_POLL_ERRORS        consecutive API errors tolerated (default 10)
#   RAILWAY_DEPLOY_BUILD_LOG_LINES        build-log lines to dump on failure (default 100)

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
BUILD_LOG_LINES="${RAILWAY_DEPLOY_BUILD_LOG_LINES:-100}"

# Timestamps here are UTC on purpose: these lines interleave with GitHub Actions' own UTC log
# timestamps, and the deployment `createdAt` values from the Railway API are UTC too.
log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

show_logs_url() {
  if [[ -n "$LOGS_URL" ]]; then
    log "Inspect the build at ${LOGS_URL}"
  fi
}

# On a genuinely broken build the Actions log would otherwise contain no build output at all,
# just a dashboard link. Upstream's own automation guidance (up.rs:28) pairs `deployment list`
# polling with exactly this call. Best-effort: never let a log-fetch failure mask the real error.
dump_build_logs() {
  log "--- last ${BUILD_LOG_LINES} lines of build log ---"
  if ! railway logs --build --lines "$BUILD_LOG_LINES" "$DEPLOYMENT_ID" 2>&1; then
    log "WARN: could not fetch build logs; see ${LOGS_URL:-the Railway dashboard}"
  fi
  log "--- end of build log ---"
}

stderr_file="$(mktemp)"
cleanup() { rm -f "$stderr_file"; }
trap cleanup EXIT INT TERM

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
consecutive_errors=0
last_status=""

log "Waiting for Railway deployment ${DEPLOYMENT_ID} (service: ${SERVICE})"
log "Timeout ${TIMEOUT_SECONDS}s, polling every ${POLL_INTERVAL_SECONDS}s"
show_logs_url

while :; do
  status=""
  payload=""
  poll_error=""
  # An explicit flag, not `[[ -n "$poll_error" ]]`: a non-zero `railway` exit with nothing on
  # stderr would otherwise leave the message empty, go uncounted, and loop silently to timeout.
  poll_failed=0

  if payload="$(railway deployment list --service "$SERVICE" --limit 50 --json 2>"$stderr_file")"; then
    # jq must not be allowed to kill the script: a zero-exit-but-unparseable response (a CLI
    # warning on stdout, a truncated body, a proxy error page) is exactly the transient blip
    # this script is supposed to absorb, so it counts against MAX_POLL_ERRORS like any other.
    # `.[0].status // "UNKNOWN"` also covers the window right after upload where the deployment
    # has not yet appeared in the list.
    if ! status="$(printf '%s' "$payload" | jq -r --arg id "$DEPLOYMENT_ID" \
      'map(select(.id == $id)) | .[0].status // "UNKNOWN"' 2>"$stderr_file")"; then
      poll_failed=1
      poll_error="unparseable 'deployment list' output: $(tr '\n' ' ' <"$stderr_file")"
    elif [[ -z "$status" ]]; then
      poll_failed=1
      poll_error="empty 'deployment list' output"
    fi
  else
    poll_failed=1
    poll_error="$(tr '\n' ' ' <"$stderr_file")"
    if [[ -z "${poll_error// /}" ]]; then
      poll_error="'railway deployment list' exited non-zero with no stderr"
    fi
  fi

  if (( poll_failed )); then
    status=""
    consecutive_errors=$(( consecutive_errors + 1 ))
    log "WARN: poll failed (${consecutive_errors}/${MAX_POLL_ERRORS}): ${poll_error}"
    if (( consecutive_errors >= MAX_POLL_ERRORS )); then
      log "ERROR: giving up after ${consecutive_errors} consecutive polling failures"
      show_logs_url
      exit 1
    fi
  else
    consecutive_errors=0
  fi

  if [[ -n "$status" && "$status" != "$last_status" ]]; then
    log "status: ${status}"
    last_status="$status"
  fi

  case "$status" in
    "")
      : # poll error, already logged and counted
      ;;
    SUCCESS | SLEEPING)
      log "Deployment ${DEPLOYMENT_ID} is live (${status})."
      exit 0
      ;;
    FAILED | CRASHED)
      log "ERROR: deployment ${DEPLOYMENT_ID} reported ${status}"
      dump_build_logs
      show_logs_url
      exit 1
      ;;
    NEEDS_APPROVAL)
      # Terminal until a human acts, so waiting out the full budget only delays the signal.
      log "ERROR: deployment ${DEPLOYMENT_ID} is awaiting manual approval in the Railway dashboard."
      show_logs_url
      exit 1
      ;;
    SKIPPED | REMOVED | REMOVING)
      # Normally a newer push superseded this deployment: that run gates its own deploy, so
      # failing here would just be the false red this script exists to remove. But "superseded"
      # is a claim worth checking -- an operator rolling back mid-run produces the same status
      # with no successor, and exiting 0 there would report success for a revision that never
      # went live. createdAt is ISO-8601 UTC, so lexicographic comparison is chronological.
      newer=""
      if ! newer="$(printf '%s' "$payload" | jq -r --arg id "$DEPLOYMENT_ID" \
        '(map(select(.id == $id)) | .[0].createdAt) as $ours
         | map(select(.createdAt > $ours)) | length' 2>/dev/null)"; then
        newer=""
      fi
      if [[ "$newer" =~ ^[0-9]+$ ]] && (( newer > 0 )); then
        log "Deployment ${DEPLOYMENT_ID} was ${status}, superseded by ${newer} newer deployment(s); not failing."
        exit 0
      fi
      log "ERROR: deployment ${DEPLOYMENT_ID} was ${status} with no newer deployment to supersede it."
      log "That usually means it was rolled back or removed by hand, not replaced by a newer push."
      show_logs_url
      exit 1
      ;;
    BUILDING | DEPLOYING | INITIALIZING | QUEUED | WAITING | UNKNOWN)
      : # not terminal, keep polling
      ;;
    *)
      # The CLI Debug-formats the status enum (deployment.rs:167), so a value Railway adds after
      # this CLI pin arrives as Other("NEW_STATUS"). Keep waiting, but say so rather than letting
      # it look like a silent hang.
      log "WARN: unrecognized deployment status '${status}'; continuing to wait."
      ;;
  esac

  if (( $(date +%s) >= deadline )); then
    log "ERROR: timed out after ${TIMEOUT_SECONDS}s (last status: ${last_status:-none})"
    show_logs_url
    exit 1
  fi

  sleep "$POLL_INTERVAL_SECONDS"
done
