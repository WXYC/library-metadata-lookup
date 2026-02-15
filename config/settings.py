"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Keys - Optional
    discogs_token: str | None = Field(None, description="Discogs API token for artwork lookup")

    # Database Configuration
    library_db_path: Path = Field(
        default=Path("library.db"), description="Path to SQLite library database"
    )

    @property
    def resolved_library_db_path(self) -> Path:
        """Get the library database path, handling empty env var case."""
        if not str(self.library_db_path) or str(self.library_db_path) == ".":
            return Path("library.db")
        return self.library_db_path

    # Application Configuration
    host: str = Field(default="0.0.0.0", description="Host to bind the server to")
    port: int = Field(default=8000, description="Port to run the server on")
    log_level: str = Field(default="INFO", description="Logging level")

    # Feature Flags
    enable_artwork_lookup: bool = Field(
        default=True, description="Enable artwork lookup from external APIs"
    )
    enable_telemetry: bool = Field(default=True, description="Enable PostHog telemetry")

    # PostHog Configuration
    posthog_api_key: str | None = Field(None, description="PostHog API key for telemetry")
    posthog_host: str = Field(default="https://us.i.posthog.com", description="PostHog host URL")

    # Sentry Configuration
    sentry_dsn: str | None = Field(None, description="Sentry DSN for error tracking")

    # Discogs Cache Database Configuration
    database_url_discogs: str | None = Field(
        None,
        description="PostgreSQL connection URL for Discogs cache",
    )

    # Discogs Cache Configuration
    discogs_track_cache_ttl: int = Field(
        default=3600, description="TTL in seconds for Discogs track cache (default: 1 hour)"
    )
    discogs_release_cache_ttl: int = Field(
        default=14400, description="TTL in seconds for Discogs release cache (default: 4 hours)"
    )
    discogs_search_cache_ttl: int = Field(
        default=3600, description="TTL in seconds for Discogs search cache (default: 1 hour)"
    )
    discogs_artist_cache_ttl: int = Field(
        default=86400, description="TTL in seconds for Discogs artist cache (default: 24 hours)"
    )
    discogs_label_cache_ttl: int = Field(
        default=86400, description="TTL in seconds for Discogs label cache (default: 24 hours)"
    )
    discogs_cache_maxsize: int = Field(
        default=1000, description="Maximum entries in Discogs caches"
    )

    # Discogs Rate Limiting Configuration
    discogs_rate_limit: int = Field(
        default=50, description="Max Discogs API requests per minute (stay under 60/min limit)"
    )
    discogs_max_concurrent: int = Field(
        default=5, description="Max concurrent Discogs API requests"
    )
    discogs_max_retries: int = Field(
        default=2, description="Max retry attempts on 429 rate limit errors"
    )

    # Application Metadata
    app_name: str = Field(default="Library-Metadata-Lookup", description="Application name")
    app_version: str = Field(default="0.1.0", description="Application version")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
