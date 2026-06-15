"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Keys - Optional (Discogs: token OR key+secret, token takes precedence)
    discogs_token: str | None = Field(None, description="Discogs personal access token")
    discogs_api_key: str | None = Field(None, description="Discogs OAuth consumer key")
    discogs_api_secret: str | None = Field(None, description="Discogs OAuth consumer secret")
    spotify_client_id: str | None = Field(
        None, description="Spotify client ID for streaming availability checks"
    )
    spotify_client_secret: str | None = Field(
        None, description="Spotify client secret for streaming availability checks"
    )

    # Apple Music API authentication (replaces unauthenticated iTunes Search after the
    # 2026-05-28 Railway egress 403; see docs/adr/0001-authenticated-apple-music-api.md).
    # All three must be set for the client to function; absent any one of them the
    # provider returns None and Apple Music checks degrade to no-op (same shape as
    # Spotify-without-creds).
    apple_music_team_id: str | None = Field(
        None, description="Apple Developer Team ID (10-char) used as the JWT `iss` claim"
    )
    apple_music_key_id: str | None = Field(
        None, description="Apple Developer Key ID (10-char) used as the JWT `kid` header"
    )
    apple_music_private_key: str | None = Field(
        None,
        description=(
            "ES256 PEM private key contents downloaded from the Apple Developer "
            "console. Railway stores the multi-line PEM verbatim; never check the "
            "key material into the repo."
        ),
    )

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

    # MusicBrainz Cache Database Configuration
    database_url_musicbrainz: str | None = Field(
        None,
        description=(
            "PostgreSQL connection URL for the musicbrainz-cache. Used by the "
            "Phase 1.5 mojibake-recovery external-cache fallback in /api/v1/lookup. "
            "Optional; when unset, the MB leg of the fallback is skipped."
        ),
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

    # Library Cache Configuration
    library_cache_ttl: int = Field(
        default=3600,
        description="TTL in seconds for library search/artist caches (default: 1 hour)",
    )
    library_cache_maxsize: int = Field(default=500, description="Maximum entries in library caches")

    # Discogs Rate Limiting Configuration
    discogs_rate_limit: int = Field(
        default=50, description="Max Discogs API requests per minute (stay under 60/min limit)"
    )
    discogs_max_concurrent: int = Field(
        default=5, description="Max concurrent Discogs API requests"
    )
    discogs_max_retries: int = Field(
        default=5,
        description=(
            "Max retry attempts on 429 rate limit errors. With jittered exponential backoff "
            "capped at 60s, 5 retries spans roughly 0.5–62s of total wait — enough to ride "
            "out a typical Discogs 60s rate-limit window without throwing away in-flight work."
        ),
    )

    # Admin Configuration
    admin_token: str | None = Field(
        None, description="Bearer token for admin endpoints (e.g. library.db upload)"
    )

    # LML API Auth (tubafrenzy / Backend-Service -> LML)
    lml_api_key: str | None = Field(
        None,
        description=(
            "Bearer token required from tubafrenzy / Backend-Service callers. "
            "Compared against the Authorization header on protected endpoints."
        ),
    )
    lml_require_auth: bool = Field(
        default=False,
        description=(
            "When True, enforce LML_API_KEY on tubafrenzy/Backend-Service-facing endpoints. "
            "Default False so the dep can be deployed before consumers are updated; "
            "flip to True after all callers send the bearer header."
        ),
    )
    lml_resolve_artist_canonical: bool = Field(
        default=False,
        description=(
            "When True, swap inbound artist names for the canonical Discogs form "
            "when trigram similarity >= CANONICAL_ARTIST_SIMILARITY_FLOOR before "
            "search_releases_by_track probes. When False, the resolver still runs "
            "in shadow mode (logs candidate + score on every lookup; emits "
            "set_data('resolver_pre_pass', ...) on the active Sentry transaction) "
            "but does not swap. Set True in Railway after the calibration sweep "
            "confirms the chosen floor's FP-rate is <= 0.5%. "
            "See WXYC/library-metadata-lookup#318."
        ),
    )
    lml_persist_apple_music_url: bool = Field(
        default=False,
        description=(
            "When True, /api/v1/lookup post-processes every item whose "
            "apple_music_url came out null and runs an Apple Music probe using "
            "the request's (artist, album, song) — independent of the item's "
            "library row. Successful resolutions are persisted to "
            "entity.album_apple_music_lookup_cache keyed by (artist_normalized, "
            "album_normalized) so subsequent lookups hit the cache instead of "
            "Apple's API. Known misses are recorded with a 7-day TTL so unknown "
            "albums don't repeatedly hammer Apple. Off by default so the "
            "rollout is reversible without a re-deploy; flip True in Railway "
            "after one staging tick confirms the post-process is healthy. "
            "Rollback: flip False (no re-deploy required). "
            "See WXYC/library-metadata-lookup#570."
        ),
    )

    # Streaming Webhook Configuration
    streaming_webhook_urls: str | None = Field(
        None,
        description="Comma-separated URLs to POST streaming status changes after library.db upload",
    )
    etl_notify_key: str | None = Field(
        None, description="Bearer token for streaming webhook authentication"
    )

    # Application Metadata
    app_name: str = Field(default="Library-Metadata-Lookup", description="Application name")
    app_version: str = Field(default="0.1.0", description="Application version")
    environment: str = Field(
        default="development", description="Runtime environment (development, staging, production)"
    )

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
