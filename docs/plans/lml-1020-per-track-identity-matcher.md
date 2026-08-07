# LML#1020 — Per-track compilation identity matcher + backfill

Sub-issue of [LML#271](https://github.com/WXYC/library-metadata-lookup/issues/271) §2 + §6. Blocked-by [#1019](https://github.com/WXYC/library-metadata-lookup/issues/1019) (closed — the recall index shipped). Blocks [#1021](https://github.com/WXYC/library-metadata-lookup/issues/1021), which blocks [WXYC/Backend-Service#1991](https://github.com/WXYC/Backend-Service/issues/1991) (S2 of [BS#801](https://github.com/WXYC/Backend-Service/issues/801)).

## What this delivers

An internal per-track **identity** matcher: for each compilation track credit WXYC holds, resolve the credited artist to external identities (Discogs + MusicBrainz), store the per-source verdict as an **attempt row**, and recover the track's position where the Discogs tracklist can supply one. Plus a backfill script that drains the existing population, and the retirement of `scripts/match_compilations.py` / `scripts/merge_cta.py`.

This is Layer 2 of #271's Option B split. Layer 1 (#1019's `lml_cache.compilation_track_location`) is a *recall/location* index — it answers "which shelf holds track T by artist A" and carries **no external-ID resolution**. Layer 2 is what `bulk-resolve`'s `tracks[]` needs, because `BulkResolveTrackIdentity.sources[]` requires per-source external IDs the recall index does not hold.

## What is already built (do not rebuild)

`scripts/build_compilation_track_location.py` + `entity/compilation_track_location.py` already do a surprising amount of the mechanical work, and this plan leans on them rather than duplicating:

- **Comp → Discogs release matching**, override-first (`lml_cache.library_release_override`) then the exact → prefix-strip → trigram title cascade against `va_release`.
- **Per-credit enumeration from Discogs** — every `release_track_artist` credit (all tiers, not just primary), joined to `release_track` for position and title. Note this is Discogs's own tracklist, *not* WXYC's CTA corpus; the two are different sets of strings about the same tracks, which is what makes D5's join a matching problem rather than a lookup.
- **Position materialization** — `track_position` is already stored NOT NULL, falling back to the track sequence number when Discogs carries no position string.
- **Population-set definition** — `wxyc_etl.text.is_compilation_artist`, which covers `Soundtracks - *` as well as `Various Artists - *` (the issue's explicit requirement).
- **Normalization** — `wxyc_etl.text.to_match_form` on artist and title, the same form a runtime probe applies.

So the position-recovery deliverable is a **local join**, not new Discogs work, and this plan does not re-derive comp→release matching.

## Findings that change the ticket as written

Four things surfaced while grounding the ticket. Each changes a stated deliverable; all four are verified against the code, not inferred.

### F1 — `track_position` is not merely NULL-heavy on the LML side; it does not exist

The 2026-08-06 amendment carries BS#1989's finding that 78% of Backend's CTA rows are position-NULL. On LML's side it is worse and simpler: **the `compilation_track_artist` table in `library.db` is three columns** — `(library_release_id, artist_name, track_title)` — per `discogs-etl/scripts/tsv_to_sqlite.py:24-34`. There is no position column at all, in either the MySQL-sourced producer today or the Backend-sourced producer that discogs-etl#351 specifies.

This is not only a discogs-etl fact — the committed contract already states it. `generated/api_models.py:1897` (`TrackMatchHint.position`): "Null when position is unknown or inapplicable (e.g., CTA-derived matches, since `compilation_track_artist` has no position column)."

Consequence: amendment #3's "position recovery is an explicit deliverable" is not a nice-to-have *and* not optional — it is the **only** source of position for LML. 100% of positions come from the tracklist join, not 78%. This strengthens the case for the deliverable and removes any "pass through what CTA gave us" fallback.

**The column shape is not verified at runtime, and the in-repo fixtures disagree about it.** `library/db.py:193-197` probes only that the *table* exists (`_has_compilation_track_artist`), never its columns. `tests/unit/test_library_db.py:1434-1438` builds `(library_release_id, artist_name, track_title)` — the shape `library/db.py:399` joins on — while `tests/integration/test_library_db.py:213-217` builds a 2-column `(library_id, artist_name)` and still sets the capability flag true. A drain against an older `library.db` snapshot would hard-fail mid-enumeration on `SELECT track_title`. Slice 3 therefore declares the minimum CTA shape it requires and **fails fast at startup with a named error**, matching the posture of the existing capability probe rather than discovering it 40,000 credits in.

### F2 — `library.db`'s `library.id` is the legacy id space; Backend's `library_id` is not

`library.db` stores the legacy MySQL `LIBRARY_RELEASE_ID` as `library.id` (and discogs-etl#351's Backend-sourced producer deliberately preserves that by emitting `legacy_release_id AS id`, for parity). Backend's `wxyc_schema.library.id` is an independent serial with `legacy_release_id` as a *separate* column (`shared/database/src/schema.ts:477-511`).

So the `library_id` LML would naturally store — the one it reads out of `library.db`, and the one `lml_cache.compilation_track_location.library_id` already holds — is **not** the `library_id` that arrives on `BulkResolveInput`. A naive id join in #1021 silently returns zero rows. This is the same trap already recorded on BS#801 ("MySQL keys CTA on `LIBRARY_RELEASE_ID`, not a Backend `library.id`… a naive id join silently returns nothing. Cost me a probe round-trip").

This plan does not solve the mapping — that is #1021's read-side problem. And #1021 has a better tool than the `artist_name`/`album_title` hints: the committed contract already names the bridge. `CatalogCompilationTrackRow.legacy_release_id` (`generated/api_models.py:490-496`) is documented as "The owning library row's `legacy_release_id` (BS#1963). Becomes library.db's `compilation_track_artist.library_release_id`, which joins to library.db's `library.id`." That is F2's mapping stated verbatim as a contract field, so #1021 can key on a named identifier rather than fuzzy-matching hint strings.

What this plan owes #1021 is: **name the column's key space unambiguously in the schema, and pin it with a test**, so the trap is sprung at review time rather than in prod.

### F3 — the shared-rate-bucket AC is **not** satisfied by holding a `DiscogsService`, and this changes where the drain runs

An earlier draft of this plan claimed the AC ("all external calls routed through the shared Discogs rate bucket; a test/inspection confirms no second client") was satisfied by construction, because `discogs/service.py` owns the rate gate (`get_discogs_rate_gate()`, line 725) and the LML#927 bulk-reservation semaphore (`acquire_discogs_permits`, line 312) inside `DiscogsService`. **That was wrong, in the permissive direction**, and it is worth stating plainly because the reformulated AC would have passed inspection while the actual constraint was violated.

Three facts, all in the code:

- `discogs_rate_bucket_enabled` defaults to **False** (`config/settings.py:362`).
- With it off, `DiscogsRateGate.acquire()` falls straight through to the **per-process** `AsyncLimiter` (`discogs/ratelimit.py:294-297`).
- The semaphore and the LML#755 saturation breaker are per-process by design — `settings.py:352-361` says so explicitly. The shared PG bucket is the *only* cross-process coordination there is.

So a standalone backfill process holding its own in-process `DiscogsService` is not on a shared anything. It is an N+1th uncoordinated limiter metering against the same single Discogs token — precisely the scenario `docs/scripts.md:206` warns about, and precisely why the sibling `artist_resolve_drain` **drives the prod HTTP endpoint instead of resolving in-process**.

Consequence for this plan: the backfill does not construct `BareNameArtistResolver`. It POSTs credit batches to **`POST /api/v1/artists/resolve/bulk`** against prod, where the resolver runs inside the service process that owns the limiter, the semaphore, and the breaker. F4's "the Discogs leg already exists" still holds — it just lives behind an HTTP boundary rather than an import. The AC becomes "the drain makes no direct Discogs calls at all," which is stronger than the version this plan started with and is checkable by inspection.

### F4 — the Discogs leg of the matcher already exists as `BareNameArtistResolver`

§2's "reuse the existing artist-level cascade where it transfers" predates LML#759. A compilation track credit **is** a bare artist name with no album anchor — which is exactly the input `artists/resolver.py`'s `BareNameArtistResolver` takes. It already implements a three-tier cascade (tier 1 batched `entity.identity` read, tier 2 discogs-cache candidate-set evidence, tier 3 live Discogs API), emits `ArtistResolveMethod` + `unresolved_reason` + `canonical_name`, dedupes by identity-match form, and is wired through `DiscogsService` (so F3 holds).

Reusing it removes the largest chunk of §2's stated scope and — more importantly — means the per-track matcher inherits LML#759's verify-before-mint discipline rather than growing a second, less-tested artist matcher. §2's "no album-artist anchor" concern is *already* this resolver's premise.

What it does **not** give us is a MusicBrainz leg. That is genuinely new (see D3).

## Design decisions

### D1 — storage: `lml_cache.compilation_track_identity`, credit-shaped key

`lml_cache.*`, not `entity.*`: LML is the only reader (BS reads the HTTP response, not the table — post-pivot there is no direct cross-service reader), so this stays lifespan-bootstrapped and avoids the three-repo alembic dance. This confirms the escape hatch #271 left open, and matches BS#801's D6, which names LML's per-track table the system of record for per-source provenance and cancels the Backend-side `library_track_identity_source` sidecar outright.

Key is **credit-shaped**, per amendment #1 — `(library_id, track_artist, track_title, source)` on the `to_match_form`-normalized artist and title. Explicitly *not* `(library_id, track_position, source)`: positions do not exist on the input (F1), and a 66-performer track legitimately shares one position, so a position key collides by construction at this grain.

Shown here with commentary for review. **The `_DDL_TABLE` constant itself ships comment-free** — `scripts/regenerate_lml_cache_sql.py:8-13` records that inline per-column `--` comments were deliberately replaced with above-statement prose because the parity tests strip comments before comparing (`tests/unit/test_compilation_track_location_schema.py:73`), and none of the seven existing `_DDL_TABLE` constants carry any. Leaving them inline would park the F2 warning and the CHECK rationale in an unverified duplicate — the exact failure LML#1038 fixed. All of the commentary below goes in the `SidecarSpec` `comments` map and the module docstring instead.

```sql
CREATE TABLE IF NOT EXISTS lml_cache.compilation_track_identity (
    -- library.db `library.id` == the legacy MySQL LIBRARY_RELEASE_ID.
    -- NOT Backend's wxyc_schema.library.id. See F2 before joining this.
    library_id           INTEGER NOT NULL,
    track_artist         TEXT NOT NULL,   -- to_match_form; join key
    track_title          TEXT NOT NULL,   -- to_match_form; join key
    source               TEXT NOT NULL CHECK (source IN ('discogs', 'musicbrainz')),
    external_id          TEXT,            -- NULL = attempted, no match
    confidence           REAL,
    method               TEXT,
    resolved_artist_name TEXT,            -- canonical name
    -- Verbatim echoes for the wire (#297's join-back keys). The normalized
    -- columns above are the key; these are what #1021 sends.
    track_artist_raw     TEXT NOT NULL,
    track_title_raw      TEXT,
    -- Recovered from the release tracklist, never from CTA (F1). NULL when
    -- the join found no tracklist row.
    track_position       TEXT,
    attempted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The D2 attempt-row invariant, enforced rather than commented. The
    -- sibling table sets the precedent (CHECK on `credit_role`, on
    -- `discogs_release_id > 0`): shape rules that a consumer depends on
    -- live in the DDL, not in prose above it.
    -- `resolved_artist_name` is deliberately NOT in this constraint: a
    -- tier-1 `identity_store` hit resolves with a non-NULL Discogs id and
    -- `canonical_name=None` (artists/resolver.py:522-530 — entity.identity
    -- stores no Discogs title; the contract says "Present iff resolved via
    -- api_search"). That is the COMMON resolved shape on a warm store, so
    -- binding it into the coherence rule would abort most inserts.
    CONSTRAINT compilation_track_identity_verdict_coherent CHECK (
        (external_id IS NULL) = (confidence IS NULL)
        AND (external_id IS NULL) = (method IS NULL)
    ),
    PRIMARY KEY (library_id, track_artist, track_title, source)
);

-- The retry cohort, and the only access path the PK does not already serve.
-- A plain (library_id) index would be dead weight: library_id is the PK's
-- leading column, so `WHERE library_id = $1` is served from the PK btree.
-- (The sibling's extra index earns its keep because (track_artist,
-- track_title) is NOT a prefix of its PK; that reasoning does not transfer.)
CREATE INDEX IF NOT EXISTS idx_compilation_track_identity_misses
    ON lml_cache.compilation_track_identity (library_id)
    WHERE external_id IS NULL;
```

`track_title` is NOT NULL in the key while `track_title_raw` is nullable: CTA's title column is nullable upstream, and a NULL cannot sit in a PK. Normalizing NULL to `''` makes the key total and keeps "no title" addressable; the raw column preserves the distinction for the wire. This is checked by test, not left implicit.

The write is **two statements, not one**, because the verdict and the position have genuinely different update rules and cramming both into one `ON CONFLICT` is how this plan produced a defect in each of its two previous drafts. (Draft 1 guarded on `external_id IS NULL` and stranded positions on resolved rows; draft 2 widened the guard and thereby let a later miss null out an existing verdict — coherently, so the CHECK would not have caught it.) Splitting them makes each statement's job single and its guard obvious:

```sql
-- 1. The verdict. Written once; never overwritten. The org data-safety
--    rule as a SQL predicate.
INSERT INTO lml_cache.compilation_track_identity (...) VALUES (...)
ON CONFLICT (library_id, track_artist, track_title, source) DO UPDATE
   SET external_id = EXCLUDED.external_id,
       confidence = EXCLUDED.confidence,
       method = EXCLUDED.method,
       resolved_artist_name = EXCLUDED.resolved_artist_name,
       attempted_at = now()
 WHERE compilation_track_identity.external_id IS NULL;

-- 2. The position. Fills a NULL, on resolved and unresolved rows alike;
--    never overwrites a populated one, never touches a verdict column.
UPDATE lml_cache.compilation_track_identity
   SET track_position = $5::text
 WHERE library_id = $1 AND track_artist = $2
   AND track_title = $3 AND source = $4
   AND track_position IS NULL AND $5::text IS NOT NULL;
-- The ::text casts are required, not cosmetic: a bare `$5 IS NOT NULL`
-- gives asyncpg nothing to infer from and raises "could not determine
-- data type of parameter $5".
```

Statement 1's `WHERE` means a successful row is never touched, and `attempted_at = now()` inside it lets a re-attempted miss age forward so `--retry-misses` cohorts can be bounded by time rather than re-draining the whole NULL set forever.

Statement 2 exists because position and verdict arrive on **different schedules**. D5 bounds position coverage by the recall index, and that index *grows over successive runs* of `build_compilation_track_location.py` — so a credit that resolved before its comp entered the index must still be able to gain a position afterwards, or positions strand on exactly the resolved rows #1021 wants them for. Making it a separate statement means "the position may arrive late" cannot leak into "the verdict may be rewritten."

DDL lives in `entity/compilation_track_identity.py` with a generated `.sql` sibling, matching the `compilation_track_location` pattern exactly (`bootstrap_lml_cache_table`, statement-text parity test, `scripts/regenerate_lml_cache_sql`).

### D2 — attempt rows are the resumability mechanism *and* the resolved signal

Per amendment #2: when the matcher visits a release, write a row for **every** CTA credit on it — misses get `external_id IS NULL`. This is load-bearing three ways and all three are testable:

1. **Org data-safety rule.** A re-run must never re-attempt a success. With attempt rows, "only retry misses" is a `WHERE external_id IS NULL` predicate rather than a separate failure-tracking table.
2. **#1021's three-state `tracks[]`.** Non-empty ⇒ the matcher visited ⇒ resolved, which is what lets the BS consumer exit its 30-day re-ask sweep (BS#801 D10). Without attempt rows the array is empty for a fully-attempted-but-unmatched release, and the sweep never terminates — the exact live cost #271 documents.
3. **Distinguishing "no source produced a row" from "the legs ran and found nothing"** — #297's `sources[]` semantics depend on it.

Note this is a deliberate departure from `compilation_track_location`'s "a failed comp produces zero rows and is retried for free" design. That table can afford absence-as-retry because it has no consumer that reads emptiness as a signal. This one cannot: emptiness *is* the signal.

### D3 — matcher: `BareNameArtistResolver` for Discogs, a thin MB leg alongside

Per F4 the Discogs leg is `BareNameArtistResolver` — but per F3 it is reached over HTTP, not by import: the drain POSTs batches of distinct credit strings to `POST /api/v1/artists/resolve/bulk` against prod, matching `scripts/artist_resolve_drain/`. That gives Discogs `external_id`, `method`, and `canonical_name` from inside the process that owns the rate limiter, semaphore, and breaker.

**Its failure contract has to be handled, not assumed** — and it has three distinct outcomes, which must land as three different things:

| Resolver outcome | What it means | What we write |
|---|---|---|
| `InvalidNameError` (blank, NUL, empty match form, bare `(2)`) | The name has no identity content | **Miss row.** The leg ran; there was nothing to resolve |
| `escalation_unavailable` (breaker shed, 429, 5xx-after-retries, no token) | Discogs was never asked | **No `discogs` row at all** |
| `CacheUnavailableError` / `PostgresError` (surfacing as HTTP 503) | Infrastructure is down | **Abort the run** |
| MB PG query error or timeout | MusicBrainz was asked but could not answer | **No `musicbrainz` row at all** |

`resolve()` validates every name up front (`artists/resolver.py:459-461`) and raises `InvalidNameError` — aborting the **entire batch** (the route maps it to 422), not the offending entry. CTA credit strings are unsanitized library free text, so this will fire; the drain pre-filters credits through the same validation before the batch call.

**The MB failure row is the third instance of one defect class in this plan, so it gets stated as a rule rather than a case.** Any outcome that means *"the source was not successfully consulted"* — Discogs breaker shed, Discogs 429, MB PG error, MB unconfigured — writes **no row for that source**. Only *"the source answered, and the answer was nothing"* writes a miss row. Getting this backwards durably records an outage as negative evidence, and D2 makes negative evidence the thing `--retry-misses` declines to revisit and D10 makes it a resolved signal for BS#1991.

This matters concretely for MB because the obvious reuse target **hides** the distinction: `lookup/external_search.py:153-155` catches bare `Exception`, logs a warning, and returns `[], None` — byte-identical to "no candidates found." The MB leg therefore does **not** call `search_external_artists`; it calls the extracted helper described below and handles the error itself.

**`escalation_unavailable` is the subtle one, and it is the same defect as the MB-unconfigured case wearing different clothes.** `artists/resolver.py:449-455` documents that Discogs API failures never raise — they land as per-name `escalation_unavailable` verdicts. Left unhandled, it falls through to "unresolved" and becomes an `external_id IS NULL` row, which under D2 asserts *the leg ran and found nothing*. It didn't run. That is exactly the assertion the MB rule refuses to make, and it is worse here: D10's semantics make a non-empty `tracks[]` a resolved signal, so BS#1991 would exit its 30-day sweep for a credit Discogs never saw. So it writes **no `discogs` row**, symmetric with MB. `scripts/artist_resolve_drain/drain.py` already treats this verdict as the one retryable class with a cooldown, which corroborates the reading.

PG failures are deliberately *not* caught — a PG outage must not be recorded as 100,000 legitimate misses that the retry cohort then treats as attempted.

Because the drain reaches the resolver over HTTP (F3), it does **not** construct `EntityStore` / `DiscogsCacheService` / `DiscogsService` itself (`artists/resolver.py:419-428`) — those belong to the running service. What the drain owns is the client side: batch size, auth, retry-with-cooldown for the `escalation_unavailable` class, and resume-on-restart, all of which `scripts/artist_resolve_drain/` already implements.

**MusicBrainz leg.** The service's MB access path is `lookup/external_search.py`'s `_MB_ARTIST_FUZZY_SQL`, reached through `get_musicbrainz_pg` against `DATABASE_URL_MUSICBRAINZ`. The MB leg reuses that SQL over a `PgSource` — cache-local only, **no live MB API calls**. (`scripts/musicbrainz_matching.py` is a one-off dev script with a hardcoded `postgresql://jake@localhost/musicbrainz` DSN; it is not the integration point and is not a foundation for anything.)

**It needs a small extraction first, named here so it isn't discovered mid-slice.** `_MB_ARTIST_FUZZY_SQL` is module-private (`external_search.py:53`), and the public wrapper `search_external_artists` is unusable for D3's decision rule twice over: it drops the `score` column the similarity floor needs, and it short-circuits on any discogs-cache hit (`external_search.py:129-141`) so the MB leg may never run. Slice 3 extracts a public helper returning `id` / `name` / `score`, which both `search_external_artists` and this matcher call.

Two properties of that SQL constrain how its output may be used, and neither is optional:

- **It is trigram-only.** `%` similarity over `mb_artist.name` ∪ `mb_artist_alias.name`, `ORDER BY similarity DESC LIMIT $2` — there is no exact leg and, unlike the Discogs leg, no verify-before-mint ambiguity veto. It returns a ranked list, not a verdict. So the MB leg needs its own decision rule, specified alongside the Discogs confidence table: a **similarity floor** below which the result is a miss row, a deterministic **tie-break** so re-runs are stable, and an **ambiguity rule** — two candidates within a narrow band of each other resolve to a miss row, not to the top one. Recording a coin-flip as a resolved identity is worse than recording a miss, because D2 makes a resolved row permanent (the upsert guard never revisits it).
- **It has a documented sequential-scan hazard.** `external_search.py:48-52` warns that without expression-trigram indexes on `lower(f_unaccent(mb_artist.name))` and `lower(f_unaccent(mb_artist_alias.name))`, the `%` predicate falls back to a seq scan, and directs the reader to coordinate with WXYC/musicbrainz-cache on the index DDL. One interactive lookup can absorb that; a full-population drain cannot. **Verify those indexes exist on the drain target before sizing slice 5** — if they don't, that is a musicbrainz-cache prerequisite, not something to discover mid-drain.

`DATABASE_URL_MUSICBRAINZ` is **optional** (`config/settings.py:165`) and the MB leg is skipped when it is unset. Under D2 that distinction is load-bearing and must not be silent: when MB is unconfigured the matcher writes **no `musicbrainz` row at all** — never a miss row. A miss row asserts "the leg ran and found nothing," which is a measurement; absence asserts "not asked," which is the truth. Getting this backwards would durably record an unconfigured environment as negative evidence that `--retry-misses` would then decline to revisit. The backfill logs the MB-unconfigured state loudly at startup and reports MB coverage as a separate stat.

Per-source rows are written independently. Composition across sources (the cross-source-agreement boost, the MIN fallback) is **#1021's job at wire-compose time**, not this table's — the same split `identity/bulk_resolve.py` already uses for the album-level arm, where `entity.identity` stores per-source facts and `_compose_main` composes at response time. Keeping composition out of storage means a rules change doesn't require a re-drain.

**The stored `method` must be an `IdentityMethod`, and `BareNameArtistResolver` does not emit one.** Its `ArtistResolveMethod` has exactly two values — `identity_store`, `api_search` — and the wire's `IdentityMethod` has eight (`manual`, `cross_source_agreement`, `exact_match`, `name_variation`, `member_group`, `alias_match`, `trigram`, `llm`). The two enums are **completely disjoint**; neither value is expressible on the wire. What *does* map cleanly is the finer-grained `ArtistResolveCacheLeg` (`cache_exact`, `cache_member`, `cache_alias`, `cache_name_variation`, `cache_trigram` → `exact_match`, `member_group`, `alias_match`, `name_variation`, `trigram`), which is currently carried as `cache_corroboration` telemetry rather than as the deciding method.

Decision: the column stores an **`IdentityMethod`**, derived from the resolver's cache leg where one is present (`cache_exact`→`exact_match`, `cache_member`→`member_group`, `cache_alias`→`alias_match`, `cache_name_variation`→`name_variation`, `cache_trigram`→`trigram`), falling back to `exact_match` for both a tier-1 `identity_store` hit and an uncorroborated `api_search` hit. The mapping is one explicit table in code, under test — the same place as the confidence mapping below. Storing `ArtistResolveMethod` verbatim would push an unrepresentable value into #1021 and force a translation layer at wire-compose time, where the evidence needed to translate it well is gone.

**`api_search` maps to `exact_match`, not `trigram`** — the tempting fallback is wrong twice over. The API tier resolves on *exact-form* candidates: `candidate_count` is documented as "1 on resolved via `api_search`", with ≥2 forcing an `ambiguous` verdict rather than a resolution. And an empty `cache_corroboration` is not evidence of fuzziness — the contract states it is **always** empty on `identity_store` short-circuits and on qualifier-bearing inputs regardless of match quality, because the cache tier was never consulted. So "no leg corroborates" means "not measured," not "matched loosely." Mapping it to `trigram` would stamp LML's strongest Discogs evidence with the weakest method and — via the method→confidence table — its lowest confidence. `trigram` is reserved for an actual `cache_trigram` corroboration.

**Confidence floors.** §2 asks for stricter floors than album-level and a confidence-matrix review in the PR. `BareNameArtistResolver` returns a method, not a raw confidence; the plan maps method → confidence via the same explicit table (one place, reviewable), pitched at or above the album-level values, with the mapping under test. The matrix goes in the PR body per the AC.

### D4 — credit splitting is a recall tactic, not a grain change

`ft.` / `feat.` / `&` / `,` splitting resolves as: **attempt the whole credit string first**, because joint credits are frequently real Discogs entities ("Duke Ellington & John Coltrane" is one artist page, and the live-repro's "Brian Reitzell And Roger J. Manning, Jr." may be too). Only on a whole-string miss do we split and attempt each side.

The stored grain stays one row per CTA credit — the verdict from whichever attempt succeeded is recorded against the original credit string, and the key does not change. A split that resolves each side separately records the whole-credit row as resolved with the method that got there; it does not manufacture extra rows the CTA corpus does not have, and therefore cannot desynchronize from #297's join-back key (which echoes the CTA credit verbatim).

### D5 — position recovery is a local join against the #1019 recall index

For each credit, join `lml_cache.compilation_track_location` on `(library_id, track_artist, track_title)` — all three already normalized identically on both sides (`to_match_form`), and `compilation_track_location.track_position` is already NOT NULL.

**The join is not 1:1 and must be collapsed explicitly.** The recall index's PK is `(library_id, track_position, track_artist)` — `track_title` is *not* in it (`entity/compilation_track_location.py:83`). So one `(library_id, track_artist, track_title)` triple legitimately matches several rows: the same artist credited at two positions on one comp, or two distinct titles that collapse to the same `to_match_form`. A plain `LEFT JOIN` fans out, and the fan-out then lands as conflicting inserts on this table's PK from a single credit — a self-inflicted upsert storm where the "winner" depends on row order.

The join is therefore a `LEFT JOIN LATERAL (… ORDER BY track_position LIMIT 1)`: a deterministic single-row pick, ordered so re-runs are stable. Where the join misses entirely (comp never matched a release, or the credit exists in CTA but not in the Discogs tracklist), `track_position` stays NULL.

**What gets recovered is sometimes a synthetic ordinal, not a vinyl-side position.** `build_rows` in the recall-index builder falls back to the track *sequence number* when Discogs carries no `position` string, so a recovered `"7"` may mean "seventh track" rather than a printed position. The contract describes `track_position` as "matches the request input", so this is worth saying out loud rather than letting a downstream reader assume Discogs-verbatim positions. It does not change the design — a sequence ordinal is still better than NULL for ordering — but it belongs in the column comment and in #1021's handoff.

**The join spans two different corpora, and shared normalization does not close that gap.** The recall index enumerates credits from Discogs `release_track_artist`; this matcher enumerates credits from `library.db`'s `compilation_track_artist`, which is MySQL-curated free text entered by WXYC librarians. Running `to_match_form` on both sides makes the comparison *fair*, not *successful* — a librarian's "Chico Science & Nação Zumbi" and Discogs's own tracklist spelling still have to agree as strings after folding. So position yield is bounded by **two independent things**, and the backfill reports them as two separate stats rather than one:

1. recall-index coverage (did the comp match a Discogs release at all), and
2. CTA↔Discogs credit-string agreement within a covered comp.

Conflating them would make a string-agreement problem look like a coverage problem and send the fix in the wrong direction. If (2) turns out to dominate, the cheaper lever is to derive the *credit population itself* from the recall index where a comp is covered — the strings then agree by construction — and fall back to CTA enumeration only for uncovered comps. That is a slice-4 measurement, not a decision to take up front, and it is recorded here so the measurement has somewhere to land.

This costs one lateral join and zero external calls.

### D6 — script retirement, carefully

- **`scripts/merge_cta.py`** — deleted outright. It SSH-writes to tubafrenzy MySQL, which the pivot's "no cross-service writes" rule forbids and the turndown removes.
- **`scripts/match_compilations.py`** — **not** deletable as written. Seven names are imported across four call sites, not the two the first draft of this plan claimed:

  | Call site | Imports |
  |---|---|
  | `scripts/build_compilation_track_location.py:58-64` | `CompAlbum`, `exact_match`, `prefix_strip_match`, `trigram_match`, `normalize_comp_title` |
  | `tests/unit/test_build_compilation_track_location.py:24` | `DiscogsMatch` |
  | `tests/unit/test_extra_zero_filter.py:136` | `DiscogsMatch`, `enrich_with_track_artists` |
  | `tests/unit/test_match_compilations_normalize.py:15` | `normalize_comp_title` (pins the normalization behavior) |

  The matcher functions move to **`scripts/_lib/release_matching.py`** — that package already exists as the shared-script-helper home (`csv_ids.py`, `runtime.py`, `signals.py`), and both consumers (`build_compilation_track_location.py` and the new backfill) are scripts. The earlier draft proposed a new top-level `compilations/` package; that is not justified for four moved functions when an established location fits. The JSON-writing CLI wrapper is deleted. All four call sites move with it, plus three prose references that name the module in docstrings — `scripts/build_compilation_track_location.py:9` and `:161`, and `tests/integration/test_build_compilation_track_location_pg.py:8`.

- **The move changes the code's type-checking status, and that is a deliverable, not a side effect.** `pyproject.toml:112-124` lists both `scripts.match_compilations` and `scripts.merge_cta` under `[[tool.mypy.overrides]] ignore_errors = true`, directly beneath a comment stating that new pipeline scripts should remain fully typed. Relocating to `scripts/_lib/release_matching.py` puts the code under full enforcement — `scripts._lib` is not in that override list. Slice 6 therefore removes **both** override entries and budgets a typing pass over the moved module; silently carrying the override forward under a new module name would defeat the block's own stated intent.

- **An older plan schedules `match_compilations.py` for a *port*, which contradicts moving it.** `docs/plans/streaming-availability-pg-migration.md:40` lists it as "Port (read-only; outputs CSV/SQL files)" and `:174` puts it in PR E's script-port batch; `:43` lists `merge_cta.py` among archived one-offs to leave alone. Slice 6 adds a superseding note to that plan's PR-E line rather than leaving two plans pointing opposite directions at the same file. The prose sweep also covers `tests/unit/test_extra_zero_filter.py:4,127` and `tests/unit/test_match_compilations_normalize.py:1`.

- **`docs/architecture.md` needs a bullet too.** Slice 6's doc list named only `docs/scripts.md` and the CLAUDE.md router line, but `docs/architecture.md:61` and its `entity/*` bullets around `:86` already carry the sibling store module in the key-files list; the new one belongs there.

- **The plan file must land in the same commit as any module docstring citing it.** `scripts/check_plan_links.sh` (the LML#1124 guard) resolves plan links against the git index, and this file is currently untracked.

  `scripts/_lib` is also the cheaper destination for a second reason: it is already covered by the `scripts*` packaging glob (`pyproject.toml:88`), so `tests/unit/test_packaging_manifest.py` stays green. A new top-level `compilations/` package would **not** have been — that test asserts every `__init__.py`-carrying top-level directory appears in `include`.
- `docs/scripts.md` has **no** section for `match_compilations.py`, `merge_cta.py`, or `build_compilation_track_location.py`, so there is nothing to remove there — the earlier "update it for both" framing was a no-op. The real doc work is additive: a new `## Compilation Track Identity Backfill (\`scripts/backfill_compilation_track_identity.py\`)` section following the file's one-section-per-script convention, plus the `docs/scripts.md` router line in `CLAUDE.md`, which enumerates the scripts it covers.

## Contract dependency — what this plan does and does not need from #297

Worth stating precisely, because it is easy to over- or under-claim.

**This plan needs nothing from wxyc-shared#297.** The storage table's nullable `track_position`, `confidence`, `method`, and `resolved_artist_name` are internal `lml_cache` columns; they are constrained by D1's CHECK, not by `api.yaml`. Slices 1–6 can all land against today's committed `generated/api_models.py`.

**#1021 needs #297, and today's committed models cannot express what this table stores.** `generated/api_models.py:1447` has `BulkResolveTrackIdentity.track_position: str` — required and non-nullable — with no `track_artist` / `track_title` join-back keys, and `BulkResolveProvenanceEntry.confidence` (line 1436) is likewise required. So an attempt row (D2) and a position-less credit (F1, D5) are both **unrepresentable on the wire** until wxyc-shared#297 merges and LML regenerates. That work is staged — [wxyc-shared PR#300](https://github.com/WXYC/wxyc-shared/pull/300) plus the `chore/regen-api-models-1-30-0` branch — but neither has landed, and the regen branch sits on the same base and does not change these fields yet.

A third field belongs on that list: **`method`**. `BulkResolveProvenanceEntry.method` is a required `IdentityMethod`, and per D3 the resolver's native `ArtistResolveMethod` cannot satisfy it. This plan resolves that at *storage* time (the column holds an `IdentityMethod`), so #1021 inherits a wire-ready value rather than a translation problem — but the mapping's existence, and the fact that it is lossy relative to the resolver's own vocabulary, is part of the handoff.

Practical consequence: do not let #1021 start against the current models and discover any of this. The handoff note to #1021 carries all three — nullable `track_position`, nullable `confidence`, and the `method` mapping — alongside F2's key-space warning and D5's synthetic-ordinal caveat.

## Implementation slices

TDD throughout — failing test, then implementation, per `docs/testing.md`. Each slice is independently reviewable; slices 1–3 are one PR, 4–5 a second, if the diff runs long.

1. **Schema — four registration surfaces, not one.** Every `lml_cache` table pays the same toll, and the sibling shows exactly where:
   - `entity/compilation_track_identity.py` (the runtime source of truth, via `bootstrap_lml_cache_table`) + its generated `.sql`.
   - A `SidecarSpec` entry in `scripts/regenerate_lml_cache_sql.py` (the `compilation_track_location` entry at lines 133–175 is the template; it pins exact statement-prefix keys, so the DDL string constants must match byte-for-byte).
   - A slot in `main.py`'s advisory-lock bootstrap block (`main.py:348-450`), **and** a direct `set_up_compilation_track_identity_schema` call from the backfill script. Self-sufficiency is an explicit design point of the sibling module, not an accident: the standalone process runs outside the FastAPI service and cannot assume the lifespan ever ran (`scripts/build_compilation_track_location.py:57` is the precedent).
   - **Two** new schema tests, named for *this* module per the generated-header convention in `scripts/regenerate_lml_cache_sql.py` (`tests/unit/test_{module_name}_schema.py`): `tests/unit/test_compilation_track_identity_schema.py` (mocked PG — statement order + `.sql` parity) and `tests/integration/test_compilation_track_identity_schema.py` (`pg`-marked, home of the EXPLAIN assertion, which targets `idx_compilation_track_identity_misses` via a `WHERE external_id IS NULL` retry-cohort query — the one access path the PK does not already serve). The identically-named `..._location_...` files are the sibling's and stay untouched.

   **Decide `advisory_key` explicitly rather than by omission.** `entity/ddl.py:204-213` says to pass it "only for a table with a genuine non-lifespan caller" — which is exactly the lifespan + direct-script-call shape slice 1 creates. The sibling declines it and says why (`entity/compilation_track_location.py:100-108`: DDL is entirely `IF NOT EXISTS`, no seeded-row ordering between the two call sites). This table has the same properties, so it declines too — but the one-sentence rationale goes in the module docstring so the next reader doesn't re-litigate it.

   Two prose counters also go stale on the eighth sidecar and belong on this checklist — neither breaks CI (the drift check is `git diff --exit-code`), which is exactly why they get missed: `scripts/regenerate_lml_cache_sql.py:1` ("seven `lml_cache.*` DDL sidecars") and `.github/workflows/ci.yml:58-62` ("the shared one for its seven siblings … cover all 8").

   Also in this slice: the F2 key-space test, asserting the documented semantic of `library_id` with a fixture whose library.db id and Backend id differ, proving the column holds the former.
2. **Store read/write — all three helpers in `entity/compilation_track_identity.py`**, colocated with the DDL exactly as the sibling colocates its read helper (`entity/compilation_track_location.py:130-176`): the two-statement writer (D1), the `WHERE external_id IS NULL` retry-cohort reader, and the per-`library_id` read #1021 will consume.
3. **Matcher — inside `scripts/backfill_compilation_track_identity.py`**, with its only caller, exactly as `build_compilation_track_location.py` keeps all its population logic in the script. Two earlier drafts got this wrong in opposite directions: `identity/` (rejected — that package is request-path code, and #1021 reads the table through the slice-2 store helper, never through the matcher) and `scripts/_lib/` (rejected — that package's own docstring scopes it to "shared helpers… extracted to remove cross-script duplication," and every current member has multiple consumers; a single-caller module doesn't qualify). The mypy argument doesn't discriminate either way — the backfill script is equally absent from the `pyproject.toml:112-124` override list. D6's `release_matching.py` move to `_lib` is unaffected: that one genuinely has two consumers.

   Contents: credit extraction from `library.db` CTA (with the F1 fail-fast shape check), the D4 split cascade, the HTTP call to `/api/v1/artists/resolve/bulk` (F3), the MB leg, and the method→confidence mapping.

   **The MB leg builds its own `PgSource`.** `core/dependencies.py:381-391`'s `get_musicbrainz_pg` is `Depends`-shaped and delegates to a private singleton — unusable outside the service. The drain constructs a `PgSource` from `settings.database_url_musicbrainz` directly, the same way `build_compilation_track_location.py` builds its own pool. That is also where "unconfigured ⇒ no row + loud startup log" concretely lives. Unit tests for the split cases (`ft.`/`feat.`/`&`/`,` + the joint-credit live-repro), a test that an invalid credit yields a miss row rather than aborting the batch, a test that an `escalation_unavailable` verdict writes **no** `discogs` row (the mirror of the MB-unconfigured test), a test that a `library.db` missing the CTA `track_title` column fails fast at startup rather than mid-enumeration, a test that a **tier-1 `identity_store` resolution** (non-NULL id, NULL `canonical_name`) inserts without tripping the coherence CHECK, a test pinning `api_search` → `exact_match`, a test that an MB PG error writes no `musicbrainz` row (distinct from an MB miss), and a test proving the drain makes **no direct Discogs calls at all** (F3 — stronger than the "no second client" the AC literally asks for).

   The MB-touching integration test needs a skip condition: the `pg` CI job runs a bare `postgres:16-alpine` with no MusicBrainz schema (`docs/testing.md:58`), so it self-skips without a `DATABASE_URL_MUSICBRAINZ` DSN, following `tests/integration/test_va_discogs_lookup.py`.
4. **Position recovery.** The D5 lateral join, plus the two coverage stats it needs to report separately (recall-index coverage vs CTA↔Discogs string agreement). Four fixtures: a CTA credit with no position gains one; a credit absent from the tracklist stays NULL; a credit matching **two** recall-index rows (same artist at two positions) resolves to one deterministic row and stays stable across re-runs; and — the case the round-2 upsert bug would have shipped — an **already-resolved** row gains a position on a later run while its verdict columns stay byte-identical.
5. **Backfill script.** `scripts/backfill_compilation_track_identity.py`. Two precedents, and the second matters more than the first: `build_compilation_track_location.py` for the CLI shape (`--incremental` / `--full` / `--retry-misses` (the `external_id IS NULL` cohort) / `--limit` / `--dry-run`), and **`scripts/artist_resolve_drain/`** (`docs/scripts.md:204-230`) for the operational shape. The reuse is by **import, not by imitation** — that module already exposes an injectable core, and reimplementing ~400 lines of paging and retry would be the wrong kind of homage:

   | Imported | New here |
   |---|---|
   | `run_drain` (injected `post_batch`/`sleep`/`clock`), `make_post_batch`, `resolve_batch`, `PAGE_SIZE`, `_TERMINAL_REASONS`, `report.py`'s spot-check sampler | The **fan-back**, and the per-source write |

   The fan-back is the genuinely new part and the reason a wrapper exists at all: the sibling drain keys on distinct *names* and stops there, while this drain must map one resolved name back to **N `(library_id, track_title)` credit rows** — the same artist credited on many compilations. That mapping, plus the D1 two-statement write and the MB leg, is the wrapper's whole job.

   A `pg`-marked integration test drains a fixture-scale population. Logging + error handling per the org's long-running-script rule.

   **Two orthogonal switches, two distinct names** — an earlier draft used "dry run" for both, which is ambiguous about whether a default run populates the new table at all:

   | Flag | Controls | Default |
   |---|---|---|
   | `--dry-run` | Whether the drain writes `lml_cache.compilation_track_identity` rows | off (i.e. **rows are written**) |
   | `--live` | Whether the endpoint mints into `entity.identity` | off (i.e. **no minting**) |

   `--live` matches the sibling's spelling (`docs/scripts.md:216-228`). So a default run populates this plan's own table and mints nothing — the useful default, and the safe one.

   **Mint policy, decided here rather than deferred: no minting unless `--live`.** The endpoint's dry-run mode runs every tier identically but skips the `entity.identity` upsert. This is not a stylistic default — `entity.identity` is **discogs-cache-owned** (CLAUDE.md, PostgreSQL schema ownership), so a 100k-credit drain that mints is a cross-repo write into another repo's contract table, and the sibling drain's runbook exists because a wrong mint is COALESCE-never-clobber and does not self-correct. A per-track credit is also weaker evidence than the curated artist names that drain was built for, which argues for minting less freely here, not more.

   **Sizing is a prerequisite, not a footnote — the inherited runbook was written for a different order of magnitude.** `scripts/artist_resolve_drain/drain.py:34-38` records the arithmetic: `PAGE_SIZE = 25`, and a fully-escalating page ≈ 25 API calls ≈ 30s against the shared 50/min budget. At that rate a naive pass over ~140K CTA credits is on the order of **tens of hours**, which is not "one off-peak session" — and `docs/scripts.md:206` inherits a single off-peak window sized for a few thousand curated names.

   The lever is **distinct credit strings**, not credit rows: the endpoint dedupes only *within* a batch, so cross-batch dedup is the drain's job and is where the order of magnitude actually falls (one artist credited across many comps collapses to one resolve). That estimate is a one-query measurement against `library.db`, and it belongs in the **same pre-slice-5 measurement pass as D3's MB expression-index check**. Slice 5 also needs a per-session budget and an explicit stop condition, so a run ends at a planned boundary rather than whenever someone notices.

   **Where a real run points follows directly from F3.** `docs/scripts.md:206` records that the artist drain "always runs against production, not staging": prod is where the single coordinated limiter, the semaphore, and the LML#755 breaker actually live. Since the drain now drives the prod HTTP endpoint rather than resolving in-process, this is inherited rather than re-argued. The fixture-scale `pg` test is unaffected; any non-fixture run inherits the sibling's prod-only + off-peak-window constraint and is part of the out-of-scope human-triggered step.

   **`--incremental` is evaluated per `(library_id, track_artist, track_title, source)`, not per credit.** The grain matters because D3 writes *no* `musicbrainz` row when `DATABASE_URL_MUSICBRAINZ` is unset. A per-credit cohort ("any row exists for this credit") would see the Discogs row, call the credit attempted, and make MB coverage unbackfillable forever once the env var lands. Pinned by a test that drains MB-unconfigured, then MB-configured, and asserts the second run writes MB rows for credits the first run already resolved on Discogs.
6. **Retirement + docs** per D6.

## Explicitly out of scope

- **The production drain.** Per amendment #4, this run delivers the script and its fixture-scale test; executing against prod is a human-triggered post-merge step (`risk:prod-write`).
- **Populating `tracks[]` in bulk-resolve** — that is #1021, and it is separately blocked on wxyc-shared#297.
- **Composition rules** (cross-source boost / MIN fallback) — #1021, at wire-compose time (D3).
- **The Backend↔legacy `library_id` mapping** — #1021's read side (F2). This plan documents and pins the boundary; it does not cross it.
- **Non-V/A tracks** — LML#1138, whose D13 resolution is "no new store, derive at read time". Nothing here should be built to accommodate it.

## Risks

| Risk | Mitigation |
|---|---|
| F2's key-space trap is re-sprung in #1021 | Column comment + dedicated test + a note on #1021 before it starts |
| Position coverage silently poor | Bounded by two independent things — recall-index coverage *and* CTA↔Discogs string agreement (D5). Backfill reports both separately so the fix targets the right one |
| MB trigram leg records a coin-flip between near-tied candidates as a permanent resolved identity | Similarity floor + deterministic tie-break + ambiguity→miss rule (D3); the upsert guard makes a wrong resolution permanent, so ambiguity must fail closed |
| MB `%` predicate seq-scans the drain because musicbrainz-cache lacks the expression indexes | Verify the indexes exist on the drain target **before** sizing slice 5; if absent it is a musicbrainz-cache prerequisite (D3) |
| Confidence floors admit track-credit false positives | Explicit method→confidence table under test; matrix review in the PR body per the AC |
| `BareNameArtistResolver` reuse mints into `entity.identity` — a **discogs-cache-owned** table — across 100k credits | `dry_run=True` by default, live mint behind an explicit flag (slice 5). A wrong mint is COALESCE-never-clobber and does not self-correct |
| An outage mid-drain is recorded as "asked and found nothing," and D10's semantics then let BS#1991 exit its sweep on credits the source never saw | One rule, not four cases: *not successfully consulted* ⇒ no row for that source; *answered with nothing* ⇒ miss row (D3). Covers Discogs shed/429, MB error, MB unconfigured |
| The drain becomes an N+1th uncoordinated Discogs limiter and 429-trips the LML#755 breaker for live lookups | It makes no direct Discogs calls — it drives `POST /api/v1/artists/resolve/bulk` against prod, where the limiter, semaphore, and breaker actually live (F3). The shared PG rate bucket is default-OFF, so in-process reuse would **not** have been coordinated |
| A PG outage mid-drain gets recorded as a mass of legitimate misses that `--retry-misses` then declines to revisit | `CacheUnavailableError` / `PostgresError` abort the run rather than being caught per-credit (D3); only `InvalidNameError` becomes a miss row |
| A later miss silently nulls out an earlier verdict | Verdict and position are separate statements with separate guards (D1). This plan produced a variant of this bug in each of its first two drafts; the split is what makes it structurally unavailable rather than merely avoided |
| `DATABASE_URL_MUSICBRAINZ` unset in the drain environment silently halves coverage | MB-unconfigured writes no `musicbrainz` row (never a miss row), logs loudly at startup, and reports MB coverage as a separate backfill stat (D3) |
