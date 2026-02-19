#!/bin/sh
# Fix volume mount permissions (Railway mounts volumes as root)
chown -R appuser:appuser /data 2>/dev/null || true

# Drop to non-root user and start the application
exec su -s /bin/sh appuser -c "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"
