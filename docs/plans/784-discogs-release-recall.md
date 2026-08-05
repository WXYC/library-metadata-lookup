# LML#784 — Discogs release recall gap: multi-artist credits, V/A-comp features, typos, generic "S/T"

Issue: https://github.com/WXYC/library-metadata-lookup/issues/784 (root causes already verified on-ticket at deployed commit 78405787; this plan is the implementation sizing it calls for).

## Problem recap (per verified category)

1. **Multi-artist "A & B"** — the PG cache arm of `DiscogsService.search()` presents one `release_artist` credit per row (`ra.extra = 0`), so a joined credit like "Fust, Merce Lemon" surfaces as "Merce Lemon" or "Fust" singly and floor-fails at 80/80 (`find_best_typed_match`). The `fallthrough` seam treats any non-empty `pg_read` as terminal, so the API arm — which resolves both verified pairs today at artist 91.4/92.3, album 100 — is never consulted. The cache masks a working API arm.
2. **V/A comps** — retrieval succeeds (fuzzy `q=` arm), but scoring structurally cannot pass: the release-level credit is "Various" (artist axis ~27) and the full comp title carries a subtitle the query never has (album axis ~26 under `token_sort_ratio`). The performer evidence lives in the tracklist or embedded in the release title — neither axis exploits it. `token_set_ratio` would clear the album axis but is exactly the #719 subset-inflation shape the guards reject.
3. **Typo** ("Horrace" → "Horace") — scoring is fine (artist 100, album 97.4); the Discogs API returns zero on both the strict and fuzzy arms. The PG trigram tier is the only typo-tolerant tier — the inverse of category 1: the cache is the rescue, not the mask. Whether prod's cache holds the release (or masked with floor-failing rows) is a prod probe, not a code question.
4. **Generic "S/T"** — `is_self_titled` exists and is applied to library-side titles in three consumers, but nothing swaps a query-side "S/T" for the artist name in `_library_miss_discogs_search`, so the probe searches `release_title=S/T` and deterministically no-matches.

## Constraints (from the ticket)

- Must not regress the precision guards: #592 (80/80 floor), #719/#721 (`token_set_ratio` subset-inflation guard). Raising recall must not re-admit those false positives.
- Data safety on any backfill: only touch rows currently unresolved; never overwrite a verified match.
- Respect the Discogs pacing envelope (LML limiter + #755 breaker; BS mirrors the ceilings).
- `lookup/` module budgets (`test_module_budgets.py`): `library_miss.py` has a 150-line ceiling (currently 80); any new `lookup/` file needs a deliberate budget entry.
- TDD required (docs/testing.md): failing test first, per category; unit + integration coverage per the bug-fix protocol.

## Design

### PR A — categories 1, 3, 4 (branch `784-library-miss-recall-pr-a`, off `main`)

**Commit A1 — floor-reject fall-through (category 1 core, also rescues Population B's Ricardo Dias Gomes).**

- Add `skip_pg: bool = False` kwarg to `DiscogsService.search()` (`discogs/service.py`). When true, bypass the `fallthrough` seam and call the existing `_api_fetch` under `request_context("search", "skip")` — same shape as the `cache is None` branch, but tagged with the seam's existing `"skip"` cache-state (the LML#537 span taxonomy already defines it for cache-bypass calls), so the retry is distinguishable from `"no_pg"` in the wait-time histograms. No behavior change for existing callers (`lookup/artwork.py`, `lookup/validation.py`, `discogs/lookup.py`).
- In `_library_miss_discogs_search` (`lookup/strategies/library_miss.py`): when `find_best_typed_match` returns `None` **and** `response.cached is True` (i.e. the candidates came from the PG arm — the seam sets `cached=False` on every API-served path), re-issue the same search with `skip_pg=True` and re-apply the floor. This redefines "hit" as "usable candidate" for this caller only, without touching the seam's semantics for artwork/validation callers or moving caller-specific floor knowledge into `DiscogsService`.
- Cost bound: at most one extra Discogs API search per library-miss lookup whose PG candidates all floor-fail. Library-miss is the tail path (fires only when the entire library pipeline returned nothing); the existing token bucket + #755 breaker bound the added load. No new env flag — the breaker is the kill switch; if review disagrees, a `LML_LIBRARY_MISS_API_RETRY` settings gate is a 5-line addition.
- Telemetry: `logger.info` + reuse the search seam's existing breadcrumbs; no new counters in this PR.

**Commit A2 — PG arm presents the joined credit (category 1 in-cache resolution, saves the A1 API call when the release is cached).**

- `CacheService.search_releases` (`discogs/cache_service.py`): keep per-credit trigram retrieval and per-credit ranking (`DISTINCT ON (r.id)` keeps the best-matching credit's similarity score), but present the aggregated credit via a `LATERAL` subquery: `string_agg(ra2.artist_name, ', ' ORDER BY ra2.artist_name)` as `artist_name` plus `array_agg(...)` as `artist_credits`, over the release's `extra = 0` rows. Ordering is alphabetical for determinism only — `token_sort_ratio` is order-insensitive, so scoring is unaffected. Row dict gains `artist_credits: list[str]`.
- `DiscogsSearchResult` (`discogs/models.py`) gains optional `artist_credits: list[str] | None = None`; `_pg_read` populates it; the API arm leaves it `None` (Discogs search results carry only the joined display title).
- **Candidate-side artist variants** in `find_best_typed_match` (`clients/streaming/matching.py`): allow `artist_fn` to return `str | None | Iterable[str]`; score the artist axis as the max across the candidate's variants (mirroring the existing query-side variant support). The change must be backward-compatible for plain-`str` extractors — `find_best_typed_match` has exactly two production call sites (`lookup/strategies/library_miss.py:65`, `lookup/artwork.py:267`), both of which will pass `artist_fn=lambda r: [r.artist, *(r.artist_credits or [])]`, but the plain-`str` contract is pinned with a dedicated non-regression test (single-credit candidate + `str` extractor scores identically before/after). This is what prevents the one regression the aggregation could introduce: a single-credit query (library item artist "Fust") scoring against a now-joined candidate ("Fust, Merce Lemon") would floor-fail without the per-credit variant. Max-over-variants can only admit candidates whose credit list genuinely contains the queried artist — not a #592 collision shape (the artist is a real credit of the release), and the album axis is untouched, so no #719 exposure.

**Category 3 — no dedicated code.** With A1+A2: if prod's cache holds the release, the trigram tier already retrieves the typo'd title and the floor passes (97.4); if the cache surfaces only floor-failing rows, A1 falls through to the API (which returns zero for typos — unchanged, nothing can fix that arm); if the release isn't cached, the outcome is today's, and the fix is the discogs-etl cache build, not LML. Pin with a `pg`-marked integration test: seed "Best Of Horace Andy", search "Best of Horrace Andy", assert resolution via the trigram tier. Documented on the ticket as the category-3 disposition.

**Commit A3 — query-side S/T swap (category 4).**

- In `_library_miss_discogs_search`: when `is_self_titled(album)` (import from `lookup.matching`), search with `DiscogsSearchRequest(album=artist, artist=artist)` and score the title axis against the artist name only — mirroring `lookup/artwork.py:219`'s established pattern, including its warning not to re-admit the "S/t" trigger string as a variant (a wrong-release candidate literally titled "S/T" would trivially clear).

**PR A test matrix (write failing-first, per commit):** tests land with the commit they verify — A1 rows cover the retry logic, A2 rows the aggregation + variant scoring, A3 rows the S/T swap; the #592/#719/#721 guard suites re-run after the final commit.

| Test | File | Marker |
|---|---|---|
| PG candidates floor-fail + `cached=True` → second `search(skip_pg=True)` call → resolves (Merce Lemon & Fust → 36830641, per-credit cache rows vs joined API rows) | `tests/unit/test_library_miss_discogs.py` | — |
| No retry when floor passes on the cache arm; no retry when `cached=False` (API already consulted); retry response that still floor-fails → `None` | same | — |
| Anadol & Marie Klock resolves (accept 37483479 or 37552383, per the ticket's alternate-pressing note) | same | — |
| Ricardo Dias Gomes — Muito Sol resolves via retry (100/100 API shape) | same | — |
| `skip_pg=True` bypasses `pg_read` and still works with `cache_service=None` | `tests/unit/test_discogs_service.py` | — |
| `search_releases` returns joined `artist_name` + `artist_credits` for a two-credit release; single-credit releases unchanged; ranking still per-credit | `tests/integration/` (seeded `release`/`release_artist` per `test_cache_service_tombstones.py` fixture shape) | `pg` |
| Typo pin: seeded "Best Of Horace Andy", query "Best of Horrace Andy" resolves via trigram tier | same file | `pg` |
| `find_best_typed_match` candidate-side variants: max-over, empty/None variant handling, no behavior change for plain-`str` extractors | `tests/unit/test_streaming_matching.py` | — |
| Artwork non-regression: query artist "Fust" passes via `artist_credits` against joined candidate | `tests/unit/` (artwork or matching suite) | — |
| S/T: query ("Matmos", "S/T") searches `release_title=Matmos`, scores title vs artist name, resolves 63794-shaped candidate; "S/T"-titled wrong-artist candidate rejected; non-self-titled path untouched | `tests/unit/test_library_miss_discogs.py` | — |
| #592/#719/#721 guard suites untouched and green (`test_streaming_matching.py`, `test_album_match_floor.py`, LML#400 contamination pins in `test_library_miss_discogs.py`) | existing | — |

### PR B — category 2, V/A-comp rescue (branch `784-va-comp-rescue-pr-b`, stacked on PR A)

New module `lookup/strategies/va_rescue.py` — PR B's first commit adds `"lookup/strategies/va_rescue.py": 200` to `MODULE_BUDGETS` in `tests/unit/test_module_budgets.py` alongside the module (the guardrail fails on any unbudgeted `lookup/` file). Called from `_library_miss_discogs_search` only after the standard floor (and PR A's API retry) found nothing:

- **Gate**: skip entirely when `is_compilation_artist(parsed.artist)` — a "Various" query has no artist signal to verify against (#638/#592 concern).
- Consider only candidates where `is_compilation_artist(candidate.artist)`. For each, in confidence order:
  - **Album axis** (floor 80, `score_match` only — never `token_set_ratio`, so no #719 exposure): pass if any of {full title, left-cumulative " - "-split segments, each with and without one trailing parenthetical stripped} clears vs `parsed.album`. Bounded transformations: delimiter-split segments and a single end-of-string `(...)` strip — no token-subset scoring anywhere.
  - **Artist axis** (floor 80): pass if any " - " segment of the candidate title clears vs `parsed.artist` (the Perry shape: performer embedded in the release title — resolves 632150, the ticket's documented-acceptable substitute for 316672); otherwise fetch the release once via the existing `get_release` read-through and pass if any track-level artist credit clears (the Tasquier shape: featured artist in the tracklist → 27518829). Fetch only for candidates that already passed the album axis (≤5, usually 1); failures/breaker sheds skip the candidate.
  - Both axes must pass; first passing candidate wins (input order = confidence order).
- Synthesized result flows through the same `LibraryItem(id=0)` + `DiscogsSearchResult` seam as the existing library-miss hit (no orchestrator changes beyond what PR A already touches).
- `LML_RESOLVE_NONLIBRARY_RELEASE` is *not* in this path (it gates `track_on_compilation`'s rowless resolution); confirm the prod Railway value anyway as the ticket asks, since category 2's song-bearing siblings route there.

**PR B test matrix:** Perry shape (segment-embedded performer, no tracklist fetch needed — assert `get_release` not called); Tasquier shape (tracklist-credit rescue, mocked `get_release`); query-artist-compilation gate; both-axes requirement (album passes + artist fails → reject, and vice versa); #719-style shape must not pass the album axis (`"Hound Dog"` vs `"Black Leather - The Hound Dog Mix"`-style segments); breaker/fetch-failure skips candidate gracefully; module-budget entry. Optional `external_api`-marked replay of one pair (self-skipping without `DISCOGS_TOKEN`).

### Ops phase (no PR; results recorded on the ticket)

1. **Category-1 masking confirmation (acceptance criterion)**: read-only `search_releases` trigram SQL for the affected pairs against the prod discogs-cache (creds via Railway variables / the EC2 recipe), or replay the pairs against prod `/lookup` pre-deploy. Excludes the transient-outage alternative the ticket flags.
2. **`LML_RESOLVE_NONLIBRARY_RELEASE` prod value**: read from Railway service variables; record.
3. **Corpus prevalence**: sample ~150 rows from the BS#1443 `enriched_no_match` cohort (22,773 rows), re-verify Discogs existence via the public API with artist-scoped search (the ticket's audit method), respecting pacing. Record the rate + extrapolated blast radius on the ticket. Script runs ad hoc (scratchpad); committed to `scripts/` + `docs/scripts.md` only if it earns reuse.
4. **Post-deploy verification**: replay the 4 Population-A pairs against staging (`main` auto-deploys there) and assert the verified release IDs (or documented substitutes: Anadol 37483479, Perry 632150). Prod deploy is a `prod`-branch push — coordinate, don't push unilaterally.
5. **Backfill decision**: with prevalence known, record on the ticket whether re-enriching the BS#1443 cohort is warranted now or deferred; any backfill only touches currently-unresolved rows (data-safety constraint) and is BS-side work coordinated on BS#1443.

## Acceptance-criteria mapping

- Category-1 masking confirmed against prod cache → Ops 1.
- 4 Population-A pairs resolve via `/lookup` + regression test per shape → PR A (pairs 1–2) + PR B (pairs 3–4), staging replay in Ops 4.
- Corpus prevalence quantified → Ops 3.
- No regression of #592/#719/#721 → their suites run green in both PRs; no `token_set_ratio` introduced anywhere; floors stay at 80.
- Backfill decision recorded → Ops 5.

## Risks & mitigations

- **Added Discogs API volume from A1**: bounded to the library-miss tail; existing limiter/breaker apply; watch the #537 wait-time histograms post-deploy.
- **A2 changes what `search()`'s PG arm presents to *all* its callers** (`artwork`, `validation`): `validation.py` uses `album_title_acceptable` on the album axis only (artist presentation irrelevant); `artwork.py` is covered by the candidate-side variant scoring; both get non-regression tests.
- **V/A rescue precision**: every guard is structural (delimiter-bounded segments, single parenthetical strip, dual-axis floors, V/A-candidates-only, non-V/A-query-only, tail-path-only). The known deliberate looseness — resolving an alternate comp like Perry's 632150 — is explicitly sanctioned by the ticket.
- **PR size**: PR A ≈ 500–700 changed lines incl. tests; PR B ≈ 400–500. Both under the 1000-line preference; stacked to keep review focused.
