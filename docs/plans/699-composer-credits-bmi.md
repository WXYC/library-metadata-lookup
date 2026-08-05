# LML#699 — Expose songwriter/composer credits for BMI reporting

**Ticket:** https://github.com/WXYC/library-metadata-lookup/issues/699 (labels `enhancement`, `concern:contract-change`)
**Downstream companions (separate, blocked-by this):** Backend-Service#1499 (add `flowsheet.composer` + CDC populate), Backend-Service#1500 (BMI export successor to tubafrenzy `recentBMI`).
**Org project:** Tubafrenzy decommissioning (#36).

## Goal

WXYC must keep submitting BMI performance lists (songwriter/composer per played track) after the 2026-08-31 tubafrenzy turndown. The composer data already exists in the discogs-cache (`release_track_artist` / `release_artist` writer-role credits) and already flows into LML at resolution time — it's just discarded. This surfaces the writer-role subset as a dedicated, provenance-tagged field on the lookup response so the CDC enrichment-worker can populate a flowsheet composer column, with artist-as-proxy fallback for the (majority) of tracks Discogs has no writer for. Decouples future BMI reporting from the dying tubafrenzy MySQL.

## Coverage estimate (satisfies the ticket's coverage-estimate acceptance criterion)

Run 2026-06-28 against the live discogs-cache (254,973 releases / 3,418,121 tracks):

| Source | Releases with ≥1 writer credit | Tracks with writer credit |
|---|---|---|
| Track-level (`release_track_artist`, `extra=1`) | 11,862 — **4.7%** | 86,620 — **2.5%** |
| Release-level (`release_artist`, `extra=1`) | 54,296 — **21.3%** | — |

**Key finding: the release-level source dominates (~8.5× the release coverage of track-level).** WXYC's catalog is mostly single-artist albums where Discogs credits the writer once at the release level, not per track. So the release-level path is the primary contributor and track-level precision is a refinement that only fires on ~2.5% of tracks (compilations, classical, splits). This inverts the "track-level primary, release-level fallback" framing in the original ticket and motivates the phasing below.

Caveats to record in the PR: (1) this is **catalog-weighted**, not the acceptance criterion's ideal **flowsheet-weighted** number — the play-weighted rate needs the Backend-Service `flowsheet` DB join, deferred to live instrumentation once the field ships (cheaper and more accurate than a one-off historical join). (2) Even at ~21%, most played tracks won't resolve a Discogs composer and degrade to artist-as-proxy — an improvement over the 0% we get post-tubafrenzy, and the same proxy tubafrenzy already used.

## Design decision

Build the ticket's **B (track-scoped) + C (release-level fallback)** as **two phases, release-level first**, because release-level delivers ~21% coverage in the smaller, lower-risk change and track-level is additive precision on top. Same end state; ordered by value/effort. Each PR stays well under the 1000-line target.

**Surface: a `writer_credits` object on `DiscogsMatchResult`** (the `artwork` blob), NOT on `LookupResultItem`. Rationale:
- `DiscogsMatchResult` is the release-metadata carrier that already rides to Backend-Service via the `album_metadata` jsonb passthrough (`apps/enrichment-worker/enrich.ts`), alongside `tracklist`/`genres`/`label`. Putting `writer_credits` there means **zero new LML→BS plumbing** for the consumer — BS#1499 reads `album_metadata.writer_credits` directly. This captures option A's free-passthrough benefit without A's downside (BS re-matching the played track), because the value is pre-resolved by LML.
- It mirrors exactly how `tracklist` is enriched at `orchestrator.py:3698-3706` (`update["tracklist"] = list(top1_release.tracklist)`), so `top1_release.extra_artists` (which already carries `role`) is in scope at the same seam with no new threading for Phase 1.
- Gating: same as `tracklist` — `extended and is_album_derived_eligible`, top-1 only. BS forces `extended: true` (`apps/enrichment-worker/handler.ts`, `lookup-coordinator.ts`), and the played track is the top-1 match, so BMI capture is unaffected. Lean non-iOS callers keep their slim payload.

## Schema (`wxyc-shared/api.yaml`) — schema-first, merged before any LML regen

Add a `WriterCredits` schema and reference it from `DiscogsMatchResult`. Stable across both phases (Phase 1 sets `provenance: release`; Phase 2 adds `provenance: track` + `track_position`), so this is a single contract change.

Name the schema `DiscogsWriterCredits` (not `WriterCredits`) to match the sibling `Discogs*` schemas — `DiscogsMatchResult`, `DiscogsTrackItem`, `DiscogsArtistCredit` — so `datamodel-codegen` emits the class as `DiscogsWriterCredits`, and LML aliases it locally as `WriterCredits = DiscogsWriterCredits` in `discogs/models.py`, exactly like the existing `TrackItem = DiscogsTrackItem` / `ArtistCredit = DiscogsArtistCredit` aliases.

```yaml
DiscogsWriterCredits:
  type: object
  description: >
    Songwriter/composer credits for the resolved playcut, derived from Discogs
    writer-role credits, for BMI performance-list reporting. Omitted entirely
    when no writer credit resolves; names are never fabricated.
  required: [names, provenance]
  properties:
    names:
      type: array
      items: { type: string }
      description: Distinct songwriter/composer names for the resolved track.
    roles:
      type: array
      items: { type: string }
      description: >
        The verbatim Discogs role strings the names were drawn from (e.g.
        "Written-By", "Composed By", "Words By, Music By"), for auditability of
        the writer-role mapping.
    provenance:
      type: string
      enum: [track, release]
      description: >
        track = scoped to the resolved track's per-track credits (precise);
        release = a release-level credit applied to the whole release
        (approximate for an individual track). Populated as `release` in the
        initial rollout; `track` is added when per-track resolution lands.
    track_position:
      type: string
      nullable: true
      description: >
        The resolved track's position (e.g. "A1", "5") when provenance=track;
        null for release-level credits.
```

On `DiscogsMatchResult` add:

```yaml
    writer_credits:
      allOf: [ { $ref: '#/components/schemas/DiscogsWriterCredits' } ]
      nullable: true
      description: >
        Songwriter/composer credits for the resolved track, for BMI reporting.
        Populated only when `extended` is true and a writer credit resolves from
        the Discogs release; omitted otherwise.
```

Non-breaking additive change (optional response field): oasdiff WARN / minor bump. BS hand-maintains its LML client types and loose-casts, so it tolerates the new field. Regen fans out to TS (BS, dj-site), Python (LML, request-o-matic), Swift (iOS), Kotlin (Android); only LML and BS consume it.

## Writer-role heuristic (the load-bearing part — `discogs/writer_roles.py`, new module)

Role strings are stored verbatim as Discogs emitted them. The live data shows the mapping must normalize aggressively. Rule:

1. Split the role cell on `, ` into component roles (cells like `Words By, Music By` and `Written-By, Producer` are common).
2. Strip any trailing `[...]` qualifier from each component (`Written-By [Uncredited]`, `Composed By [Composition By]`, `Songwriter [All Songs By]`).
3. Lowercase and fold hyphen↔space (`Written-By` ≡ `Written By`).
4. A component is a writer role iff it is in the base set: **`written by`, `composed by`, `music by`, `lyrics by`, `songwriter`, `words by`**.
5. The cell counts as a writer credit iff ANY component matches. Extract that credit's artist name.

Include/exclude matrix to unit-test (drawn from the real cache strings):

| Role string | Writer? | Why |
|---|---|---|
| `Written-By`, `Written By` | yes | base, both hyphen + space variants observed |
| `Composed By`, `Music By`, `Lyrics By`, `Songwriter`, `Words By` | yes | base |
| `Words By, Music By`, `Music By, Lyrics By` | yes | compound, all components writers |
| `Written-By, Producer`, `Vocals, Lyrics By` | yes | compound, ≥1 writer component |
| `Written-By [Uncredited]`, `Written-By [Sample]`, `Composed By [Composition By]`, `Songwriter [All Songs By]` | yes | bracket qualifier stripped, base matches |
| `Producer`, `Arranged By`, `Adapted By`, `Performer`, `Featuring`, `Mixed By`, `Engineer` | no | explicitly excluded (Arranged/Adapted per ticket default) |
| `Drums`, `Bass`, `Guitar`, `Vocals`, instruments | no | performer |
| `Recorded By, Mixed By`, `Drums, Percussion` | no | compound, no writer component |
| `` (empty string) | no | the 288k empty-role release_artist rows must not count |

The matrix above is derived from an actual enumeration of the live cache's writer-ish role strings (2026-06-28), not guessed — so it already reflects the real hyphen/space/bracket/compound variants. Optional post-implementation guard against future drift (e.g. new Discogs role types, locale variants): once test #1 is green, run `is_writer_role()` over `SELECT DISTINCT role FROM release_track_artist WHERE extra=1` and log any role the base set misses for a quick eyeball — a one-off, not a CI gate. Document the rule + the Arranged/Adapted exclusion decision in the module docstring.

## Phase 1 — release-level credits (PR-B, after the api.yaml PR-A merges)

Test-first throughout (LML mandates TDD; red before green).

1. **`discogs/writer_roles.py`** (new): `is_writer_role(role: str) -> bool`, `extract_writer_names(credits: list[ArtistCredit]) -> list[str]` (dedups **by name**, order-preserving — an artist who appears under multiple writer-role credits, e.g. `Alice` with both `Composed By` and `Written-By`, collapses to a single `Alice` entry), and `writer_credits_from_release(release: ReleaseMetadataResponse) -> WriterCredits | None` returning the release-level object (`provenance="release"`, `track_position=None`). Unit tests cover the full matrix above. **Compilation guard — single responsibility:** `is_writer_role()` is pure role-string classification and carries NO release/VA logic (so it stays trivially unit-testable against the matrix). The VA/compilation guard lives solely in `writer_credits_from_release()`, which short-circuits to `None` for a compilation / Various-Artists release *before* any role extraction by reusing the **existing** `wxyc_etl.text.is_compilation_artist(release.artist)` helper already imported at `lookup/orchestrator.py:20` (no new import or duplicated check) — release-level writers are meaningless for a VA comp's individual track and would be actively wrong. Do not duplicate the VA check inside `is_writer_role()`. Test explicitly: a VA release whose `extra_artists` includes a `Written-By` credit returns `None` (the credit is never collected), distinct from a single-artist release with the same credit returning it.
2. **Internal model + conversion** (`discogs/models.py`): add `writer_credits: WriterCredits | None = None` to the internal match-result model (the one with `to_match_result`), placing it in the enriched-fields block (after `profile_tokens`, ~line 237, with default `None`, matching how the other enriched fields are grouped after the wire-contract fields), and map it in `to_match_result()` alongside `tracklist`.
3. **Regen** `generated/api_models.py`. **Blocking prerequisite, run first:** `cd ../wxyc-shared && git fetch origin && git checkout main && git pull` to sync the local sibling checkout to merged PR-A — *then* run `scripts/generate_api_models.sh` from the LML tree. This ordering matters because the script reads the **local sibling `../wxyc-shared/api.yaml` checkout first** (`generate_api_models.sh:20-31`), only falling back to downloading `wxyc-shared` main when no sibling exists — so before regenerating, sync the local `wxyc-shared` checkout to merged main (with PR-A), or the regen silently generates against stale schema and drops the new classes. Pre-regen step, explicitly: `cd ../wxyc-shared && git fetch origin && git checkout main && git pull` — then run the regen from the LML tree. (CI's drift-check independently fetches `wxyc-shared` main, which is why PR-B must wait for PR-A on main; the local sync only prevents local-vs-CI divergence during development.) Confirm `DiscogsWriterCredits` + `DiscogsMatchResult.writer_credits` generate; wire the local alias in `discogs/models.py` like the other generated aliases.
4. **Populate at the enrichment seam** (`lookup/orchestrator.py:~3698-3706`): inside the existing `if extended and is_album_derived_eligible:` / `top1_release is not None` block, add `update["writer_credits"] = writer_credits_from_release(top1_release)`. No new Discogs call — `top1_release.extra_artists` is already fetched.
5. **Docs:** the lookup-response shape is owned by `docs/architecture.md` (the 7-step pipeline / `DiscogsMatchResult` enrichment section), not `docs/api-endpoints.md` (which covers the non-lookup endpoints) — document the `writer_credits` field and the extended+top-1 gating there. Document the writer-role rule + the Arranged/Adapted exclusion in the `writer_roles.py` module docstring.

### Phase 1 test plan (red-first, in order)

Each test is written and confirmed failing before the implementation that satisfies it. `is_writer_role()` is tested as a **pure function with no release object** — the VA/compilation guard is exercised only through `writer_credits_from_release()`, keeping the two responsibilities independently verifiable.

| # | Test (file::name) | First red assertion | Goes green when |
|---|---|---|---|
| 1 | `tests/unit/test_writer_roles.py::test_is_writer_role_matrix` | parametrized over the full include/exclude matrix (incl. hyphen/space, compound `, `, `[...]` bracket, empty-string rows); pure string in → bool out, **no release object** | `is_writer_role()` exists with the split/strip/fold/base-set rule |
| 2 | `::test_extract_writer_names_dedup_and_order` | duplicate + multi-role credits collapse to distinct names in first-seen order | `extract_writer_names()` exists |
| 3 | `::test_writer_credits_from_release_single_artist_release_level` | single-artist release with a `Written-By` extra-artist → `WriterCredits(names=[...], provenance="release", track_position=None)` | `writer_credits_from_release()` exists |
| 4 | `::test_writer_credits_from_release_various_artists_returns_none` | VA/compilation release whose `extra_artists` includes `Written-By` → `None` (guard fires **before** extraction; the credit is never collected) | the VA guard is in `writer_credits_from_release()` only |
| 5 | `::test_writer_credits_from_release_no_writer_returns_none` | release with only performer/producer roles → `None` | extraction filters non-writer roles |
| 6 | `tests/unit/test_enrichment.py::test_writer_credits_populated_from_cache_release_level` (tests #6-8 colocate here — that file already owns the enrichment-gating logic: `extended`, `is_album_derived_eligible`, top-1) | a resolved top-1 release with a `Written-By` extra-artist makes the lookup response carry `artwork.writer_credits` with `provenance="release"`; **asserts the `discogs_service` fetch methods are NOT called** (cache-only, reading already-fetched `top1_release.extra_artists`; use the existing `test_enrichment.py` await-count / mock-assert idiom — e.g. `assert discogs_service.get_release.await_count == 0` — rather than inventing a new pattern) | step 4's enrichment-seam population lands |
| 7 | `::test_writer_credits_omitted_when_no_writer` | top-1 release with no writer credit → `artwork.writer_credits` is `None`/omitted | same |
| 8 | `::test_writer_credits_absent_when_not_extended` | a non-extended request → `writer_credits` omitted (rides the existing `extended and is_album_derived_eligible` gate) | step 4 sits inside the existing gate |

## Phase 2 — track-level precision (PR-C, after PR-B)

Gate: Phase 1's full test suite passes on the rebased-onto-merged-Phase-1 base before any Phase 2 code is written. Phase 2 is additive (it only refines `writer_credits_from_release()` to prefer track-level when a position is supplied), so every Phase 1 test must stay green — a Phase 1 regression here means the additive contract was violated.

1. **Widen the cache read** (`discogs/cache_service.py:~558-575`): the `release_track_artist` query currently filters `extra=0` and selects only `artist_name`. Add a parallel fetch (or widen) for `extra=1` rows with `role`. **Keying — note the `sequence` vs `position` distinction:** `release_track_artist.track_sequence` is the integer join key (it pairs with `release_track.sequence`), while `TrackItem.position` is the display string (`"A1"`, `"5"`) and is the *only* track identifier the matched `TrackItem` carries downstream — a `TrackItem` has no `sequence` field. So build the per-track writer map keyed by **`position`**, not `track_sequence`: `get_release` already has the `sequence → position` mapping in scope (it builds each `TrackItem` from `track_rows` where `row["sequence"]` and `row["position"]` are both present), so translate the `track_sequence`-keyed writer rows through it into `dict[position, list[(name, role)]]`. This lets the matched `TrackItem.position` look up writers directly in Phase 2 with no `sequence` threading. Keep the existing `extra=0` → `TrackItem.artists` behavior untouched (consumer-safety: `_scan_tracklist_for_match` still sees performers-only). Carry the position-keyed writer map on `ReleaseMetadataResponse` as a new internal field (not on the wire `DiscogsTrackItem`, which stays `list[str]`).
2. **Capture the resolved track position.** Mechanics, precisely: `discogs/service.py::_scan_tracklist_for_credit()` today returns `str | None` (the *joined performer string* of the first title-matched track) and throws away the matched `TrackItem`. `_iter_title_matched_items()` yields the matched `TrackItem`s, which **already carry `.position`** (no new field needed — `DiscogsTrackItem.position` exists). Phase 2 extends the credit-scan seam to also surface the matched track's `position` — either widen `_scan_tracklist_for_credit` to return the matched `TrackItem` (or a `(names, position)` tuple) instead of just the joined string, or add a sibling `_scan_tracklist_for_writer_position` that returns the matched `TrackItem`. Thread that `position` to the orchestrator enrichment seam (`~3698`), where `top1_release` and the played track are both in scope during validation. This is the only "new threading" the design fork flagged. **Phase 1 does not prep this seam:** its release-level path leaves `track_position=None`, the schema field is already nullable, and Phase 2 is purely additive — so there is no Phase-1/Phase-2 coupling to manage here.
3. **Prefer track-level in `writer_credits_from_release`:** when a resolved track position is supplied and that track has per-track writer credits, emit `provenance="track"` + `track_position`; otherwise fall back to the Phase 1 release-level path (`provenance="release"`). **Null-safety:** the resolved `position` may be null/empty (discogs-cache stores `release_track.position` as a nullable string, and the per-track writer map may have no entry for the matched `track_sequence`) — in that case fall through to release-level behavior silently (no error log; a missing position is normal, not exceptional). Never index the writer map without a presence check.
4. **Phase 2 test plan (red-first, in order):**

| # | Test (file::name) | First red assertion | Goes green when |
|---|---|---|---|
| T1 | `tests/unit/test_writer_roles.py::test_writer_credits_track_level_scoped` | multi-track release; the played track's `position` returns only *that* track's writers (`provenance="track"`, `track_position` set), no cross-track bleed | the position-keyed writer map + track-preference branch land |
| T2 | `::test_writer_credits_track_missing_falls_back_to_release` | a played track with no per-track writer falls back to release-level (`provenance="release"`) | the fallback branch is wired |
| T3 | `::test_writer_credits_null_position_falls_back_without_raising` | a matched track with null/empty `position` (or no entry in the writer map) falls back to release-level provenance and does not raise | the presence-checked lookup lands |
| T4 | `::test_writer_credits_compilation_track_level_resolves` | a VA/compilation release: the release-level guard still returns `None`, but a per-track writer on the played track still resolves (`provenance="track"`) — per-track is correct on a comp | track-level path bypasses the VA guard (which only gates the release-level path) |

## Cross-repo sequencing & merge order

PR-A and PR-B cannot land in one PR — LML regen CI downloads `wxyc-shared` main, so the api.yaml change must be on main first (per the API-model cross-repo merge-order constraint).

1. **PR-A — `wxyc-shared`:** add `DiscogsWriterCredits` + `DiscogsMatchResult.writer_credits` to `api.yaml`. oasdiff should report a non-breaking minor bump. Merge (self-authored merge needs `--admin`, user-run).
2. **PR-B — LML Phase 1:** regen against merged shared main + heuristic module + release-level populate + tests + docs. `Closes` nothing yet (keep #699 open through Phase 2) — reference #699. **Do not open or push PR-B until PR-A is merged to `wxyc-shared` main** — PR-B's regen drift-check CI fetches `wxyc-shared` main and will fail against the pre-PR-A schema (the standing LML↔shared merge-order constraint).
3. **PR-C — LML Phase 2:** track-level read + threading + provenance refinement + tests. `Closes #699`.
4. **BS companions (#1499, #1500):** out of scope here; unblocked once PR-B ships the field.

Each LML PR through the `/code-review max` review-loop before rebase-merge (one independent review agent per PR), per house convention.

## Acceptance criteria mapping (from the ticket)

- [x] Coverage estimate — done (above); flowsheet-weighted deferred to live instrumentation with rationale.
- [ ] `api.yaml` carries the field; codegen regenerated — PR-A + PR-B.
- [ ] Resolved track with a `Written-By` credit returns the writer; none → omitted — PR-B tests.
- [ ] Writer-role mapping documented + unit-tested — PR-B (`writer_roles.py` + matrix).
- [ ] Track-position scoping tested on a multi-track release; release-level fallback tagged with distinct provenance — PR-C (Phase 2) for track scoping; provenance tag present from PR-B.
- [ ] No new outbound Discogs calls (cache-only) — asserted in PR-B/PR-C tests.

## Risks / notes

- **Concurrent work collision (#706):** the cold-tail latency plan (PR1 `.worktrees/706-pr1`, branch `feat/706-pr1-streaming-warm-offpath`) touches `orchestrator.py` + `cache_service.py` on the streaming/hot path. My changes touch the release-credit enrichment seam and the `release_track_artist` read — adjacent but non-overlapping. **Create distinct Phase-1 and Phase-2 worktrees independently off a freshly-fetched `origin/main` (`git fetch origin && git worktree add <path> origin/main`); do NOT reuse or branch from the `706-pr1` tree** — that would tangle the two workstreams' commits. (The default LML tree tracks `prod` and can lag `origin/main`, so the explicit `fetch` + `origin/main` base matters.) Keep changes surgical. If #706 lands before Phase 1 merges, rebase Phase 1 onto the updated `origin/main` to absorb any `orchestrator.py` changes; do not start/rebase Phase 2 until Phase 1 is merged (Phase 2 builds directly on Phase 1's `writer_roles.py` + enrichment seam).
- **Data readiness:** both `release_track_artist.role` (migration 0005) and `release_artist.role` (this session's discogs-xml-converter#74 + discogs-etl#292) are already populated in the live cache (confirmed by the coverage queries) — no rebuild dependency blocks either phase.
- **Compilation correctness:** the release-level path MUST suppress on VA/compilations (guard + test). This is the main correctness trap in Phase 1.
- **Out of scope:** MusicBrainz Works as a higher-authority composer source (separate cache expansion, only if Discogs coverage proves inadequate); the BS flowsheet composer column + BMI export successor (#1499/#1500).
