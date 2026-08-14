# 0002 — Wikipedia-preferred artist bio, quotation-with-link-back attribution

`artist_bio` on `/api/v1/lookup` and `/api/v1/lookup/bulk` has always been sourced exclusively from the Discogs artist `profile` field. Discogs profiles are inconsistent in quality, carry Discogs-specific markup (`[a123]`/`[r456]` refs needing the `discogs/markup_parser.py` token subsystem to render cleanly), and are frequently blank. Wikipedia's lead paragraph is usually better, prose-clean, editorially-maintained text. We are switching `artist_bio`'s preferred source to the Wikipedia lead paragraph (`clients/wikipedia.py`'s REST `/page/summary` `extract` field), falling back to the Discogs profile when no confident Wikipedia page is found or its fetch fails. Full design: `docs/plans/lml-1192-wikipedia-artist-bio.md`.

Two decisions this ADR exists to record, because they need a home that outlives the implementation program:

## Decision 1 — source adoption: Wikipedia preferred, Discogs fallback

Precedence is Wikipedia lead paragraph → Discogs profile → null. This is a hard prerequisite dependency on fixing the pre-existing `wikipedia_url` extractor first (LML#513): the old first-substring-match heuristic over the Discogs artist `urls` list could surface a band-member, album, label, or non-English-wiki page ahead of the artist's own page, with no signal it had picked wrong. A wrong link is a recoverable UX defect; once this program starts serving fetched bio *text*, a wrong pick is wrong *prose* — and because Backend-Service freezes the first LML answer for an album permanently (BS#1747: once Discogs/artwork match, BS never re-asks), a bad cold-cache first answer is not self-correcting. So bio text is only ever fetched and served from a Wikipedia page the slug-scored extractor (`lookup/wikipedia_url.py`) picked with confidence (`score >= SCORE_MATCH_ACCEPTANCE_FLOOR`, i.e. `below_floor=False`).

## Decision 2 — licensing posture: quotation with a link back, no new attribution machinery

Wikipedia/Wikimedia content is CC BY-SA licensed, which formally requires attribution. Two paths were available: (a) build CC-attribution machinery — a `bio_source` field, a license-notice string, a byline the client renders — or (b) treat this as quotation with a link back to the source, since Wikimedia's Terms of Use accept a hyperlink to the article as attribution and LML already has one.

We chose (b), and it costs nothing new to wire: `wikipedia_url` already flows through the API contract into Backend-Service's V2 shapes today (`FlowsheetEntryFields.artist_wikipedia_url` / `FlowsheetV2TrackEntry.artist_wikipedia_url` in `wxyc-shared`'s generated models) and renders in the iOS playcut details view. LML's contract already guarantees the served link points at the exact source page of the served text (the `entity/artist_wikipedia_bio.py` cache's self-healing read predicate enforces this — a stored row whose `wikipedia_url` no longer matches the current extractor pick is read as a miss, not served stale). So: **no `bio_source` field, no license-notice rendering, no new API contract surface.** The swap is client-transparent — the existing `artist_bio` string just gets better text, and the existing `wikipedia_url` string is the attribution link, already rendered.

Jake's call, recorded 2026-08-13, carried through plan review round 5 (2026-08-14) without dissent.

## Considered options

- **Full CC-attribution machinery** (new `bio_source` enum field, license-notice string surfaced to clients): rejected — no client need identified beyond the link-back Wikimedia's own ToU already accepts as sufficient, and it would require an `api.yaml` change plus client-side rendering work across iOS/dj-site/BS for no incremental compliance benefit.
- **Keep Discogs as the only source, just fix the extractor** (LML#513 alone, no Phase B/C): considered and rejected as the full scope — the extractor fix alone doesn't improve bio *text* quality, only the link's correctness, and the actual user-facing motivation (better artist bios) needs the source swap.
- **Serve Wikipedia text without first fixing the extractor**: rejected outright — this is the "wrong page pick is now wrong text, not just a wrong link" risk the plan's constraints section names as the reason the extractor fix is a hard prerequisite, not a nice-to-have.

## Consequences

- `profile_tokens` (the tokenized Discogs profile, used for structured client-side rendering of Discogs markup refs) keeps parsing the Discogs `top1_bio` text, never the Wikipedia extract — a response can now legitimately carry Wikipedia prose in `artist_bio` and Discogs-profile tokens in `profile_tokens` simultaneously, two different source texts rather than two renderings of one bio. This is an accepted, documented semantic shift (see `docs/architecture.md`'s step-4b note), not a bug.
- A Wikipedia outage or a below-floor extractor pick degrades to exactly today's Discogs-profile behavior — no request-path dependency on Wikipedia at all (fetches happen only in the `warm_cache`-gated background task and the offline drain), so there is no new availability risk to `/lookup`.
- The extractor fix (`lookup/wikipedia_url.py`) is deliberately LML-Python-local rather than promoted to the shared `wxyc_etl.text` Rust crate immediately — no Rust release/pin-bump gates this program, and Backend-Service carries the identical heuristic bug at `proxy.controller.ts:498` unfixed. The promotion + BS-side adoption stays open on LML#513 as an explicit follow-up (also filed as a `wxyc-etl` ticket alongside this program).
