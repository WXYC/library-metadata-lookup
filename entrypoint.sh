#!/bin/sh
# Drop to non-root user and start the application
exec su -s /bin/sh appuser -c "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"
