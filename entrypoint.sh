#!/bin/sh
# Drop to non-root user and start the application.
#
# UVICORN_WORKERS wires horizontal scaling (LML#747). Defaulting to 1 keeps
# today's single-worker behavior unchanged until the var is set in Railway.
# Setting it >1 spawns N worker processes, parallelizing the per-request
# synchronous work across event loops and draining the single-worker starvation
# tax (plans/lookup-latency-event-loop-starvation.md §3).
#
# DO NOT set >1 before the shared-Discogs-60/min-token prereqs are in place, or N
# uncoordinated per-process limiters will over-issue and 429-trip the LML#755
# breaker: enable DISCOGS_RATE_BUCKET_ENABLED (LML#841) AND divide
# DISCOGS_RATE_LIMIT down to floor(50/N) (the bucket fails open to the local
# limiter on a PG blip), then re-size the breaker/concurrency knobs and confirm
# the discogs-cache PG connection budget + container RAM. See the horizontal-
# scaling runbook in docs/deployment.md.
raw_uvicorn_workers=${UVICORN_WORKERS:-1}
case "$raw_uvicorn_workers" in
  "" | *[!0123456789]*)
    echo "WARN: UVICORN_WORKERS must be a positive integer; got '$raw_uvicorn_workers', defaulting to 1" >&2
    uvicorn_workers=1
    ;;
  *)
    if [ "$raw_uvicorn_workers" -lt 1 ]; then
      echo "WARN: UVICORN_WORKERS must be a positive integer; got '$raw_uvicorn_workers', defaulting to 1" >&2
      uvicorn_workers=1
    else
      uvicorn_workers=$raw_uvicorn_workers
    fi
    ;;
esac

exec su -s /bin/sh appuser -c "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${uvicorn_workers}"
