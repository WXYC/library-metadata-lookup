"""Application configuration using Pydantic Settings."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Postgres memory-value grammar (digits + optional unit) for ``work_mem`` (LML#804).
# ``discogs_search_work_mem`` is interpolated into a ``SET LOCAL`` string (Postgres
# ``SET`` takes no bind params), so it is validated against this at BOTH ends: the
# ``_validate_work_mem`` field validator below (fail loudly at Settings load) and
# ``discogs.cache_service._build_search_bounds_sql`` (defense in depth at the
# interpolation point). ``\Z`` — not ``$`` — so a stray trailing newline can't slip
# through (``$`` matches just before a final ``\n``).
_WORK_MEM_RE = re.compile(r"^\d+(kB|MB|GB|TB)?\Z")


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

    # Object Storage (Railway Bucket) — WXYC/library-metadata-lookup#835.
    # When BOTH are set, library.db and streaming_availability.db are served from
    # an S3-compatible Railway Bucket instead of the local /data volume (the
    # volume-eviction epic #834). Both default None -> local-directory mode, so
    # dev and the endpoint test suite need no bucket. Credentials come from the
    # standard AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars that boto3 reads
    # natively (Railway's variable-reference presets provision those names).
    lml_bucket_name: str | None = Field(
        None, description="Railway Bucket name (the bucket's BUCKET var); enables bucket mode"
    )
    lml_bucket_endpoint: str | None = Field(
        None,
        description="Railway Bucket S3 endpoint URL (the bucket's ENDPOINT var); enables bucket mode",
    )

    # Constrained to the three values botocore's Config(s3={"addressing_style": ...})
    # accepts, so an out-of-range value fails loudly at Settings load rather than
    # lazily at first S3ObjectStore construction (a boot-time / first-request fault).
    lml_bucket_addressing_style: Literal["virtual", "path", "auto"] = Field(
        default="virtual",
        description=(
            "S3 addressing style for the Railway Bucket client: 'virtual' "
            "(virtual-hosted, <bucket>.<endpoint> — Railway's current default for "
            "newly created buckets), 'path' (<endpoint>/<bucket> — required only by "
            "legacy pre-change buckets, per the bucket's Credentials tab), or 'auto'. "
            "Fed verbatim to botocore's Config(s3={'addressing_style': ...}). Default "
            "'virtual' so a freshly created bucket works with no override. See "
            "WXYC/library-metadata-lookup#834."
        ),
    )

    @property
    def bucket_mode(self) -> bool:
        """Whether object storage is bucket-backed (both bucket vars set) vs local."""
        return bool(self.lml_bucket_name and self.lml_bucket_endpoint)

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

    # Discogs-cache trigram search-arm bounds (LML#804). Applied per-transaction
    # via SET LOCAL on the two trigram search arms (`search_releases`,
    # `search_releases_by_track`) so they revert at transaction end and never
    # touch the write-serving pool session.
    discogs_search_statement_timeout_ms: int = Field(
        default=10000,
        ge=1,
        description=(
            "Per-transaction statement_timeout (ms) on the discogs-cache trigram search "
            "arms. Bounds a runaway scan to a hard ceiling ABOVE the largest legitimate "
            "caller budget (Backend-Service ~5s), so it catches only true runaways, not "
            "steady-state queries. A timed-out query surfaces as asyncpg.QueryCanceledError, "
            "which the search arms map into CacheUnavailableError (degrade to cache-only, "
            "never a 500), freeing the pool slot and the /lookup in-flight cap slot at once. "
            "Applied via SET LOCAL, so it never touches the write-serving pool session. "
            "Must be >= 1. See WXYC/library-metadata-lookup#804."
        ),
    )
    discogs_search_work_mem: str = Field(
        default="128MB",
        description=(
            "Per-transaction work_mem on the discogs-cache trigram search arms, as a "
            "Postgres memory value (e.g. '128MB'). Default work_mem (4MB) makes the trigram "
            "bitmap heap scans go lossy and recheck-storm under degraded latency (~92s vs "
            "~12.7s with a large work_mem, per the 2026-05-25 pg_trgm finding). Bounded on "
            "purpose: per-connection allocations multiply by pool size on the shared 8GB "
            "Railway instance, so NOT 1GB — 128MB is a safe default; empirical prod tuning "
            "is a separate follow-up (WXYC/library-metadata-lookup#806). Applied via SET "
            "LOCAL so it never touches the write-serving pool session. See "
            "WXYC/library-metadata-lookup#804."
        ),
    )

    @field_validator("discogs_search_work_mem")
    @classmethod
    def _validate_work_mem(cls, v: str) -> str:
        """Reject a non-Postgres-memory-value ``work_mem`` at Settings load (LML#804).

        The value is interpolated into a ``SET LOCAL`` string, so an invalid one
        (e.g. ``"128mb"`` lowercase, ``"256 MB"`` with a space, ``"garbage"``)
        would otherwise fail lazily at first ``DiscogsCacheService`` construction —
        a first-request 500 that also takes down the whole Discogs service —
        rather than loudly at boot. This makes it symmetric with the ``ge=1`` on
        ``discogs_search_statement_timeout_ms`` and mirrors the interpolation-time
        guard in ``discogs.cache_service._build_search_bounds_sql``.
        """
        if not _WORK_MEM_RE.match(v):
            raise ValueError(f"work_mem {v!r} is not a valid Postgres memory value (e.g. '128MB')")
        return v

    # Library Cache Configuration
    library_cache_ttl: int = Field(
        default=3600,
        description="TTL in seconds for library search/artist caches (default: 1 hour)",
    )
    library_cache_maxsize: int = Field(default=500, description="Maximum entries in library caches")

    # Discogs Rate Limiting Configuration
    discogs_rate_limit: int = Field(
        default=50,
        ge=1,
        description=(
            "Max Discogs API requests per minute (stay under 60/min limit). Also seeds the "
            "LML#841 shared token bucket's refill (rate / 60), so it must be >= 1 — a zero "
            "would divide-by-zero the bucket's retry_after computation."
        ),
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

    # Discogs Saturation Circuit-Breaker (LML#755)
    # When the outbound Discogs path saturates under a sustained flood (the
    # 2026-07-10 flowsheet-metadata-backfill incident), the retry/backoff loop
    # converts excess load into unbounded per-request latency → BS 35s timeouts
    # → 502s. This breaker sheds the live-probe tail instead, fast-failing
    # lookups to cache-only. Per-event-loop (process-global on the single-worker
    # deployment today; re-size if UVICORN_WORKERS > 1 ever ships — LML#747).
    discogs_breaker_failure_threshold: int = Field(
        default=4,
        ge=1,
        description=(
            "Consecutive **failed requests** (not attempts) that trip the saturation breaker "
            "OPEN. Each `_request_with_retry` that exhausts its retries into 429s records ONE "
            "failure, so this counts requests, not the per-attempt 429s (which are bounded by "
            "`discogs_max_retries + 1`). A single healthy response resets the run, so only a "
            "*sustained* rate-limited window — 4 lookups in a row all failing — opens it. "
            "Must be >= 1 (a pydantic validation error is raised otherwise)."
        ),
    )
    discogs_breaker_remaining_floor: int = Field(
        default=3,
        ge=0,
        description=(
            "Proactive trip point on `X-Discogs-Ratelimit-Remaining`: when a response — 429 OR "
            "success — reports this many tokens or fewer left in the shared 60/min bucket, open "
            "the breaker before the next wave of live probes starts 429ing. Set 0 to disable the "
            "proactive floor and rely solely on observed 429s. Must be >= 0."
        ),
    )
    discogs_breaker_cooldown_seconds: float = Field(
        default=20.0,
        ge=0.0,
        description=(
            "How long the breaker stays OPEN (shedding live probes to cache-only) before "
            "admitting a single half-open trial request. Roughly a third of the Discogs 60s "
            "rate-limit window, so the bucket has partially refilled before we re-probe. "
            "Must be >= 0."
        ),
    )
    discogs_breaker_trial_watchdog_multiplier: float = Field(
        default=20.0,
        ge=0.0,
        description=(
            "LML#787 HALF_OPEN watchdog: a trial unresolved after "
            "`max(cooldown_seconds × this, 60s)` is presumed lost and the breaker re-OPENs "
            "(fresh cool-down → fresh trial) instead of latching. Size it above the worst-case "
            "legitimate trial (~360s with the default retry/backoff settings); the default "
            "(20 → 400s at the 20s cool-down) leaves headroom. The 60s floor keeps the watchdog "
            "alive even at 0. Must be >= 0."
        ),
    )

    # Shared Discogs Rate Token Bucket (LML#841)
    # The per-process `AsyncLimiter` bounds each LML process to `discogs_rate_limit`/min,
    # but prod and staging share ONE Discogs token against ONE 60/min upstream bucket, so
    # N uncoordinated limiters can collectively exceed it and 429-trip the #755 breaker
    # (reference_lml_staging_shares_prod). This flag moves the *rate* dimension to a shared
    # PG token bucket in the LML-owned `lml_cache.discogs_rate_bucket` row, so N processes
    # meter against one shared global budget instead of N uncoordinated ones (same burst
    # envelope as a single AsyncLimiter, shared globally). The per-process semaphore
    # (`discogs_max_concurrent`) and the #755
    # breaker stay local. Default OFF: enabling is a staged rollout (merge inert → staging →
    # prod), and any discogs-cache PG hiccup fails OPEN back to the local `AsyncLimiter`.
    discogs_rate_bucket_enabled: bool = Field(
        default=False,
        description=(
            "When True, draw Discogs rate permits from the shared PG token bucket "
            "(`lml_cache.discogs_rate_bucket`) instead of the per-process AsyncLimiter, so all "
            "processes/environments meter against one shared budget for the 60/min token. Fails "
            "open to the local limiter on any PG error. Default False (inert)."
        ),
    )
    discogs_rate_bucket_key: str = Field(
        default="discogs",
        description=(
            "Primary key of the shared bucket row. All LML processes sharing one Discogs token "
            "and one discogs-cache PG must use the SAME key so they draw from one budget. "
            "Override only to isolate an independent token (e.g. a second Discogs app)."
        ),
    )
    discogs_rate_bucket_timeout_s: float = Field(
        default=0.5,
        gt=0.0,
        description=(
            "Per-round-trip deadline (seconds) on a SINGLE token-bucket UPDATE before the gate "
            "fails open to the local AsyncLimiter. Bounds one PG call, NOT the queue-for-a-token "
            "wait — a slow/locked discogs-cache can never wedge the live-probe tail. Must be > 0: "
            "a zero deadline would immediately time out every acquire and silently fail the gate "
            "open on every call; to disable the bucket use `discogs_rate_bucket_enabled=false`."
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
    lml_api_key_cache_refresh_seconds: int = Field(
        default=300,
        ge=1,
        description=(
            "How often (seconds) the in-process ApiKeyCache (core/api_key_cache.py) "
            "reloads its active-key snapshot from lml_cache.api_keys (LML per-consumer "
            "API keys plan). Bounds revocation staleness: a revoked key stays valid in "
            "an already-booted process for up to this long after the revoke lands in "
            "PG. Default 300 (5 minutes), the same cadence this org already reasons in "
            "elsewhere (e.g. the wxyc-canary schedule). Must be >= 1 -- a bad value fails "
            "loudly at Settings load rather than busy-looping the refresh task, mirroring "
            "discogs_search_statement_timeout_ms's ge=1. See "
            "docs/plans/lml-per-consumer-api-keys.md."
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
            "rate. Off on /lookup/bulk by default; caller-opt-in since LML#920 "
            "via ?allow_release_resolution_fallback=true. "
            "See WXYC/library-metadata-lookup#604."
        ),
    )
    lml_resolve_nonlibrary_release: bool = Field(
        default=False,
        description=(
            "When True, five row-less producers surface a resolvable Discogs "
            "release that is NOT in library.db as a row-less result (LibraryItem("
            "id=0) + a real DiscogsSearchResult) instead of dropping it. The three "
            "track-validating strategies (TRACK_ON_COMPILATION, SONG_AS_TRACK, "
            "SWAPPED_INTERPRETATION) drop a non-library release on the library-row "
            "gate (#536 defers track-validation until after that gate); the A1 "
            "carry-through resolves it via the bounded resolve_release_for_track("
            "max_validations=5) (#633), amortized by the #632 PG positive cache "
            "keyed on the typed artist (#628). SONG_AS_ARTIST (#631) surfaces a "
            "row-less pick over the releases the artist probe already fetched, and "
            "the A4 cached-track safety net (#629) surfaces one over the Discogs-"
            "cache track confirmations (both without an extra resolve or cache "
            "write). When False, a non-library release stays dropped (today's "
            "empty/sentinel behavior). Off on /lookup/bulk by default: the "
            "per-request allow_release_resolution_fallback kill switch (False "
            "unless the caller opts in via ?allow_release_resolution_fallback=true, "
            "LML#920) gates all five producers, so a backfill item never pays the "
            "carry-through resolve, the row-less artwork bind, or a row-less surface "
            "through any of those five paths (#652) unless it explicitly asks for "
            "them. (The #583 library-miss search — typed artist+album — is a "
            "separate path from #652's five producers, but it is likewise gated "
            "on this flag via #671: off on default bulk, and when the caller "
            "opts in it surfaces a confident Discogs album match row-less via a "
            "single search, not the resolution fan-out.) "
            "Mirrors lml_resolve_compilation_release; default off, roll staging -> "
            "prod and watch the Discogs call rate. "
            "See WXYC/library-metadata-lookup#628."
        ),
    )
    lml_library_release_override: bool = Field(
        default=False,
        description=(
            "When True, /lookup prefetches verified library-release -> Discogs-"
            "release pins from lml_cache.library_release_override for the request's "
            "library rows (one query, keyed by library_id), and fetch_one binds the "
            "pinned release BEFORE the trust-bind and the artist-floor fuzzy search. "
            "A hand-verified human override is the most-trusted signal, so it wins "
            "even over a carried release; an override hit also short-circuits the "
            "fuzzy search + its Discogs call. The pinned release then flows through "
            "enrichment on the normal title gate: a genuine library-album lookup "
            "(request album == the catalog row title, as the dj-site picker and BS "
            "flowsheet-linkage send) passes it and the pin's tracklist surfaces; a "
            "request whose album diverges from the surfaced row's title collapses "
            "to the sentinel as before (no sibling-leak). Skipped on the "
            "/lookup/bulk drain by default (allow_release_resolution_fallback=False "
            "unless the caller opts in, LML#920). When "
            "False, no prefetch runs and resolution is the pre-override fuzzy pick, "
            "byte-for-byte. Ship dark; seed the table, then flip True in Railway "
            "after the staging sample verifies. Fully reversible: DELETE the "
            "source-stamped rows + flip False. See WXYC/library-metadata-lookup#850."
        ),
    )
    lml_emit_server_timing: bool = Field(
        default=True,
        description=(
            "When True, /lookup and /lookup/bulk surface the "
            "RequestTelemetry timings that track_step already captures as a "
            "Server-Timing response header (name;dur=<ms> entries). On /lookup: "
            "one entry per tracked step, a derived `discogs` leg from the "
            "cache_stats pg/api split, then one live `total`; /lookup/bulk emits "
            "a batch-level `total` only (per-item stages aren't meaningful in one "
            "per-HTTP-request header). Out-of-band instrumentation only — the "
            "cache_stats JSON body is byte-identical either way — so a caller "
            "(request-o-matic's `lookup` CLI) can attribute a slow lookup to a "
            "named server stage without any api.yaml / CacheStats change. Default "
            "True; flip False in Railway to withhold internal stage latencies "
            "without a redeploy. The LML half of the cross-repo Server-Timing "
            "trace; part of the enrichment-observability hardening epic "
            "WXYC/Backend-Service#881 (Epic G)."
        ),
    )
    lml_event_loop_lag_gauge: bool = Field(
        default=True,
        description=(
            "When True, a background sampler task (started in main.py lifespan) "
            "measures event-loop scheduling lag — how long the single uvicorn "
            "worker's loop takes to resume past a fixed sleep — and the router "
            "stamps the current value onto each request's cache_stats as "
            "event_loop_lag_ms, riding the cache.* / lml.cache.* projection to "
            "PostHog + Sentry. This surfaces the /lookup single-worker starvation "
            "tax (plans/lookup-latency-event-loop-starvation.md §3) that "
            "track_step and the Server-Timing header structurally cannot see, and "
            "is the before/after metric for Lever A' (#904) and Lever B (#747). "
            "Default True. To disable, set False and confirm a redeploy landed: "
            "the flag is read at process start (the sampler is a lifespan task and "
            "the stamp reads @lru_cache'd Settings), so a var flip takes effect only "
            "on the next boot, and a Railway var-set that comes back SKIPPED leaves "
            "the running sampler untouched. Near-zero cost; see "
            "WXYC/library-metadata-lookup#907."
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
