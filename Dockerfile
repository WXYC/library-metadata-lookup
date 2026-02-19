FROM python:3.12-slim

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -s /sbin/nologin appuser

WORKDIR /app

# Install dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy application
COPY . .

# Create logs and data directories
RUN mkdir -p /app/logs /data && chown -R appuser:appuser /app/logs /data

# Expose port
EXPOSE 8000

# Entrypoint fixes volume permissions then drops to non-root user
ENTRYPOINT ["/app/entrypoint.sh"]
