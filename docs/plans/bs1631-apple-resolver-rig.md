# Plan: Off-prod Apple-Music-URL resolver for the BS#1631 tail

## Problem / context

BS#1631 (Apple-Music-URL backfill) drives LML `/api/v1/lookup` over ~27K still-null `album_metadata` rows (plus a flowsheet phase) to fill `apple_music_url`. It keeps tripping the prod LML health watchdog and captures far fewer URLs than exist. This session established, empirically:

- **Capture gap.** The backfill's own logs show ~29% album-phase resolve. An independent iTunes-Search-API ceiling sample (n=475, random still-null album candidates) found **~58% have a findable Apple album** (95% CI [53.9%, 62.7%]; `any_apple_result` 70.9%). Spot-checked: hits are exact, and misses include false-misses from album-title format noise (`cd-single`, `7-inch`, quotes), so the true ceiling is *above* 58%. So the tail is worth pursuing — coverage is not the limiter.
- **Why capture < ceiling — three causes:**
  1. **Async warm throttle (timing).** `apple_music_url` is filled by an async background "warm" (live Apple probe → `lml_cache.album_streaming_url_cache`), serialized globally at `LML_STREAMING_WARM_CONCURRENCY=1` (pinned during the flood). The backfill re-reads only `BACKFILL_SECOND_PASS_DELAY_MS=5000` later, so under real throughput the warm hasn't drained before the second pass → false `still_null`. **Confirmed**: an album returned `null` on pass 1, then the real Apple URL on pass 2 after a 75s wait.
  2. **Apple-API matching < iTunes ceiling (structural).** LML uses the authenticated Apple Music API (migrated off iTunes Search after the LML#443 egress 403) with a fuzz-floor 3-way match; some iTunes-findable albums are (correctly or not) rejected. Realistic achievable sits between 29% and 58%.
  3. **Prod LML latency (structural, overriding).** With the backfill paused, organic lookups take **16–25s** (`extended` and non-`extended` alike). Server-Timing shows ~280ms in-process; the remaining ~10–20s is **uninstrumented synchronous external I/O** — the Apple Music JWT API artwork/track probe on the hot path (plus Discogs/Spotify sync enrichment). The discogs-cache DB is healthy (2GB shared_buffers, 99.6% hit ratio, idle — LML#706 buffer-thrash is fixed), so this is *not* the DB. Every lookup exceeds the backfill's own `BACKFILL_LML_PER_CALL_TIMEOUT_MS=15000`.

**Key insight.** Almost none of LML's per-lookup work is needed to get `apple_music_url`. Library search, Discogs cross-ref, identity resolution, artwork, extended deep-parse — all overhead. The persistent album-level `apple_music_url` the backfill reads comes solely from the warm calling **`clients/streaming/apple_music.py::AppleMusicClient.find_album_match(artist, album)`** (`entity/streaming_url_cache.py:350`), which uses the shared fuzz-floor `find_best_source_match`. So a standalone resolver that calls `find_album_match` directly has **exact match parity** with the album-phase capture path, at a fraction of the cost.

This plan builds that lean resolver, run **off-prod** (no LML service, no watchdog, controllable concurrency), and applies results via the existing Backend-Service fill-only write.

## Desired end state

The still-null album-phase tail is resolved off-prod: each candidate's `(artist, album)` is matched against the Apple Music API via LML's exact matching module, at high concurrency, with results applied back to `wxyc_schema.album_metadata.apple_music_url` under the existing fill-only (`WHERE apple_music_url IS NULL`) semantics. No prod LML lookups, no watchdog trips. Expected wall-clock: ~27K × ~1–3s ÷ N-way concurrency, respecting Apple's rate limits (order ~1–2 hours at 10–15 way).

## Approach: reuse the matching module, skip the service (Option B)

Two candidate shapes were considered:

| | **A. Full local LML** | **B. Lean Apple resolver (chosen)** |
|---|---|---|
| Mechanism | Stand up LML against a local filtered discogs-cache + library.db + creds; drive `/lookup` | Standalone script importing LML's `clients/streaming/apple_music.py` + `clients/streaming/matching.py`; call `find_album_match` per candidate |
| Match parity | full stack | **exact** — same `find_album_match` / `find_best_source_match` the warm uses |
| Per-item cost | ~16–20s (full LML path) | ~1 Apple API call (~1–3s) |
| Setup | high (cache export, library.db, boot, config) | low (script + Apple JWT creds) |
| Concurrency | bounded by LML internals + warm=1 | fully controllable (I/O-bound → high concurrency) |
| Touches prod | no | no |

**Chosen: B.** The full LML machinery is pure overhead for `apple_music_url`; B reuses the only load-bearing part (Apple matching), runs at arbitrary concurrency off-prod, and sidesteps both the watchdog and the sync-enrichment latency wall.

**Two-phase, clean ownership boundary:**

1. **Resolve (LML repo).** `scripts/resolve_apple_urls.py` reads the still-null candidate set (artist, album, `album_id`), calls `AppleMusicClient.find_album_match(artist, album)` at bounded concurrency, and emits `(album_id, apple_music_url)` for accepted matches to a TSV. Read-only against BS; no writes.
2. **Apply (Backend-Service repo).** Feed the emitted TSV to the existing fill-only write (`jobs/apple-music-url-backfill/resolve.ts::applyUpdate`, `WHERE apple_music_url IS NULL`) via a thin apply entry, or a small one-off applier that reuses the same Drizzle UPDATE. Never overwrites a non-null URL.

This reuses **both** proven pieces (LML's Apple matching + BS's fill-only write) and keeps the LML↔BS boundary clean (LML never writes BS RDS).

### Scope note: album-level only (this pass)

`find_album_match` is album-level. The album-phase backfill (no track) captures `apple_music_url` *only* via this album-level warm, so parity is exact for the album phase. The flowsheet phase additionally has a track-level probe (`find_track_metadata`, LML#477 title gate) that can surface track-specific URLs; **this plan targets the album-phase tail** (`album_metadata`). A flowsheet/track-level pass is a possible follow-up, explicitly out of scope here.

### Steps

0. **Worktree.** `git worktree add` off `origin/main` in LML before any code (the default tree is on `prod` and lags main). All work lands there.
1. **Pin the candidate set + creds.** Reuse the exact album-phase predicate already pinned for the seed plan (`am.apple_music_url IS NULL AND (am.discogs_url IS NOT NULL OR l.on_streaming) AND COALESCE(a.artist_name,l.artist_name) IS NOT NULL`, JOIN library/artists). Export `(album_id, artist, album)` read-only from BS RDS. Confirm the Apple JWT creds available for the run (`APPLE_MUSIC_TEAM_ID`/`KEY_ID`/`PRIVATE_KEY`) and that they are the prod creds (coordinate rate budget; see Data safety).
2. **Resolver.** `scripts/resolve_apple_urls.py`: construct `AppleMusicClient` from `os.environ` Apple creds **modeled on the existing off-prod precedent** `scripts/recheck_streaming_after_mojibake.py:193-197` (same `team_id`/`key_id`/`private_key` construction + missing-creds skip/log shape; `scripts/streaming_availability/__main__.py:842-851` is a second precedent, and `streaming/dependencies.py::get_apple_music_client` the in-app factory). Throttle with `asyncio.Semaphore(concurrency)` + `asyncio.gather`, matching the repo pattern (`scripts/bandcamp_pipeline.py:50-58`); if a hard per-minute Apple ceiling is needed, add a simple sleep-based delay rather than a new dependency (do **not** add `aiolimiter` — not in `pyproject.toml`/`uv.lock` and divergent from the established `Semaphore` pattern). `find_album_match(artist, album)` per candidate; emit `album_id\tapple_music_url` for accepted matches; log per-outcome counts (matched / no_match / api_error). `--dry-run` (resolve + tally, emit nothing), `--limit`, `--concurrency`, `--rate-per-min`. Uses the repo logging convention.
3. **Accept-rate sample run.** Run against the same 475-album ceiling sample first; compare the resolver's accept rate to the iTunes ceiling (58%) and to the backfill's 29%. This quantifies cause-2 (Apple-API matching haircut) with real numbers before a full run. (Distinct from `scripts/resolver_calibration/`, the unrelated artist-similarity threshold-sweep tooling — this is just an accept-rate tally.)
4. **Full resolve run** (off-peak; bounded concurrency/rate). Produce the full `(album_id, url)` TSV + a run record.
5. **Apply (BS).** First **confirm `resolve.ts::applyUpdate` still exists** in Backend-Service and pin its commit/branch (it is the BS#1631 job this effort augments, so it may be mid-change); if it has moved, fall back to the small one-off applier (a Drizzle UPDATE with the same fill-only predicate). Apply the TSV via the fill-only UPDATE. SELECT-before / count-after; never overwrite non-null.
6. **Verify.** Re-count `apple_music_url IS NOT NULL` on the candidate set before/after; spot-check a dozen applied URLs resolve to the right album on Apple.

### Deliverables

- `scripts/resolve_apple_urls.py` (LML) — reusable, bounded-concurrency, rate-limited, `--dry-run`, TSV output, structured logging.
- Backend-Service apply entry (or reuse of the existing job's `applyUpdate`) for the fill-only write.
- A run record (per-outcome counts, accept rate vs ceiling) attached to BS#1631.
- Docs: document the resolver in `docs/scripts.md` (the CLAUDE.md router's designated home for scripts, alongside bandcamp_pipeline / artist_resolve_drain / resolver_calibration) — not a new runbook or README section (neither convention exists here).

### TDD (required)

- **Resolver unit tests** (no network): mock `AppleMusicClient.find_album_match` to assert (a) accepted matches emit `(album_id, url)`, (b) `None` matches emit nothing, (c) API errors are counted and don't abort the run, (d) `--dry-run` emits nothing, (e) concurrency/rate limits are honored (semaphore/limiter wired). Follow existing LML async-client test patterns (`docs/testing.md`).
- **Match-parity test**: assert the resolver calls `find_album_match` (album-level) — the exact warm path — not `find_track_metadata`, so captured URLs match what the warm would have cached.
- No live Apple calls in tests (mock the client); a single opt-in `external_api`-marked smoke test may exercise one real call.

## Data safety

- **Fill-only, never overwrite.** Apply uses `WHERE apple_music_url IS NULL` (the existing `resolve.ts` invariant). No non-null URL is ever changed. SELECT-before / count-after to prove scope.
- **Read-only against BS for candidates**; the only BS write is the fill-only apply, reviewed separately.
- **Apple rate limits + shared creds.** The Apple JWT creds are prod's. Run off-peak, rate-limited to Apple's published ceiling, so the resolver never starves prod's own Apple usage. A 403/429 backs off (reuse the client's existing backoff). Coordinate before the full run.
- **No prod LML load**; no discogs-cache writes; no watchdog exposure.
- **Dry-run first** (resolve + tally, no emit, no apply).

## Risks / open items

- **Apple-API matching haircut (cause 2).** Realistic yield < 58% iTunes ceiling. Step 3 calibration quantifies it before committing to a full run; if the resolver's accept rate is near the backfill's 29%, the timing fix (cause 1) — not matching — was the real gap and a simpler prod-side change (raise warm concurrency off-peak / decouple second pass) may suffice instead.
- **Shared Apple creds / rate budget.** Uncoordinated bulk Apple calls could 429 prod's live enrichment. Bound rate; run off-peak.
- **Album-title noise.** `cd-single` / `7-inch` / quote artifacts in `album_title` depress matches (seen in the ceiling misses). Consider a normalization pass (strip format suffixes) — measure its lift in Step 3.
- **Album vs track scope.** This pass is album-level only; flowsheet track-level URLs are out of scope (see scope note).
- **Genuinely-absent tail.** ~30–40% have no Apple album at all; those stay `still_null` correctly. Do not re-probe them repeatedly (the fill-only predicate keeps them eligible; a future re-run is safe but low-yield).

## Acceptance criteria

- [ ] Worktree off `origin/main` before implementation.
- [ ] Red unit tests exist and fail before the resolver; prove emit-on-match, no-emit-on-None, error-tolerance, `--dry-run` no-emit, and album-level `find_album_match` parity.
- [ ] `resolve_apple_urls.py` resolves candidates via `find_album_match` at bounded concurrency + rate limit, emits `(album_id, url)` TSV, `--dry-run`, structured logging. Green.
- [ ] The Step-3 accept-rate sample run on the 475-sample reports the resolver's accept rate vs the 58% ceiling and 29% baseline, recorded on BS#1631.
- [ ] Apply is fill-only (`WHERE apple_music_url IS NULL`); a SELECT-before proves scope; non-null URLs provably unchanged (before == after on the already-set rows).
- [ ] Full-run record (per-outcome counts) on BS#1631.
- [ ] No prod LML lookups issued by the resolver; Apple calls rate-limited; run off-peak.

## Related

- WXYC/Backend-Service#1631 — the backfill this replaces/augments.
- WXYC/library-metadata-lookup#706 — the sync-enrichment latency wall (cause 3) this sidesteps; worth its own ticket for organic-traffic impact.
- `clients/streaming/apple_music.py::find_album_match` / `clients/streaming/matching.py::find_best_source_match` — the reused matcher.
- `entity/streaming_url_cache.py:350` — proof the warm (the backfill's capture path) calls `find_album_match`.
- `Backend-Service/jobs/apple-music-url-backfill/resolve.ts::applyUpdate` — the reused fill-only write.
- `https://github.com/WXYC/discogs-etl/blob/main/docs/seed-cache-from-clone-runbook.md` — the seed effort; its live-coverage check surfaced that the tail is library albums and that resolution ≠ Apple-URL yield.
