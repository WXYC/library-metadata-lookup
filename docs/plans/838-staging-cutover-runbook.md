# #838 — Staging cutover runbook (volume → Railway Bucket)

Operator runbook for [WXYC/library-metadata-lookup#838](https://github.com/WXYC/library-metadata-lookup/issues/838), the staging leg of the volume-eviction epic [#834](https://github.com/WXYC/library-metadata-lookup/issues/834). Cuts the **staging** LML service from the `/data` volume to a per-environment Railway Bucket for `library.db` and `streaming_availability.db`.

**HITL.** Every step here flips live Railway infrastructure or reads live admin surfaces. Run it interactively, not from an AFK agent. Do not remove the volume (Phase 7) until every Phase 5 verification gate is green.

**Seed-then-hard-cutover** (epic decision 3): snapshot → seed the bucket out-of-band → flip variable refs → verify → remove volume. No dual-path transition code, no organic-fill window (that was the #672 two-lineages anti-pattern).

---

## Pre-flight risks (read before starting)

1. **Addressing style — configurable, defaults to virtual-hosted.** `S3ObjectStore` now defaults to **virtual-hosted** addressing (`<bucket>.<endpoint>`), which is what Railway serves for newly created buckets — so a fresh staging bucket needs no override. Only **legacy** (pre-change) buckets require path-style; for one of those, set `LML_BUCKET_ADDRESSING_STYLE=path` on the service in Phase 4. The bucket's Credentials tab states which style applies. (Addressing was a hard-pinned path-style assumption until [#855](https://github.com/WXYC/library-metadata-lookup/issues/855) / PR [#856](https://github.com/WXYC/library-metadata-lookup/pull/856); it is now a default plus an env override, so this can no longer stall the cutover.)
   - **Mitigation, by construction:** Phase 3 seeds the bucket by importing the app's *own* `S3ObjectStore` with the same setting the service will use. Any addressing mismatch therefore fails **on your laptop, before any service flip or volume change** — the cheapest place to catch it. If the seed fails with an addressing/DNS error, flip `LML_BUCKET_ADDRESSING_STYLE` (`virtual` ⇄ `path`) and re-seed.

2. **Region.** `S3ObjectStore` hardcodes `region_name="us-east-1"`; Railway's bucket `REGION` is `auto`. Runtime and the Phase 3 seed use the *same* hardcoded region, so if the seed succeeds the runtime client will too. No action unless the seed fails with a region error (then treat as item 1: fix in code, not in the runbook).

3. **Staging is not a sandbox for its shared dependencies.** LML staging shares the discogs-cache PostgreSQL and the Discogs API token with prod. This cutover touches neither — it only moves two SQLite artifacts to object storage — and **Railway Buckets are per-environment isolated instances**, so the staging bucket is genuinely staging-only. Nothing here can write prod data. (Reference: `reference_lml_staging_shares_prod`.)

4. **Never-delete rule.** Phase 1 snapshots are taken first and **retained**. Do not delete them at the end — they are the rollback source and the audit trail.

---

## Phase 0 — Prerequisites

- [ ] **PR [#854](https://github.com/WXYC/library-metadata-lookup/pull/854) (#837, library.db boot-fetch) is merged to `main` and deployed to staging.** This is #838's hard blocker: the boot-fetch that repopulates `library.db` from the bucket after a volume-less restart must already be live on staging. Confirm the staging deploy is from a commit that includes #837.
- [ ] You can reach the Railway project. LML lives in the **`request-o-matic`** Railway project; the service is **`library-metadata-lookup`**. Confirm the staging environment's exact name (assume `staging` below — verify in the dashboard / `railway environment`).
- [ ] Local checkout at repo root with `uv sync --extra dev` done (Phase 3 imports the app's `S3ObjectStore`).
- [ ] `aws`/S3 client and `gh` CLI available.

Set these once (fill in the staging values):

```bash
export LML_ENV=staging                       # confirm the real env name first
export LML_STAGING_URL=https://<lml-staging-domain>   # Railway service public domain
# Admin token for the staging service:
export ADMIN_TOKEN=$(railway variables --service library-metadata-lookup --environment "$LML_ENV" --kv | sed -n 's/^ADMIN_TOKEN=//p')
[ -n "$ADMIN_TOKEN" ] && echo "ADMIN_TOKEN loaded" || echo "ADMIN_TOKEN MISSING"
```

---

## Phase 1 — Snapshot both files locally (retain)

Take snapshots **before** any infra change; keep them.

```bash
mkdir -p ~/lml-838-snapshots && cd ~/lml-838-snapshots
```

**streaming_availability.db** — via the existing admin download endpoint:

```bash
curl -fsSL -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$LML_STAGING_URL/admin/download-streaming-db" \
  -o streaming_availability.db
ls -l streaming_availability.db
# sanity: it's a valid SQLite with an albums table
sqlite3 streaming_availability.db "SELECT count(*) FROM albums;"
```

**library.db** — there is **no** `download-library-db` admin endpoint (the #838 issue's "download both via the existing endpoints" wording is inaccurate for library.db). Its canonical hosted copy is the `streaming-data-v1` GitHub release asset — the same file the daily `sync-library.sh` uploads to LML, so it *is* the current staging catalog (modulo the last sync):

```bash
gh release download streaming-data-v1 --repo WXYC/library-metadata-lookup \
  --pattern library.db --dir .
ls -l library.db
sqlite3 library.db "SELECT count(*) FROM library;"
```

> If you need the byte-exact copy the staging replica is serving *right now* (not the release copy), pull `/data/library.db` off the running container via `railway ssh` into the staging service and copy it out. In practice the release asset is the intended seed and is sufficient — the boot-fetch will serve whatever we seed, and the next daily sync overwrites it anyway.

- [ ] Both snapshots present, valid SQLite, non-trivial row counts. **Retain this directory.**

---

## Phase 2 — Create the staging bucket + read credentials

1. In the Railway **`request-o-matic`** project, **`staging`** environment, create a **Bucket** (canvas → **+ New** → **Bucket**). Pick a region (immutable after creation). Name it recognizably, e.g. `lml-staging`.
2. Open the bucket's **Credentials** tab. Record — and note the **URL style** it tells you to use (this resolves pre-flight risk #1):

   | Railway var         | Meaning                                  | Use as |
   | ------------------- | ---------------------------------------- | ------ |
   | `BUCKET`            | S3 bucket name (display name + hash)     | `LML_BUCKET_NAME` |
   | `ENDPOINT`          | S3 endpoint, e.g. `https://storage.railway.app` | `LML_BUCKET_ENDPOINT` |
   | `ACCESS_KEY_ID`     | S3 access key id                         | `AWS_ACCESS_KEY_ID` |
   | `SECRET_ACCESS_KEY` | S3 secret key                            | `AWS_SECRET_ACCESS_KEY` |
   | `REGION`            | `auto`                                   | (informational; client uses `us-east-1`) |

- [ ] Bucket created in the **staging** environment. Credentials + **addressing style** recorded. If the Credentials tab says **path-style** (a legacy bucket), note it — you'll set `LML_BUCKET_ADDRESSING_STYLE=path` in Phase 4 and use the same in the Phase 3 seed. A new bucket is virtual-hosted (the default); no override needed.

---

## Phase 3 — Seed the bucket (out-of-band, using the app's own client)

Seeding with the repo's `S3ObjectStore` guarantees the seed and the runtime boot-fetch use **identical** addressing/region — and surfaces any addressing incompatibility here, on your laptop, before the service is touched.

From the repo root, with the Phase 2 credentials exported:

```bash
export AWS_ACCESS_KEY_ID=<bucket ACCESS_KEY_ID>
export AWS_SECRET_ACCESS_KEY=<bucket SECRET_ACCESS_KEY>
export SEED_BUCKET=<bucket BUCKET>
export SEED_ENDPOINT=<bucket ENDPOINT>
export SEED_ADDRESSING=virtual        # set to "path" ONLY for a legacy path-style bucket (Phase 2)
export SNAP=~/lml-838-snapshots

uv run --no-sync python - <<'PY'
import asyncio, os
from pathlib import Path
from storage.object_store import S3ObjectStore

store = S3ObjectStore(
    bucket=os.environ["SEED_BUCKET"],
    endpoint_url=os.environ["SEED_ENDPOINT"],
    addressing_style=os.environ.get("SEED_ADDRESSING", "virtual"),
)
snap = Path(os.environ["SNAP"])

async def main():
    for key in ("library.db", "streaming_availability.db"):
        src = snap / key
        await store.put(key, src)                    # multipart streaming from disk
        stat = await store.head(key)                 # read-back
        assert stat is not None, f"{key} missing after put"
        assert stat.size == src.stat().st_size, f"{key} size mismatch: {stat.size} != {src.stat().st_size}"
        print(f"seeded {key}: {stat.size} bytes, etag={stat.etag}")

asyncio.run(main())
print("SEED OK")
PY
```

- [ ] `SEED OK` printed; both objects present with byte sizes matching the local snapshots. (A path-style/addressing failure surfaces here — see pre-flight risk #1.)

---

## Phase 4 — Enable bucket mode on the staging service

Bucket mode is active iff **both** `LML_BUCKET_NAME` and `LML_BUCKET_ENDPOINT` are set (`Settings.bucket_mode`). boto3 reads the credentials from the standard `AWS_*` names natively.

On the **`library-metadata-lookup`** service, **`staging`** environment, set as **variable references** to the bucket (preferred — auto-updates if the bucket rotates), or as plain values if references aren't available:

- `LML_BUCKET_NAME`  → bucket `BUCKET`
- `LML_BUCKET_ENDPOINT` → bucket `ENDPOINT`
- `AWS_ACCESS_KEY_ID` → bucket `ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY` → bucket `SECRET_ACCESS_KEY`

Railway's bucket **auto-inject** feature (Variables tab → add bucket credentials, AWS SDK preset) provisions the `AWS_*` pair for you; then add the two `LML_BUCKET_*` references by hand. **Only if the bucket is legacy path-style** (Phase 2), also set `LML_BUCKET_ADDRESSING_STYLE=path`; a new virtual-hosted bucket needs nothing here (the default is `virtual`). Keep the `/data` volume **mounted** for now (removed in Phase 7 only after verification).

Deploy the service (push to `main` is not required — a variable change triggers a redeploy; or `railway up`/redeploy). Watch the deploy logs for:

```
Object store: S3 bucket mode (bucket=<...>)
Boot-fetched library.db from the object store (<N> bytes) -> <path>
```

- [ ] Variables set on the **staging** service. Deploy logs show **bucket mode** + a successful **boot-fetch** (not the degraded `library.db absent`/`serving degraded` path).

---

## Phase 5 — Verify (all gates must pass before Phase 7)

```bash
# 1. Health green (proves boot-fetch populated library.db from the bucket)
curl -fsS "$LML_STAGING_URL/health" | python3 -m json.tool

# 2. streaming_availability.db round-trips byte-identical
curl -fsSL -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$LML_STAGING_URL/admin/download-streaming-db" -o /tmp/roundtrip-streaming.db
cmp ~/lml-838-snapshots/streaming_availability.db /tmp/roundtrip-streaming.db \
  && echo "streaming round-trip: IDENTICAL"

# 3. A live lookup returns results (library.db is being served from the bucket copy)
curl -fsS -X POST "$LML_STAGING_URL/api/v1/lookup" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <LML_API_KEY if LML_REQUIRE_AUTH on staging>" \
  -d '{"artist":"Jessica Pratt","album":"On Your Own Love Again","raw_message":"Jessica Pratt - On Your Own Love Again"}' \
  | python3 -m json.tool | head -40
```

**4. Coverage guard is live** — a force-less *thin* streaming upload must be rejected **409**. Build a deliberately thin (1-row `albums`) DB and confirm rejection (it is rejected, so nothing is stored):

```bash
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/tmp/thin-streaming.db")
c.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, apple_url TEXT, spotify_url TEXT, deezer_url TEXT)")
c.execute("INSERT INTO albums (id) VALUES (1)")
c.commit(); c.close()
print("thin DB built")
PY

# Expect HTTP 409 with detail.error == "streaming coverage regression". Do NOT pass force=true.
curl -s -o /tmp/thin-resp.json -w "%{http_code}\n" -X POST \
  "$LML_STAGING_URL/admin/upload-streaming-db" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F "file=@/tmp/thin-streaming.db"
cat /tmp/thin-resp.json | python3 -m json.tool
```

- [ ] `/health` 200/healthy
- [ ] streaming round-trip IDENTICAL
- [ ] live lookup returns a result
- [ ] thin upload → **409** (`detail.error == "streaming coverage regression"`), nothing stored

---

## Phase 6 — Observe a zero-downtime deploy

The payoff of the epic: a volume-less service redeploys without the `ConnectError` blip that surfaces as request-o-matic "Search unavailable". Run a request loop across one staging redeploy and confirm no failures.

```bash
# In one terminal — hammer /health (or a lookup) through a redeploy:
END=$((SECONDS+180))
fail=0; ok=0
while [ $SECONDS -lt $END ]; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$LML_STAGING_URL/health")
  [ "$code" = "200" ] && ok=$((ok+1)) || { fail=$((fail+1)); echo "non-200: $code @ $(TZ=America/Los_Angeles date +%H:%M:%S)"; }
  sleep 1
done
echo "ok=$ok fail=$fail"
# Meanwhile: trigger a redeploy of the staging service from the Railway dashboard.
```

- [ ] Redeploy observed; `fail=0` (or only the expected in-flight cutover window, documented).

---

## Phase 7 — Remove the staging volume

Only after every Phase 5 gate is green and Phase 6 looks clean.

1. On the **staging** `library-metadata-lookup` service, detach/remove the `/data` volume mount (Railway dashboard → service → Settings/Volumes, or `railway volume`/MCP `remove_volume`).
2. Redeploy. Confirm the service still boots (boot-fetch repopulates `library.db` from the bucket, streaming reads from the bucket) and `/health` is green with **no** volume attached.

- [ ] Staging volume removed; service healthy with no volume; boot-fetch log present on the volume-less boot.

---

## Phase 8 — Soak before prod (#839)

Leave staging in bucket mode and let the real writers exercise it:

- [ ] At least one daily `sync-library.sh` run (discogs-etl) completes and its `POST /admin/upload-library-db` succeeds — confirm the write-through put-first path lands the new `library.db` in the staging bucket and hot-swaps the local file (row count changes; log line `Library database replaced`).
- [ ] Ideally one weekly `refresh-streaming.yml` run round-trips `streaming_availability.db` through the bucket (upload accepted, coverage guard not tripped).

Only after a clean soak, proceed to **#839 (prod cutover)** with this same runbook against the `production` environment.

---

## Rollback

Bucket mode is a pure variable toggle; the volume stays until Phase 7, so rollback before Phase 7 is instant.

- **Before Phase 7 (volume still mounted):** unset `LML_BUCKET_NAME` + `LML_BUCKET_ENDPOINT` on the staging service and redeploy. `bucket_mode` goes False → `LocalDirStore` over `/data` → exactly today's behavior. The volume copy is untouched.
- **After Phase 7 (volume removed):** re-add the volume mount, then `POST /admin/upload-library-db` with the retained Phase 1 `library.db` snapshot and `POST /admin/upload-streaming-db` with the retained streaming snapshot (round-trip, no `force`), or simply unset the bucket vars and let the next daily sync repopulate `/data`. The retained snapshots are the recovery source — **do not delete them** until prod (#839) is also cut over and soaked.
- **Boot-fetch failure at any point** is non-fatal by design (#837): the service boots degraded, `/health` 503s, and `POST /admin/upload-library-db` stays available as the recovery path. It does not crash-loop.

---

## Acceptance criteria (mirrors #838)

- [ ] Local snapshots of both files taken and retained before any infra change
- [ ] Staging bucket seeded; variable references set; service healthy in bucket mode
- [ ] Round-trip + live-lookup + 409-guard verification all pass
- [ ] Staging volume removed; zero-downtime deploy observed via request loop
- [ ] Soak started (daily library sync + ideally weekly streaming refresh)
</content>
</invoke>
