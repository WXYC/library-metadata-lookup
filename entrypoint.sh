#!/bin/sh
# Fix volume mount permissions (Railway mounts volumes as root)
chown -R appuser:appuser /data 2>/dev/null || true

# Drop to non-root user and start the application.
#
# LML#706: worker count is env-tunable (UVICORN_WORKERS, default 1). A single
# uvicorn worker means one event loop services every cold /lookup, so under
# concurrency the loop starves and even trivial spans inflate to seconds.
# Raising UVICORN_WORKERS on Railway gives the replica N loops (each with its
# own asyncpg pools) without a redeploy. Default 1 preserves historical
# single-loop behavior. Mind the memory + connection budget when raising it:
# total discogs-cache connections = UVICORN_WORKERS x LML_DISCOGS_POOL_MAX_SIZE.
exec su -s /bin/sh appuser -c "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${UVICORN_WORKERS:-1}"
