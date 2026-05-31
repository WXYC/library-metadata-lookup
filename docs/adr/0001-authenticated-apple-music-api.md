# 0001 — Authenticated Apple Music API for catalog search

Starting 2026-05-28, iTunes Search (`itunes.apple.com/search`) returned HTTP 403 to every outbound call from LML's Railway egress IP, breaking the Apple Music URL enrichment that the iOS app's flowsheet view depends on. Verified the block is IP-scoped (curl from local laptop and from BS EC2 returns 200 against the same User-Agent), so changing User-Agent or retrying won't recover. We are migrating both call sites — `clients/streaming/apple_music.py:AppleMusicClient` (used by `/streaming-check`) and `lookup/orchestrator.py:_fetch_apple_music_url` (the iOS flowsheet hot path) — to the authenticated Apple Music API (`api.music.apple.com`) using an ES256-signed developer token, registered against the MusicKit identifier `media.org.wxyc.lml`, with credentials in Railway as `APPLE_MUSIC_TEAM_ID` / `APPLE_MUSIC_KEY_ID` / `APPLE_MUSIC_PRIVATE_KEY`.

The migration ships as four stacked PRs so the auth client and its tests land separately from each call-site cutover. PR-1 introduces the new client + settings + tests without wiring it in (no production behavior change). PR-2 cuts `/streaming-check` over to the new client. PR-3 replaces `_fetch_apple_music_url` with `AppleMusicClient.find_track_url` (this is the PR that ends the user-visible cliff). PR-4 deletes the iTunes-era code and supersedes LML#444. Each PR runs its own staging smoke test before merge.

## Considered options

- **Rotate Railway egress IP**: short-term unblock, but Railway tenants share IPs and the block is likely to recur on the next reputational issue.
- **Proxy iTunes calls through a static-IP egress** (e.g. BS EC2): adds an operational dependency and a code path that throws itself away when migrating to auth properly.
- **User-Agent header probe**: verified to not affect the 403 (same UA succeeds from non-Railway IPs), ruled out.
- **Keep iTunes path as fallback**: provably broken from LML's only egress, so the "fallback" is dead code.

## Consequences

- Apple Developer Program membership becomes load-bearing for LML. Membership renewal failure becomes a category of outage to monitor.
- The `.p8` private key is downloaded once at MusicKit-key creation and is unrecoverable; loss requires revoke + re-issue with new Key ID.
- Local dev without `APPLE_MUSIC_*` env vars sees `apple_music_url=null` in lookup responses (matches existing Spotify-without-creds behavior).
- LML#444 (silent-failure observability) is superseded at PR-4 — the new client surfaces every non-200 via log + Sentry exception capture + a dedicated `apple_music.search` child span carrying status + result.
