# Library Metadata Lookup

FastAPI service that searches the WXYC library catalog and cross-references results with external metadata sources (Discogs, MusicBrainz, Apple Music) to enrich flowsheet entries with artwork, streaming links, and identity resolution.

## Language

### Apple Music integration

**Apple Music**: Apple's commercial streaming service. Catalog accessed via the authenticated `api.music.apple.com` endpoint.
_Avoid_: "iTunes" (the deprecated free Search endpoint is a different system, see below).

**iTunes Search**: Apple's free, unauthenticated search endpoint at `itunes.apple.com/search`. Returns flat JSON results; rate-limited by IP reputation. Used by LML historically; replaced by Apple Music API after the 2026-05-28 Railway-IP block. Removed entirely after the migration.

**Developer token**: ES256-signed JWT identifying a MusicKit key. Required on every request to Apple Music API. Generated per-request from the `.p8` private key + Team ID + Key ID. Apple permits `exp` up to ~6 months; LML signs with ~20-minute `exp` for simplicity.
_Avoid_: "API key", "auth token" (Apple's docs use "developer token" specifically).

**MusicKit identifier (Media ID)**: Reverse-DNS identifier registered in the Apple Developer console that authorizes a key for MusicKit / Apple Music / Apple Music Feed / ShazamKit. LML uses `media.org.wxyc.lml`.
_Avoid_: "App ID" (different identifier type, for iOS apps), "service ID" (yet another type, for Sign in with Apple).

**Storefront**: Country/region-scoped catalog (`us`, `gb`, etc.). Apple Music returns different metadata per storefront. LML hardcodes `us`.

**Match floor**: Per-call verification threshold (`fuzz.token_set_ratio >= 80`) applied to Apple Music search results' artist/track/album fields before accepting a URL. Stops the wrong link from freezing onto a flowsheet row when Apple's search ranking is unstable for obscure artists (LML#389, #396).

### Release resolution (compilation cross-reference)

**Release resolution**: Finding *which specific Discogs release* a track sits on and confirming the track is actually on it, by cross-referencing the track against Discogs release search and checking the tracklist. Independent of the release-level artist credit — it answers "which release", not "does the typed artist own this release". Lives in one module, `lookup/release_resolution.py`: `resolve_release_for_track(song, artist, album?)` returns a ranked `list[ResolvedRelease]` (title-match to the requested album first, stable `release_id` tie-break), or empty. The module also owns the shared probe/validate primitives (`merge_wave_b_compilations`, `validate_release_for_track`) that the miss-path search strategies (`TRACK_ON_COMPILATION`, `SONG_AS_TRACK`) delegate to — they keep their own interleaved library-match-then-validate flow (so validation only pays for library-matching releases, LML#536), while the binding step calls `resolve_release_for_track` directly as its lazy fallback.
_Avoid_: "album lookup", "Discogs search" (those name the raw probe; release resolution is probe + tracklist validation + ranking).

**ResolvedRelease**: The value release resolution yields — `release_id`, `release_url`, `is_compilation`, `album_title`. Carried internally (never on the wire) from whichever producer resolved it to the binding step, keyed by library-row id. The seam that carries it was historically lossy: it carried only the album-title string (`discogs_titles: dict[int, str]`), dropping the proven `release_id`.

**Track-credit validation**: `validate_track_on_release` matching the *per-track* artist credit (`tracklist[].artists`), not the release-level artist. This is why a Various-Artists compilation validates: "Message to Black Youth" credits "A Guy Called Gerald" on its own tracklist row even though the release credits "Various". Release resolution leans on this so the typed track-artist never has to match the release credit.

**Artist floor**: The 80/80 acceptance threshold the artwork/Discogs-binding search applies to (release artist, title) via `find_best_typed_match` — the same family as the Apple Music **match floor** above. It correctly rejects a wrong-artist release, but it *also* rejects the right Various-Artists release for a track-artist query, because "A Guy Called Gerald" can never clear 80 against a release credited "Various". Release resolution deliberately **bypasses** the artist floor: the artist was already settled by track-credit validation, so binding a resolved release does not re-gate on the release credit.

**Binding step**: `fetch_artwork_for_items` — where a resolved release becomes the album's outbound `discogs_url` / artwork / `release_year` (the `DiscogsMatchResult` Backend persists to `album_metadata`). When it carries a `ResolvedRelease` it trust-and-binds by id (no re-search, no floor); when it doesn't and its floor search comes up empty, it *lazily* runs release resolution as a fallback (flagged; negative-cached; live-worker path only, not the bulk backfill).

**Compilation / Various-Artists (V/A)**: A release crediting "Various" at the release level while each track credits its own artist. Filed in the WXYC library under its own V/A conventions (see the librarian V/A invariant). The reason a flowsheet track and its Discogs release disagree on "the artist".

## Example dialogue

> **Dev**: A compilation track shows streaming buttons and cover art but no Discogs link. Why just that one?
>
> **Domain expert**: Three different paths feed those surfaces. Streaming links resolve track-level by (artist, album), and the cover comes from the app's own free-text Discogs search — neither touches the **artist floor**. The Discogs link only appears when **release resolution** bound a `discogs_url`, and for a Various-Artists release the binding step re-searched by the track artist and the artist floor rejected "Various".
>
> **Dev**: But we *know* which release it's on — the track validated against the tracklist.
>
> **Domain expert**: Right, that's **track-credit validation** — it matches the per-track credit, so the comp validates fine. The problem was the proven `release_id` got dropped at a lossy seam and the binding step re-derived a release through the floor. Resolve it once, carry the **ResolvedRelease**, and let binding trust it instead of re-floor-gating.

> **Dev**: A DJ reported that the Apple Music button for one of her flowsheet entries opens the wrong song. The wrong artist's track. Same title.
>
> **Domain expert**: Yeah — Apple's search ranking can return a popular but wrong artist when the requested artist is obscure. We mitigate that with the **match floor**: the `artistName` from the API response has to clear an 80% fuzzy match against the queried artist before we accept the URL. If it doesn't clear, the URL gets dropped.
>
> **Dev**: So the floor is on the API response, not the catalog?
>
> **Domain expert**: Right. The catalog gives us the artist + track + album the DJ entered. The **Apple Music API** is what returns multiple candidate URLs to pick from, and the floor decides which (if any) is acceptable. The **storefront** is locked to `us`, so we never get a non-US release back.
>
> **Dev**: And the `developer token` is the JWT we sign per request?
>
> **Domain expert**: Yeah. ES256 over the `.p8` private key, claims are `iss=Team ID`, `iat`, `exp = iat + 20 min`. The Key ID goes in the JWT header as `kid`. Apple validates the signature against the public half registered to the **MusicKit identifier** `media.org.wxyc.lml`.
