# Environment Variables

## Required

- `DISCOGS_TOKEN` -- Discogs API token

## Optional

- `DATABASE_URL_DISCOGS` -- PostgreSQL URL for Discogs cache
- `DATABASE_URL_MUSICBRAINZ` -- PostgreSQL URL for the musicbrainz-cache. Powers the MB leg of the `/api/v1/lookup` external-cache fallback (Phase 1.5 mojibake recovery). Optional; when unset the MB leg is skipped.
- `SPOTIFY_CLIENT_ID` -- Spotify client ID for streaming availability checks
- `SPOTIFY_CLIENT_SECRET` -- Spotify client secret for streaming availability checks
- `SENTRY_DSN` -- Sentry error tracking. Init lives in [`wxyc-fastapi`](https://github.com/WXYC/wxyc-fastapi); LML calls `init_sentry(service_name="library-metadata-lookup", environment=settings.environment, ...)` at startup. The default `HttpxIntegration` is on, so outbound calls (Discogs, Spotify, Deezer, Apple Music, Bandcamp) are traced.
- `POSTHOG_API_KEY` -- PostHog telemetry. Client construction lives in `wxyc-fastapi` as a process-wide singleton; LML's `core/dependencies.get_posthog_client` only wraps it with the LML-side `enable_telemetry` flag. The shared client warns once per process when this is unset.
- `LIBRARY_DB_PATH` -- Path to SQLite database (default: `library.db`)
- `ADMIN_TOKEN` -- Bearer token for admin endpoints (upload endpoint)
- `STREAMING_WEBHOOK_URLS` -- Comma-separated URLs to POST streaming status changes after library.db upload
- `ETL_NOTIFY_KEY` -- Bearer token used by LML when *pushing* the streaming-status webhook to tubafrenzy
- `LML_API_KEY` -- Bearer token required from tubafrenzy / Backend-Service callers (see "Inbound Auth" below)
- `LML_REQUIRE_AUTH` -- When `true`, enforce `LML_API_KEY` on protected endpoints. Default `false` (see rollout phasing in `core/auth.py`)
- `LML_RESOLVE_ARTIST_CANONICAL` -- When `true`, the canonical-artist resolver pre-pass in `search_compilations_for_track` swaps the inbound artist name for the top trigram match in `discogs-cache` when the similarity score >= `CANONICAL_ARTIST_SIMILARITY_FLOOR` (`lookup/orchestrator.py`). Default `false`. When the flag is `false`, the entire pre-pass is skipped — no PG trigram lookup, no `resolver_pre_pass` log line, no Sentry attribute. This is the cost-saving path (~50 ms / lookup) until calibration clears the swap to be enabled. To enable the swap, run `scripts.resolver_calibration` against the discogs-cache, confirm FP-rate ≤ 0.5% at the chosen floor, document the run in `docs/resolver-calibration/`, and flip the flag in Railway. See WXYC/library-metadata-lookup#318 and #343.
- `LML_SEARCH_BUDGET_MS` -- Soft budget (ms) for `execute_search_pipeline`; default 4000. Short-circuits the cascade once `state.results` is non-empty (LML#340). When the caller sends `X-Caller-Budget-Ms`, the effective budget is `min(caller − 200ms, env)` (LML#345), AND the cascade also short-circuits when `state.results` is empty (Epic A no-results tail follow-up; sets `state.timed_out=True`). The empty-state cutoff is only active when the caller explicitly opts in via the header — without a header, the cascade keeps grinding past the budget for the no-results case (preserves warm-cache / write-path semantics). Tune via Railway env; values must be a positive integer (unparseable/zero/negative fall back to the default with a WARN).
- `LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS` -- Per-call wall-clock ceiling (ms) for `AppleMusicClient.find_track_url` from the lookup hot path (`lookup/orchestrator.enrich_one`); default 4000. Wraps the call in `asyncio.wait_for` so a single Apple-side retry storm (worst case 2×5s with the LML#450 trim) can't pull the rest of the enrichment past its deadline. On timeout the item degrades to no-Apple-URL — same path as the LML#444 exception guard. Values must be a positive integer (unparseable/zero/negative fall back to the default with a WARN). See LML#449 + LML#450.
- `LML_ARTIST_IDENTITY_SPLIT_GATE` -- When `true` (default), the `extended=true` lookup path gates `artist_bio` / `wikipedia_url` / `profile_tokens` on the LML#504 `artist_identity_verified` composite predicate (request artist ↔ `item.artist` ↔ `top1_release.artist` each at `score_match >= 80`, neither side a `is_compilation_artist` alias). When `false`, those three artist-scoped fields revert to the broader `is_album_derived_eligible` gate that PR #500 introduced — over-suppresses on the same-artist sibling-album shape (Yenbett→Tzenni) but is safer if the split predicate is found to leak somewhere unexpected in production. Read at request time via `os.getenv` so the knob flips without a redeploy. The split predicate only takes effect when `LookupRequest.extended=true` (legacy non-extended callers — request line, dj-site picker — always see the old gate); BS forces `extended=true` on every wire call, so the split exercises on all BS write-path traffic immediately. See LML#504.
- `LML_SEARCH_HARD_TIMEOUT_MS` -- Hard ceiling (ms) on `execute_search_pipeline` wall time; default 25000. Fires regardless of `state.results` to bound cascade-exhaustion tail latency (LML#370). Wrapped around every strategy via `asyncio.wait_for`, so a single slow Discogs cascade gets cancelled with its in-flight `gather()` probes — frees the Discogs semaphore on cap-fire. Unlike the soft budget, callers cannot raise this via header (safety floor, not a budget). To effectively disable, set well above the request timeout (e.g. `600000`). Telemetry: `hard_cap_fired:true` projects onto the Sentry transaction; response carries `LookupResponse.timeout: true`.

## Cross-cache-identity feature flags

Per-cache toggles for which `wxyc_library` hook table the matcher reads (legacy schema vs. new normalized schema), plus an emergency rollback flag for the §3.2.2.1 manual-override skip endpoint. All default `false`.

The **canonical inventory** (with naming-convention rationale, phase state machine, rollout-checklist locations, and approval gates) lives in `WXYC/Backend-Service/CLAUDE.md` "Cross-cache-identity feature flags (canonical inventory)". When a flag is renamed or its default changes, both the canonical Backend section AND this list must update in the same PR; CI on this repo grep-asserts the names listed here match the §4.2 inventory.

| Flag | Scope | Default | Set true when |
|---|---|---|---|
| `LML_USE_NEW_HOOK_DISCOGS` | per-cache (Docker discogs, port 5433) | `false` | Docker discogs cache parity-check passes 7 consecutive days |
| `LML_USE_NEW_HOOK_DISCOGS_FULL` | per-cache (Homebrew full discogs) | `false` | Homebrew (full) discogs cache parity-check passes 7 consecutive days |
| `LML_USE_NEW_HOOK_MUSICBRAINZ` | per-cache | `false` | musicbrainz cache parity-check passes 7 consecutive days |
| `LML_USE_NEW_HOOK_WIKIDATA` | per-cache | `false` | wikidata cache parity-check passes 7 consecutive days |
| `LML_MANUAL_OVERRIDE_CHECK_DISABLED` | global | `false` | Emergency rollback only — disables the §3.2.2.1 manual-override skip endpoint call. Backend's write rules still reject low-confidence reruns of manual rows correctly, just less efficiently. |

Production location: Railway environment variables for the LML service. Updater is Jake via the Railway dashboard. The per-cache flags require 7 consecutive days of clean parity-check audit (E5 daily report); `LML_MANUAL_OVERRIDE_CHECK_DISABLED` is for unscheduled emergency rollback only.

Plan reference: `WXYC/wiki/plans/library-hook-canonicalization-plan.md` §4.2.

## Inbound Auth (`LML_API_KEY`)

`core/auth.py:require_lml_key` is a FastAPI dependency that validates `Authorization: Bearer <LML_API_KEY>` on every tubafrenzy / Backend-Service-facing endpoint. Wired in `main.py` via `app.include_router(..., dependencies=[Depends(require_lml_key)])` for the `lookup`, `library`, `discogs`, and `streaming` routers.

Three auth surfaces in this service, intentionally separate:

| Surface | Direction | Env var | Implementation |
|---|---|---|---|
| Inbound from tubafrenzy / Backend-Service | tubafrenzy → LML | `LML_API_KEY` | `core/auth.py:require_lml_key` (router-level dep) |
| `/admin/*` (library.db upload) | ETL → LML | `ADMIN_TOKEN` | `routers/admin.py:_validate_auth` (per-route call) |
| Outbound streaming-status webhook | LML → tubafrenzy | `ETL_NOTIFY_KEY` | `routers/admin.py:_send_streaming_webhook` (sends `Authorization: Bearer ...`) |

Untouched: `/health`, `/identity/resolve`, `/identity/bulk`. Identity routes are consumed by semantic-index too, so locking them down is a separate decision.

**Rollout:** `LML_REQUIRE_AUTH` defaults to `false` so the dep can ship before consumers send the header. Once tubafrenzy and Backend-Service are updated, flip the flag in Railway to enforce.
