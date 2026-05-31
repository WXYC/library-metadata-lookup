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

## Example dialogue

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
