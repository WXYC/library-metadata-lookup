# LML structural audit — 2026-06-13

Cross-cutting structural analysis run via [code-audit-pipeline](https://github.com/jakebromberg/code-audit-pipeline) using a newly-built Python extractor (stdlib `ast`, no third-party deps) on branch `feat/python-extractor` ([PR #272](https://github.com/jakebromberg/code-audit-pipeline/pull/272), tracking issue [#271](https://github.com/jakebromberg/code-audit-pipeline/issues/271)). 307 source files indexed; 867 type records and 3552 function records cataloged with zero parse errors.

This is *cluster signal, not bug list*. Each finding lists the structural evidence; intent verification is up to the reader.

## Methodology

```bash
mkdir -p /tmp/wxyc-audit/lml && cd /tmp/wxyc-audit/lml

# Catalogs
python3 .../extractors/python/type_catalog.py \
    --root /Users/jake/Developer/WXYC/library-metadata-lookup \
    --output type-catalog.json
python3 .../extractors/python/function_catalog.py \
    --root /Users/jake/Developer/WXYC/library-metadata-lookup \
    --output function-catalog.json

# Queries (production-only, is_test filter pre-applied)
QUERIES=.../pipeline/queries
jq -L $QUERIES -rf $QUERIES/exact-duplicates.jq    type-catalog.json
jq -L $QUERIES -rf $QUERIES/name-collisions.jq     type-catalog.json
jq -L $QUERIES -r --argjson threshold 0.7 -f $QUERIES/near-duplicates.jq      type-catalog.json
jq -L $QUERIES -r --argjson threshold 0.8 -f $QUERIES/function-duplicates.jq  function-catalog.json
```

## Top findings

### 1. `BandcampResolveResult` ≡ `DiscogsResolveResult` — exact-duplicate dataclass

| | location |
|---|---|
| `BandcampResolveResult` | `release/bandcamp_resolver.py:39` |
| `DiscogsResolveResult`  | `release/discogs_resolver.py:30` |

Both are 3-field dataclasses with the identical shape:

```python
canonical: CanonicalRelease | None
identifiers: ReleaseIdentifiers
warnings: list[str]
```

The docstrings differ only in the source name ("Bandcamp album URL" vs "Discogs release ID"). Strong candidate for a single `ResolveResult` dataclass in `release/models.py`, optionally with a `source: Literal["bandcamp", "discogs"]` discriminator if downstream code needs to know the origin without unwrapping `identifiers`.

### 2. `Identity` ↔ `IdentityResponse` — 88% near-duplicate (the canonical / API split)

| | location | shape |
|---|---|---|
| `Identity` (dataclass)         | `entity/store.py:26`      | adds `id: int`; the DB row PK |
| `IdentityResponse` (Pydantic)  | `identity/models.py:6`    | same 8 fields, no `id` |

The two are otherwise field-for-field equivalent — `library_name`, `discogs_artist_id`, `wikidata_qid`, `musicbrainz_artist_id`, `spotify_artist_id`, `apple_music_artist_id`, `bandcamp_id`, `reconciliation_status` — same names, same nullability, same default (`"unreconciled"` for the status).

Today nothing bridges them, so any field added to one must be added to the other by hand. Two reasonable directions:

1. **Single Pydantic source of truth.** `Identity` becomes a Pydantic model; the response surface is `Identity.model_dump(exclude={"id"})` (or a thin `IdentityResponse(Identity)` subclass that re-declares no fields). Cuts the parallel-update tax to zero.
2. **Derive the response from the dataclass.** `IdentityResponse` becomes `IdentityResponse(BaseModel): @classmethod from_identity(cls, i: Identity) -> "IdentityResponse"`. Trades one method for not migrating the store layer.

Also see `ReconciledIdentity` (`generated/api_models.py:811`, 75% match) — the generated API model is missing `library_name` and `reconciliation_status`. If those are intentionally hidden from the public API, document why; if not, the `wxyc-shared/api.yaml` spec should be widened.

### 3. Provenance row — three near-identical shapes

| name | location | shape |
|---|---|---|
| `_ComposedRow`              | `identity/bulk_resolve.py:99`     | `source, method, confidence, external_id, is_inherited` |
| `ProvenanceRow`             | `entity/store.py:233`             | `source, method, confidence, external_id` |
| `BulkResolveProvenanceEntry`| `generated/api_models.py:978`     | `source, method, confidence, external_id` |

Three parallel shapes for the same concept. The local `_ComposedRow` adds `is_inherited` (per its docstring: "Per-source row, post Rule 6 floor and method/confidence resolution"); the store-layer `ProvenanceRow` and the generated API model are byte-identical.

Direction: promote a single `ProvenanceRow` from `entity/store.py` (it's already the canonical source), have `bulk_resolve` subclass or compose with `is_inherited` as an additional field. The generated API model is downstream of `wxyc-shared/api.yaml` — track that the spec mirrors the canonical form.

### 4. `LibraryItem` ↔ `LibraryCatalogItem` — internal vs API drift (76%)

| | location | adds / drops |
|---|---|---|
| `LibraryItem` (internal)         | `library/models.py:20`           | smaller; missing `library_url`, `call_number` |
| `LibraryCatalogItem` (API)       | `generated/api_models.py:846`    | adds `library_url`, `call_number` |
| `LibrarySearchItem` (API search) | `generated/api_models.py:1279`   | adds `alternate_artist_name`, `matched_via`, `matched_via_alias` |

Per LML's `CLAUDE.md`: "internal domain models (`LibraryItem`, `DiscogsSearchResult`) are converted via `to_catalog_item()` / `to_match_result()` methods." Verify that `to_catalog_item()` synthesizes the `library_url` and `call_number` fields rather than dropping them silently — if the conversion is lossy, the API is starving its consumers of two known-good fields the catalog presents.

### 5. `SpotifyClient` defined twice — production code

| | location |
|---|---|
| `SpotifyClient` (the real one)   | `clients/streaming/spotify.py:17`         |
| `SpotifyClient` (in a script)    | `scripts/spotify_artist_catalog.py:43`    |
| `SpotifyValidator` (third copy)  | `scripts/revalidate_spotify.py`           |

Function-duplicate output confirms it:

```
[6 lines, 2 decls] cid=function-duplicates-exact:...spotify_artist_catalog.py:50:SpotifyClient._get_token+...revalidate_spotify.py:47:SpotifyValidator._get_token
[4 lines, 2 decls] cid=function-duplicates-exact:...spotify_artist_catalog.py:44:SpotifyClient.__init__+...revalidate_spotify.py:41:SpotifyValidator.__init__
```

Both script copies have byte-identical `__init__` and `_get_token` implementations. Direction: `scripts/spotify_artist_catalog.py` and `scripts/revalidate_spotify.py` should depend on `clients.streaming.spotify.SpotifyClient` instead of carrying their own — or that client should be promoted to `clients/streaming/spotify_auth.py` if the script paths need a leaner surface.

### 6. `ResultsDB` defined twice — independent SQLite wrappers

| | location |
|---|---|
| `ResultsDB` | `scripts/streaming_availability/results_db.py:82` |
| `ResultsDB` | `scripts/va_disambiguate/results_db.py:63`        |

Two independent SQLite results-DB wrappers in two scripts. Worth checking whether they share enough surface (connect, ensure-table, upsert-row, query-row) to extract a common base in `scripts/_lib/results_db.py`.

### 7. `_handle_signal` — quadruplicate

5–6 line `signal.signal(SIGINT, …)` setup, byte-identical, across:

- `scripts/streaming_availability/__main__.py:48`
- `scripts/track_streaming/__main__.py:37`
- `scripts/discogs_rematch.py:34`
- `scripts/search_unmatched_compilations.py:35`

Direction: hoist to `scripts/_lib/signals.py` (or wherever the scripts package keeps shared helpers).

### 8. `main()` entrypoint — duplicate boilerplate

`scripts/streaming_availability/__main__.py:901` and `scripts/track_streaming/__main__.py:558` share an 8-line `main()` body (argparse setup + `asyncio.run`). Likely a candidate for a small `scripts/_lib/runner.py` helper.

### 9. Test fixture duplication (lower priority)

- `pg_pool` async fixture appears in 7 integration tests with byte-identical bodies. Promote to `tests/integration/conftest.py`.
- `app_client` async fixture: 12 lines, identical across `tests/integration/test_bulk_resolve_libraries.py:87` and `tests/integration/test_release_identity.py:276`.
- `mock_mb_pg` / `mock_wikidata_pg` fixtures: same 3-4 line body, three copies across `tests/unit/test_external_artist_search.py`, `test_external_release_search.py`, `test_musicbrainz_reconciliation.py`, `test_wikidata_reconciliation.py`.

### 10. Intentional duplication — tagged-union arms (false-positive cluster)

`discogs/markup_parser.py` declares 11 `@dataclass(frozen=True)` arms of the `_DiscogsToken` union (`_PlainText`, `_ArtistName`, `_ArtistId`, `_MasterId`, `_ReleaseId`, `_LabelName`, `_Bold`, `_Italic`, `_Underline`, `_Url`). Some share shape (e.g. `_Bold/_Italic/_Underline` all have `content: str`; `_ArtistId/_MasterId/_ReleaseId` all have `id: int`). These show up in exact-duplicates clusters but are **intentional** — they're nominal arms used in `match`-statement dispatch. No action.

### 11. Generated-vs-generated drift (out of scope locally)

`generated/api_models.py` has multiple near-duplicate Flowsheet*, Schedule*, and Discogs* models — pairs like `FlowsheetEntryResponse`/`FlowsheetV2TrackEntry` (84%), `Rotation`/`RotationEntry` (80%), `DiscogsTrack`/`DiscogsTrackItem` (75%). These are codegen output; the fix lives in `wxyc-shared/api.yaml`, not LML. Flagged here for completeness; not LML-actionable.

## What this didn't surface

Cross-package shadow detection requires `--shared` to be set. The audit ran without it because `wxyc-shared` is a JavaScript/TypeScript package — `wxyc-shared/src/` has no `.py` files. The Pydantic-shaped surface that *would* be the natural `--shared` target lives **inside LML** at `generated/api_models.py`, generated from `wxyc-shared/api.yaml`. Drift between local models (`identity/models.py`, `library/models.py`) and the generated layer is what surfaced in findings #2, #3, and #4 above; cross-package shadows would only have changed the framing.

If LML ever adopts a separate Python package for shared models (decoupling the generator from this service), re-run with `--shared /path/to/wxyc-python-shared`.

## Reproducibility

Artifacts under `/tmp/wxyc-audit/lml/`:
- `type-catalog.json` — 867 entries
- `function-catalog.json` — 3552 entries
- `file-hashes.json` — 144 entries (no byte-equal `.py` files; the duplication is at AST level, not file level)

Re-run with the command block at the top of this document.
