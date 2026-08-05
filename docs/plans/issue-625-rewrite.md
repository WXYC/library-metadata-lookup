## Principle

LML's job is to resolve metadata for **any** release the inputs identify — whether the record is in rotation, the permanent library, or a DJ's personal collection. Library membership confirms WXYC owns a copy and supplies call numbers / filing / curated streaming links; it should **enrich** a result, never **authorize** it. Today the `/lookup` pipeline inverts that: it is *rooted* in `library.db`, and Discogs is used only to cross-reference, validate, or enrich rows that already matched a library row. A release absent from `library.db` is dropped even when LML has already resolved and validated it.

> This was originally framed as "rotation comps are invisible to `library.db`." That's a *symptom*, not the bug. Sourcing `ROTATION_RELEASE` into `library.db` would paper over the rotation case while doubling down on catalog coupling — and still wouldn't help a personal-collection or one-off release. The fix is to decouple resolution from catalog membership.

## Concrete prod repro

Live flowsheet entry, enriched **after** the prod flag flip (22:34Z), so `LML_RESOLVE_COMPILATION_RELEASE` was ON:

| field | value |
|---|---|
| artist / track / album | **Chez Damier & Ben Vedren / "The Three Dimensions of Air" / "When There Is No Sun"** |
| `add_time` | `2026-06-19T23:05:55.931Z` |
| `rotation_id` | 43171 (rotation track, not in `library.db`) |
| `metadata_status` | `enriched_match` |
| `discogs_url` | `''` |
| `album_id` / `artwork_url` | `None` / `None` |

iOS "More Info" renders cover art + streaming **search** buttons but **no Discogs link** — the #604 symptom, on the same comp ("When There Is No Sun") as #604's own spine case (A Guy Called Gerald). So #604's fix doesn't surface a Discogs link for the case it was written for — not because of "rotation", but because that release has no library row.

## Architecture: the pipeline is library-rooted

`perform_lookup` only ever returns `library_results` ([orchestrator.py:3187](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L3187)), populated entirely by the five search strategies, each of which begins with a `db.search()` against `library.db`. `ARTIST_PLUS_ALBUM` and `SWAPPED_INTERPRETATION` are library-only by construction ([orchestrator.py:3161-3162](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L3161-L3162)); the three Discogs-aware strategies use Discogs to *find candidate releases* but then re-gate each one on a fuzzy library-row match and discard it if none exists. So a non-library release has no path to a result except two narrow, lossy escape hatches (A2/A3 below). The result: library-membership and Discogs-cross-reference are conflated into one gate, when the principle wants them orthogonal — presence should enrich, not authorize.

## Violation sites

### A. Hard gates — a non-library release returns nothing

**A1 — Discogs-aware strategies discard a validated release for lack of a library row (the core bug).** Each of these resolves + validates the track against a specific Discogs release, then throws the answer away when no `library.db` row fuzzy-matches the album title:

- `TRACK_ON_COMPILATION` / `process_release` — [orchestrator.py:1599](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L1599): `if not matches: return []`. (This is the path the prod repro hits.)
- `SONG_AS_TRACK` / `_validate_one` — [orchestrator.py:998-1006](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L998-L1006): `if not matches: return None` … `if not eligible: return None`.
- `SONG_AS_ARTIST` / `search_album` — [orchestrator.py:900-908](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L900-L908): keeps only Discogs releases that also fuzzy-hit a library row.

In each, track-credit validation against the Discogs tracklist has already *succeeded*; the library-membership check is a second, unrelated gate that drops a correct answer. The #604 binding ([`_bind_resolved_release`, L2121](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L2121)) then has nothing to bind to. LML found the answer and threw it away for lack of a catalog row.

**A2 — The library-miss Discogs fallback is too narrow.** The only path that returns Discogs metadata with an empty library is [orchestrator.py:3207](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L3207): `if not library_results and parsed.artist and parsed.album and parsed.album.strip()`. A **song-only**, **artist+song (no album)**, or **album-only** query for a non-library release still returns empty — and even when it fires, it synthesizes a `release_id=0` / BS#1185 streaming-only sentinel (`discogs_url=''`, `album_id=None`) at [`enrich_artwork_results`, L2315](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L2315) that **zeroes the Discogs identity by design**.

**A3 — `include_external_caches` is a half-measure.** The opt-in mojibake-recovery fallback (`lookup/external_search.py`) returns only a canonical **artist name** wrapped in a synthetic item — not a resolved release.

### B. Soft violations — works, but corrupted / degraded for non-library input

**B1 — Artist correction snaps non-library artists into library vocabulary.** `find_similar_artist` ([library/db.py:502-618](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/library/db.py#L502-L618)) fuzzy-matches the typed artist against **library artists only** (threshold 85, raised for short names), and when something clears the bar `perform_lookup` overwrites `parsed.artist` ([orchestrator.py:3148-3150](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L3148-L3150)) — *before* the Discogs album-resolution query runs on that artist. A DJ spinning a non-library artist whose name lands within fuzzy range of a library artist gets silently snapped to the wrong artist, corrupting the Discogs lookup. (When nothing clears the threshold it correctly returns `None` and preserves the typed artist — that half is fine; the bug is that correction is scoped to the *library* vocabulary yet applied to a source-agnostic lookup.) This is upstream of, and independent from, A1 — it needs its own fix and is the most cleanly separable sub-issue here.

**B2 — Enrichment fields assume a real library row.** Even on the row-less synthesis path, the curated streaming-links override hard-requires a real library row (`item.id` truthy) at [orchestrator.py:2562-2567](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L2562-L2567), and artist bio / Wikipedia resolve through `library_row_acceptable` ([defined at L2547](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L2547), gate at [L2738-2742](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L2738-L2742)) — the split-gate can't even apply without a `library_row_anchor_present`. So a non-library release that *does* surface gets thinner metadata than a catalogued one.

### C. The compliant model (precedent to follow)

The non-`/lookup` endpoints already honor the principle and are the template: `/api/v1/releases/resolve` (#170) resolves arbitrary Discogs/Bandcamp URLs source-agnostically; `/api/v1/streaming-check`, `/identity/resolve`, `/identity/bulk`, `/api/v1/identity/resolve`, and `/api/v1/cache/refresh-for-identities` all take arbitrary `(artist, title)` / `(source, external_id)` inputs with **no** library-membership requirement. The lookup pipeline is the outlier.

## What already works

For releases that **are** in `library.db`, binding works — validated on staging with `release_resolution_bind {'bound': True}`: Hoagy Carmichael / "A Hoagy Carmichael Songbook" → release `10466949`; ambient "Bounce to the Ounce" → release `4163711`. The resolution logic is sound; only the library-row gate drops it for non-catalog releases.

## Desired end state

`/lookup` returns the resolved Discogs identity (`release_id`, `release_url`/`discogs_url`, artwork, year) whenever LML can resolve and validate the release from the inputs — **independent of whether a `library.db` row exists** — and a library row, when present, *adds* call number / filing / curated streaming links on top. A flowsheet add for a rotation / personal-collection / promo release surfaces the Discogs button and full enrichment just like a catalogued one, and the artist the DJ typed is never silently rewritten to a near-miss catalog artist.

## Suggested approach

Primary (A1): stop discarding a resolved+validated release when no library row matches. The `release_id=0` synthesis path ([`enrich_artwork_results`, L2315](https://github.com/WXYC/library-metadata-lookup/blob/6cf01c1b4200bf00309e6e6efe3dcdf293de5278/lookup/orchestrator.py#L2315)) already returns a row-less result for non-library releases — but it **zeroes the Discogs identity by design** (the BS#1185 sentinel). Carry the resolved `release_id`/`release_url` through that row-less result instead of zeroing it, when the compilation / release-resolution path actually produced a validated release. Then:

- **A2** — widen the library-miss probe beyond the artist+album precondition, or route the strategies' already-resolved releases through the same row-less carry-through.
- **B1** — scope artist correction so it doesn't rewrite the artist used for source-agnostic Discogs resolution (e.g. correct only for the library-search leg, or gate correction on edit-distance rather than library-vocabulary fuzzy %).
- **B2** — let the row-less result carry artwork / bio / streaming from the resolved release rather than re-gating on a library row.

Things to settle:

- Backend / iOS must accept a result that carries a real `release_id` + `discogs_url` with `album_id=None` (no backing `LibraryItem`). Today `release_id=0` is the BS#1185 contract for "no Discogs identity"; this needs a shape for "Discogs identity, no catalog row."
- Don't regress the #604 title-gate guard that correctly strips loose fuzzy *library* mismatches — the row-less path bypasses that gate precisely because there's no library row to mis-trust.
- Keep it consistent with `LML_RESOLVE_COMPILATION_RELEASE` and the existing negative-cache / chunked-validate cost controls (non-library releases are the cold-cache shape).

**Not the fix:** sourcing `ROTATION_RELEASE` into `library.db`. (Making rotation searchable is independently worthwhile for the *catalog-search* role, but it's orthogonal — it doesn't decouple metadata resolution from catalog membership and wouldn't help non-rotation non-library releases. Track that separately if wanted.)

This issue is the architectural root; the fronts above are deliberately separable. A1 (+ the contract change) is the spine; B1 is the cleanest standalone sub-issue. Split into sub-issues if the delta gets large — see acceptance criteria.

## Acceptance criteria

- [ ] `/lookup` for a release **not** in `library.db` (the repro above, or any `rotation_id`-bearing add) returns a non-empty `discogs_url` when the release is resolvable on Discogs — via all three Discogs-aware strategies (`TRACK_ON_COMPILATION`, `SONG_AS_TRACK`, `SONG_AS_ARTIST`), not just the compilation path.
- [ ] The #604 spine case (A Guy Called Gerald / "When There Is No Sun") binds end-to-end.
- [ ] A `song`-only / `artist+song` (no album) query for a resolvable non-library release no longer returns empty (A2).
- [ ] Artist correction no longer rewrites the typed artist used for Discogs resolution when that artist is absent from the library (B1).
- [ ] A surfaced non-library release carries artwork / bio / streaming from the resolved release, not the thinner sentinel shape (B2).
- [ ] In-library comp binding does **not** regress (the staging `bound:True` cases still bind); the library title-gate still strips wrong fuzzy *library* matches.
- [ ] Backend/iOS contract updated/confirmed for "Discogs identity present, `album_id` absent".
- [ ] Unit + integration coverage for the row-less resolved-release path and the artist-correction scoping change.

## Related

- #604 (parent bug — the architectural root the rotation case exposed)
- #616 (PR2 implementation — the library-coupled binding)
- #170 (`/releases/resolve` — already does source-agnostic release resolution from a URL; precedent for release-centric, library-independent metadata, and the compliant model the other endpoints follow)
