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
    lml_resolve_compilation_release: bool = Field(
        default=False,
        description=(
            "When True, the artwork binding step lazily runs "
            "resolve_release_for_track() as a fallback when its floor search "
            "returns None and a song is present — binding the validated release "
            "(typically a Various-Artists compilation) without the artist floor — "
            "and trust-and-binds a release the search strategy already carried on "
            "the discogs_titles seam (skipping the re-search). When False, a "
            "floor-rejected release stays unbound and carried releases re-search "
            "exactly as before (pre-PR2 behavior). The lazy fallback honors "
            "lml_resolve_artist_canonical for the canonical-swap and fires the "
            "album-title probe, matching the live search_compilations_for_track "
            "probe. Default False; roll staging -> prod, watch the Discogs call "
            "rate. Never enabled on the /lookup/bulk path. "
            "See WXYC/library-metadata-lookup#604."
        ),
    )
    lml_resolve_nonlibrary_release: bool = Field(
        default=False,
        description=(
            "When True, the track-validating Discogs-aware strategies "
            "(TRACK_ON_COMPILATION, SONG_AS_TRACK, SWAPPED_INTERPRETATION) surface a "
            "resolvable Discogs release that is NOT in library.db as a row-less "
            "result (LibraryItem(id=0) + a real DiscogsSearchResult) instead of "
            "dropping it on the library-row gate (#536 defers track-validation "
            "until after that gate, so a non-library release is dropped before the "
            "validation that would confirm it). The release is resolved via the "
            "uncached bounded resolve_release_for_track(max_validations=5) (#633) "
            "and amortized by the #632 PG positive cache keyed on the typed artist. "
            "When False, a non-library release stays dropped (today's empty/sentinel "
            "behavior). Mirrors lml_resolve_compilation_release; default off, roll "
            "staging -> prod and watch the Discogs call rate. "
            "See WXYC/library-metadata-lookup#628."
        ),
    )
    # Persistent streaming-URL cache flags (LML#573). A service is persisted
    # only when BOTH the master kill switch AND its per-service flag are true
    # (AND-gate). The master defaults True and the per-service flags default
    # False, so the feature is OFF until Railway sets a per-service flag —
    # matching the off-by-default posture of the LML#571 apple flag this
    # replaces. The master is the single-flip kill switch.
    lml_persist_streaming_urls: bool = Field(
        default=True,
        description=(
            "Master kill switch for the persistent streaming-URL cache "
            "post-process in /api/v1/lookup. When False, the whole post-process "
            "short-circuits regardless of the per-service flags. Default True; "
            "flip False in Railway to disable Apple + Spotify persistence in one "
            "move without a re-deploy. See WXYC/library-metadata-lookup#573."
        ),
    )
    lml_persist_streaming_url_apple_music: bool = Field(
        default=False,
        description=(
            "Per-service flag for Apple Music in the streaming-URL cache "
            "post-process. A service persists only when this AND "
            "LML_PERSIST_STREAMING_URLS are both True. Default False; Railway "
            "supplies True. Replaces the LML#571 LML_PERSIST_APPLE_MUSIC_URL "
            "(renamed at deploy, no alias). See WXYC/library-metadata-lookup#573."
        ),
    )
    lml_persist_streaming_url_spotify: bool = Field(
        default=False,
        description=(
            "Per-service flag for Spotify in the streaming-URL cache "
            "post-process. A service persists only when this AND "
            "LML_PERSIST_STREAMING_URLS are both True. Default False; Railway "
            "supplies True. New in PR-1. See WXYC/library-metadata-lookup#573."
        ),
    )
    lml_persist_streaming_url_bandcamp: bool = Field(
        default=False,
        description=(
            "Per-service flag for Bandcamp in the streaming-URL cache "
            "post-process. A service persists only when this AND "
            "LML_PERSIST_STREAMING_URLS are both True. Default False; Railway "
            "supplies True. New in PR-3. See WXYC/library-metadata-lookup#573."
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
