FROM python:3.12-slim

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -s /sbin/nologin appuser

WORKDIR /app

# Install dependencies. wxyc-etl (like every other dependency) is pinned in
# pyproject.toml (`wxyc-etl>=0.8.0,<0.9.0`) and ships prebuilt abi3 manylinux
# wheels on PyPI, so `pip install .` pulls it with no build step. This is the
# same resolution CI validates against. Do NOT reintroduce a source build of
# wxyc-etl here: compiling it from git HEAD ignored the version pin (shipping
# an untested version into the image) and paid a full Rust release compile on
# every cold build. See WXYC/library-metadata-lookup#1077.
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy application
COPY . .

# Create logs and data directories.
# /data MUST stay: since the volume-eviction cutover (LML#834) there is no Railway
# volume mounted here, so this line is the only thing creating the writable,
# appuser-owned directory the lifespan boot-fetch copies LIBRARY_DB_PATH into. Do
# not "clean it up" — without it, boot-fetch has nowhere to write library.db.
RUN mkdir -p /app/logs /data && chown -R appuser:appuser /app/logs /data

# Unbuffer Python stdout/stderr so logs reach Railway's collector in real time
# (otherwise the StreamHandler in core/logging.py is block-buffered on the pipe).
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Expose port
EXPOSE 8000

# Entrypoint drops to the non-root appuser and starts uvicorn
ENTRYPOINT ["/app/entrypoint.sh"]
