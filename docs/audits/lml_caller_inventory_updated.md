# LML Caller Inventory (Updated)

Refreshed inventory for M0.4. Compares against the 11-site list in the parent epic's plan and surfaces any new callers added since the epic was drafted.

## Scan command

Run from `/Users/jake/Developer/WXYC` (the org root):

```
rg -l --no-ignore --hidden \
  -e 'library-metadata-lookup' \
  -e 'LIBRARY_METADATA_URL' \
  -e '/api/v1/lookup' \
  -e 'lml\.client' \
  --type-add 'src:*.{ts,js,py,java,kt,swift,sh}' -t src
```

Filter out: node_modules, `.git`, `.venv`, `dist/`, `build/`, `.next/`, generated `.egg-info`, repo worktrees (`-watchdog-*`, `-auto-deploy`, `-bugfix-*`, `-utf8-fix`, `-auth-dep`, `-db-backup-step`), the `library-metadata-lookup` repo itself, and historical references in markdown.

## Plan inventory vs. current state

| # | Repo | File | Caller | Status |
|---|---|---|---|---|
| 1 | Backend-Service | `apps/backend/services/lml/lml.client.ts` | TS LML client | ✅ matches plan |
| 2 | Backend-Service | `apps/backend/controllers/proxy.controller.ts` | `/proxy/library/search` | ✅ matches plan |
| 3 | Backend-Service | `apps/backend/services/metadata/metadata.service.ts` | Track metadata | ✅ matches plan |
| 4 | Backend-Service | `apps/backend/services/requestLine/requestLine.enhanced.service.ts` | Request-line search | ✅ matches plan |
| 5 | Backend-Service | `apps/backend/services/artwork/providers/discogs.ts` | Artwork via LML's Discogs proxy | ✅ matches plan |
| 6 | Backend-Service | `scripts/backfill-metadata.ts` | One-shot backfill | ✅ matches plan |
| 7 | tubafrenzy | `libs/core/src/main/java/.../library/StreamingCheckLibraryReleaseListener.java` (resolved as the streaming-check trigger; the HTTP path is `LibrarySearchClient`) | Streaming-check on release add | ✅ matches plan |
| 8 | tubafrenzy *(in-progress branch `tubafrenzy-lml-resolve`)* | `webapps/playlists/.../servlets/{Artist,Release,Song}AutocompleteServlet.java`, `LibrarySearchClient.java` | Autocomplete + release search | ✅ matches plan |
| 9 | discogs-etl | `scripts/sync-library.sh` | Daily upload of `library.db` to LML | ✅ matches plan |
| 10 | semantic-index | `run_pipeline.py` (`--entity-source=lml`) + `semantic_index/lml_identity.py` | Identity resolution during graph build (PG, not HTTP) | ✅ matches plan |
| 11 | request-o-matic | `services/lookup_client.py` | Song-request dispatch | ✅ matches plan |

## New callers (not in the plan inventory)

| # | Repo | File | Caller | Notes |
|---|---|---|---|---|
| 12 | **archive** | `app/api/artwork/route.ts` | `POST /lookup` for album artwork on the archive playback page | New since the epic was drafted. UTF-8 safe. Add to M2.x propagation checklist. |
| 13 | **semantic-index** | `semantic_index/discogs_client.py` | `GET /discogs/release/{id}` HTTP fallback after the discogs-cache PG miss | Wasn't called out separately in the plan because semantic-index is listed once via `run_pipeline.py`; it's the same repo and already covered by the static audit row. Listed here for completeness. |

## Adjacent references (not callers)

These hits surfaced under the grep but aren't runtime LML callers:

- `Backend-Service/jobs/artist-identity-etl/fetch-lml.ts` — schema-shared comment + reads `entity.identity` directly via `DATABASE_URL_DISCOGS`. Not an HTTP caller. Same propagation concern as semantic-index #10.
- `Backend-Service/dev_env/mock-api-server/src/{routes/lml.ts,server.ts}` — local mock server, not production traffic.
- `Backend-Service/tests/{e2e,integration,unit}/...` — caller's own tests.
- `wxyc-shared/{e2e/proxy.test.ts,railway/setup-environment.sh}` — test + env-setup script. No runtime calls.
- `archive/lib/types/playlist.ts` — comment-only reference.
- `metadata-proxy-migration/orchestrate.ts` — orchestrator config.
- Various `*.next/` / `dist/` build outputs — derived from canonical sources above.

## Flags for downstream phases

- **M2.x propagation must include archive** (caller #12). After V012 + sync-library.sh propagate corrected names into `library.db`, the archive playback page will start showing the corrected artist names automatically — no archive-side change needed unless the page caches LML responses (Next.js `revalidate: 3600` is set, so worst case is a 1h stale window).
- **Backend-Service `fetch-lml.ts`** reads `entity.identity` directly. M2.2 (semantic-index reconciliation) and any LML-side reconciliation of `entity.identity` should be sequenced before the next artist-identity ETL run; otherwise the ETL will pick up stale corrupted names.
