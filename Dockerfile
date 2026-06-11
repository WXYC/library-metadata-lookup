FROM python:3.12-slim AS builder

# Install Rust toolchain and build wxyc-etl wheel
RUN apt-get update && apt-get install -y --no-install-recommends curl git gcc libc6-dev && \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
    . "$HOME/.cargo/env" && \
    pip install --no-cache-dir maturin && \
    git clone --depth 1 https://github.com/WXYC/wxyc-etl.git /tmp/wxyc-etl && \
    cd /tmp/wxyc-etl/wxyc-etl-python && \
    maturin build --release && \
    cp /tmp/wxyc-etl/target/wheels/*.whl /tmp/

FROM python:3.12-slim

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -s /sbin/nologin appuser

WORKDIR /app

# Install wxyc-etl wheel from builder stage
COPY --from=builder /tmp/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Install dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy application
COPY . .

# Create logs and data directories
RUN mkdir -p /app/logs /data && chown -R appuser:appuser /app/logs /data

# Unbuffer Python stdout/stderr so logs reach Railway's collector in real time
# (otherwise the StreamHandler in core/logging.py is block-buffered on the pipe).
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Expose port
EXPOSE 8000

# Entrypoint fixes volume permissions then drops to non-root user
ENTRYPOINT ["/app/entrypoint.sh"]
