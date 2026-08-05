# Review: LML #504–507

PR #500 (LML#487) landed at `origin/main`, my checkout doesn't have it. Issues are coherent against `main`; line refs drift ~5–15 lines from `/tmp/orch_main.py`. Sequencing 504 → 505 → 507 → 506 is sound with one caveat (see #506).

## #504 — Split `is_album_derived_eligible`

**Direction is right.** Source-scope split is faithful to the code: `artist_bio`/`wikipedia_url`/`profile_tokens` all flow from `get_artist_details(release.artist_id)` (orchestrator `:1789`, `:1793`, `:1810-1818`); `release_year`/`tracklist`/`label`/`full_release_date` come from `top1_release`. The gate at `:1879` (`is_top1 and artwork is not None`) really does over-suppress on the synthesis path.

**Three issues with the proposal as written that should be resolved before implementation:**

1. **`VARIOUS_ARTIST_ID` doesn't exist anywhere — not as constant, not even as magic `194`.** The only `194` in the repo is `lookup/external_search.py:41` referencing an unrelated GitHub issue. VA detection today uses `COMPILATION_ARTIST_CANONICAL_FORM = "Various Artists"` (string match) and `is_compilation_artist()`. Pick one before merging: (a) introduce the constant after independently confirming Discogs entity 194 is "Various", or (b) pivot to `release.is_compilation` semantics already in the codebase.

2. **`request_artist` is not a parameter of `enrich_artwork_results`.** The function takes `song` and `album` from `parsed`, not `artist` (`:2266-2277`). The proposed predicate references `request_artist` as if it's available — it isn't. Threading it requires touching `perform_lookup`'s signature too. Issue glosses over this; call it out as a signature-change line item.

3. **Internal variable-name inconsistency in the predicate.** Body uses `top1_release.artist` (string) AND `release.artist_id` (int) — but the actual in-scope variable is `top1_release` throughout `enrich_one`. Rewrite as `top1_release.artist_id not in {VARIOUS_ARTIST_ID, None}` for consistency.

**Minor:** Also state the `library_row_acceptable` predicate explicitly (presumably the existing `is_top1 and artwork is not None`). Right now the proposal only specs `artist_identity_verified`; the album gate is implicit.

**Coverage caveat I'd add to Tests:** asymmetric whitespace — the predicate guards `bool(request_artist) and bool(item.artist)` but only `bool(getattr(top1_release, "artist", None))` for the release side. `score_match(" ", "x")` doesn't get the same protection.

## #505 — Override + probe artwork pairing leak

**Mechanism verified at code.** Override block at `:1906-1921` gates on `row_title_matches_requested_album` only — `library_row_acceptable` doesn't appear in the override guard. All five service URLs leak together from one `library_db.get_streaming_links(item.id)` payload. Reissue-suffix strip in `clients/streaming/matching.py:25-29` does catch `(Deluxe Edition)` etc., so `score_match("Album X", "Album X (Deluxe Edition)") == 100` is correct and the leak triggers on every Deluxe/Remaster shape, not just the rare comfortably-≥-80 case.

**Cross-service blast radius is real — Spotify dominates.** `scripts/streaming_analysis_report.md` shows Spotify ~46.8K rows vs Bandcamp ~2.8K (and Apple lower than Spotify). Framing "the higher-volume user-visible leak is Spotify, not Apple" is well-supported.

**Best-supported fix:** Option B (no-field-change variant). The probe's own `album_score >= 80` filter (apple_music.py `:408-411`) is against `album` (the requested album, a function arg) — so on the synthesis branch, any non-None `probe_match` already means "probe matched the requested album." A post-hoc clear of all 5 override URLs when `not library_row_acceptable and probe_match is not None` is correct without adding `album_name` to `AppleMusicTrackMatch`.

**Two gaps the issue misses:**

- **Option A isn't "lose curated URLs" — it's "downgrade to search URLs."** Code at `:1923-1940` fills any unset service from search-URL fallbacks. So Option A trades a verified Spotify URL for `https://open.spotify.com/search/?q=...` — a quality downgrade per your own `Bandcamp direct > Apple Music > Spotify validated > Bandcamp search` priority hierarchy. Worth explicit framing.
- **`apple_music_url` itself leaks via precedence.** Line `:1947`: `"apple_music_url": apple_music_override or apple_music_result or None`. Override beats the Apple probe URL — but post-PR#500 the probe is what found the *correct* release. Option B clearing override lets the probe URL win — that's actually a *second* fix Option B gets for free.

## #506 — MusicBrainz tracklist rescue sanity check

**Real-but-rare framing is accurate.** Resolver mechanics (`_SIMILARITY_FLOOR=0.70`, `_PG_TIMEOUT_S=2.0`, trigram match) all verified at the cited lines in `release/musicbrainz_resolver.py`.

**Sequencing claim is overstated.** The proposed sanity check (`if not any(score_match(song, t.title) >= 80.0 ...)`) operates on `mb_tracklist` *after* the resolver returns and doesn't care which gate fired the rescue. It can ship today against the existing `if artwork is None:` gate — the failure mode exists on cross-artist mojibake right now (issue concedes this in the baseline note). The `gate_widened` Sentry tag *is* coupled to #504, but the sanity check itself isn't. Recommend reframing #506 as "ships when convenient, ordering with 504/505/507 is purely merge-conflict avoidance."

**False-negative the issue doesn't acknowledge:** Deluxe Edition tracklists are usually a *superset* of Original. When the DJ requests a song that exists on both editions, `any(score_match(song, t.title) >= 80.0)` returns True against both — the check only catches Deluxe-vs-Original when the request is for a bonus-only track. Worth a line saying "this catches the bonus-only-track variant; the shared-track variant remains undetected by this heuristic."

**Track suffixes stripped by `score_match` are OK.** `_PARENTHETICAL_SUFFIX_RE` matches `reissue|remaster(ed)?|deluxe|limited|edition|expanded|anniversary|bonus` — none of those would commonly appear in track titles. "(Live)" / "(Remix)" pass through. So using `score_match` (designed for albums) on track titles is acceptable.

## #507 — Wasted top-1 Discogs prefetch

**Hoisting is feasible.** `library_row_acceptable` inputs (`items_with_artwork[0][1]`, `album`, `item.title`) are all available at function entry of `enrich_artwork_results`; `score_match` is pure; `fetch_top1_release_details` doesn't depend on per-item state. Sequence (await at `:1802`, gather at `:1992`) means the skip can only shorten the critical path.

**The contradiction concern from your reviewer instinct (top1_release needed for #504's predicate) resolves cleanly — but worth a clarifying note in the issue body.** When `library_row_acceptable=False`, you skip `get_release`, so `top1_release=None`, so `artist_identity_verified=False` by short-circuit on its first conjunct (`top1_release is not None`). You never *compute* `artist_identity_verified` in this branch — it falls through to False automatically. So all three fetches (`get_release`, `get_artist_details`, `parse_async`) skip together. The `artist_identity_verified=True AND library_row_acceptable=False` state is unreachable post-#504 *because* the former presupposes a fetch the latter cancels. Worth one line in the "Scope post-#504" section to spell this out — a reviewer reading only #507 in isolation could miss it.

**Minor under-count:** `get_release` cascade is 1 + 4 parallel + 3 parallel = 8 PG queries (`cache_service.py:485-661`), not "~4-7". Doesn't change the conclusion, but the cost framing is slightly conservative.

## Cross-cutting

- **Line-ref hygiene before implementation.** Every issue cites `/tmp/orch_main.py` line numbers. Map them to `lookup/orchestrator.py` against the rebased branch (PR #508 just merged on `main` is a LML#487 follow-up that may shift things further) before the implementer starts. Otherwise the first thing they hit is "the line numbers don't match."
- **Implement against `main` after rebase**, not against the current `fix/artist-stub-fetched-at-502` branch — that one predates PR #500 entirely.
- **PR # references are correct** — PR #500 is "LML#487 fix" (Apple-probe-on-LML#477-gate-fail), merged 2026-06-07. The `is_album_derived_eligible` gate itself predates PR #500 (introduced in commit `1f13003` for #401); what PR #500 changed is the *reachability* of the synthesis branch. Issues' "PR #500 introduced X" wording is loose but not wrong.
