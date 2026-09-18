"""Admin endpoints for service management."""

import asyncio
import logging
import os
import sqlite3
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse, Response

from config.settings import Settings, get_settings
from core.auth import require_admin_token
from core.dependencies import (
    close_library_db,
    get_discogs_cache_service_from_pool,
    get_object_store,
)
from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from discogs.memory_cache import evict_cached
from discogs.service import DiscogsService
from storage.object_store import ObjectNotFoundError, ObjectStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

STREAMING_DB_FILENAME = "streaming_availability.db"
LIBRARY_DB_FILENAME = "library.db"

# The single fixed backup key for library.db (LML#1313). One generation, and
# overwriting it *is* the rotation — bounded by construction, so there is no
# lifecycle policy to configure, nothing to garbage-collect, and the ObjectStore
# Protocol needs no `delete`. One generation covers the failure this exists for:
# a bad upload a human notices within a day. Restore path in docs/deployment.md.
LIBRARY_DB_PREVIOUS_FILENAME = "library.db.previous"

# Relative row-count guard for /admin/upload-library-db (LML#1313). Same rule and
# same tolerance as STREAMING_COVERAGE_TOLERANCE: 5% absorbs genuine deaccession
# churn while catching a catalog that arrived a fraction of its real size.
LIBRARY_ROW_DROP_TOLERANCE = 0.05

# The metric name the library guard reports through _check_count_regression, and
# the key that appears in a 409's `regressions` records.
LIBRARY_ROW_METRIC = "library_rows"

# Coverage-regression guard tolerance for /admin/upload-streaming-db (LML#672).
# An upload is rejected if any guarded metric drops below prior * (1 - tolerance)
# or goes non-zero -> zero. 5% absorbs legitimate churn (a release falling off a
# service, a removed library row) while still catching a stripped column.
STREAMING_COVERAGE_TOLERANCE = 0.05

# The streaming_availability.db metrics guarded on upload. The three URL counts
# live on the `albums` table; `track_results` is a separate table that
# export_streaming_links.py also consumes, so its loss must be caught too.
_STREAMING_COVERAGE_METRICS = ("apple_url", "spotify_url", "deezer_url", "albums", "track_results")

# Strong references to background tasks to prevent GC before completion.
# See https://docs.python.org/3/library/asyncio-task.html#creating-tasks
_background_tasks: set[asyncio.Task] = set()


class StreamingCoverageUnreadableError(Exception):
    """A streaming_availability.db object exists but could not be read.

    Distinguishes "no prior object" (a legitimate first upload) from "prior object
    present but unreadable" so the upload guard can fail *closed* on the latter:
    waving an upload through because the baseline read failed would re-open the
    288-Apple-URLs -> 0 hole the guard exists to plug, precisely when the stored
    object is corrupt.
    """


def _streaming_coverage(db_path: Path) -> dict[str, int]:
    """Coverage metrics for a streaming_availability.db file (LML#672 guard).

    Always returns the same five fixed keys (see ``_STREAMING_COVERAGE_METRICS``).
    A missing file returns all-zero (the first-upload baseline). A missing
    ``albums``/``track_results`` table, or a missing URL column, reads as ``0``
    rather than raising, so the regression check can iterate without ``KeyError``
    and a disappearing table/column is caught as an N -> 0 regression.

    Raises:
        StreamingCoverageUnreadableError: the file exists but the read failed (corrupt
            file, locking error, I/O fault). Callers decide how to react -- the
            upload guard fails closed (409) rather than treating an unreadable
            baseline as "nothing to regress against".
    """
    zero = dict.fromkeys(_STREAMING_COVERAGE_METRICS, 0)
    if not db_path.exists():
        return zero
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cov = dict(zero)
            if _has_table(conn, "albums"):
                cov["albums"] = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
                album_cols = _table_columns(conn, "albums")
                for col in ("apple_url", "spotify_url", "deezer_url"):
                    if col in album_cols:
                        cov[col] = conn.execute(
                            f"SELECT COUNT({col}) FROM albums"  # noqa: S608 (col from fixed allowlist)
                        ).fetchone()[0]
            if _has_table(conn, "track_results"):
                # Count only *usable* track rows -- resolved and URL-bearing -- to
                # mirror the predicate export_streaming_links.py applies. The
                # pipeline inserts a row per track up front (resolution_status
                # 'pending', NULL URLs) and fills URLs in later, so a raw COUNT(*)
                # would let an upload that nulls every track URL slip past the
                # guard. Fall back to COUNT(*) on a legacy/partial schema missing
                # any of these columns (over-counting only makes the guard harder
                # to trip falsely) so a disappearing table is still caught as N->0.
                track_cols = _table_columns(conn, "track_results")
                if {"resolution_status", "spotify_url", "deezer_url"} <= track_cols:
                    cov["track_results"] = conn.execute(
                        "SELECT COUNT(*) FROM track_results "
                        "WHERE resolution_status IN ('local_match', 'api_match') "
                        "AND (spotify_url IS NOT NULL OR deezer_url IS NOT NULL)"
                    ).fetchone()[0]
                else:
                    cov["track_results"] = conn.execute(
                        "SELECT COUNT(*) FROM track_results"
                    ).fetchone()[0]
            return cov
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Failed to read streaming coverage from %s", db_path, exc_info=True)
        raise StreamingCoverageUnreadableError(str(db_path)) from e


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}


def _check_count_regression(
    old: dict[str, int],
    new: dict[str, int],
    tolerance: float = STREAMING_COVERAGE_TOLERANCE,
    metrics: Sequence[str] = _STREAMING_COVERAGE_METRICS,
) -> list[dict]:
    """Return regression records for metrics that shrank too far.

    For each named metric, a record ``{metric, old, new, floor}`` is returned when
    ``new`` drops below ``floor = old * (1 - tolerance)`` **or** goes non-zero ->
    zero. Empty list means the upload is safe. Pure function; both inputs are
    expected to carry every key in ``metrics``.

    Written for the streaming-coverage guard (LML#672), whose five fixed metrics
    are the default, and reused verbatim by the ``library.db`` row-count guard
    (LML#1313) with ``metrics=(LIBRARY_ROW_METRIC,)`` — one rule for "did this
    upload shrink too far", not two.
    """
    regressions: list[dict] = []
    for metric in metrics:
        old_v = old.get(metric, 0)
        new_v = new.get(metric, 0)
        if old_v <= 0:
            continue  # nothing to regress against (first upload / absent metric)
        floor = old_v * (1 - tolerance)
        if new_v == 0 or new_v < floor:
            regressions.append({"metric": metric, "old": old_v, "new": new_v, "floor": floor})
    return regressions


def _library_row_count(db_path: Path) -> int:
    """Rows in a library.db's ``library`` table; ``0`` when absent or unreadable.

    The baseline half of the LML#1313 relative guard, read from the *serving*
    replica's local file — the copy an upload is about to replace — so no object
    fetch is needed to know what is being traded away.

    Unlike ``_streaming_coverage``, an unreadable file degrades to ``0`` (i.e. "no
    baseline", which the regression check skips) instead of raising to fail the
    upload closed. That asymmetry is deliberate: an unreadable local catalog means
    this replica is *already* degraded and this endpoint is its recovery path, so
    failing closed would put recovery behind ``force=true``. The absolute floor
    still applies, and it is the guard that does not depend on prior state.
    """
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            return int(conn.execute("SELECT count(*) FROM library").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        logger.warning(
            "Failed to read a row-count baseline from the served %s; "
            "the relative upload guard has nothing to compare against.",
            db_path,
            exc_info=True,
        )
        return 0


def _get_streaming_ids(db_path: Path) -> set[int]:
    """Read the set of library_ids from streaming_links in a SQLite file.

    Returns an empty set if the file does not exist or lacks a streaming_links table.
    """
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(str(db_path))
        has_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='streaming_links'"
        ).fetchone()
        if not has_table:
            conn.close()
            return set()
        rows = conn.execute("SELECT library_id FROM streaming_links").fetchall()
        conn.close()
        return {row[0] for row in rows}
    except Exception:
        logger.warning("Failed to read streaming_links from %s", db_path, exc_info=True)
        return set()


def _compute_streaming_diff(old_ids: set[int], new_ids: set[int]) -> list[dict]:
    """Compute streaming status changes between old and new ID sets.

    Returns a list of dicts sorted with additions first (ascending), then removals
    (ascending), for deterministic output.
    """
    changes: list[dict] = []
    for lib_id in sorted(new_ids - old_ids):
        changes.append({"library_release_id": lib_id, "on_streaming": True})
    for lib_id in sorted(old_ids - new_ids):
        changes.append({"library_release_id": lib_id, "on_streaming": False})
    return changes


async def _send_streaming_webhook(
    webhook_url: str,
    notify_key: str | None,
    changes: list[dict],
) -> dict:
    """POST streaming changes to a single webhook URL.

    Returns a status dict with url, status ('sent' or 'failed'), and either
    changes_count or error.
    """
    payload = {
        "changes": changes,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    headers: dict[str, str] = {}
    if notify_key:
        headers["Authorization"] = f"Bearer {notify_key}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=payload, headers=headers)
            resp.raise_for_status()
            return {"url": webhook_url, "status": "sent", "changes_count": len(changes)}
    except Exception as e:
        logger.warning("Streaming webhook to %s failed: %s", webhook_url, e, exc_info=True)
        return {"url": webhook_url, "status": "failed", "error": str(e)}


async def _send_streaming_webhooks(
    webhook_urls: str,
    notify_key: str | None,
    changes: list[dict],
) -> list[dict]:
    """Send streaming changes to all comma-separated webhook URLs concurrently."""
    urls = [u.strip() for u in webhook_urls.split(",") if u.strip()]
    tasks = [_send_streaming_webhook(url, notify_key, changes) for url in urls]
    results = list(await asyncio.gather(*tasks))
    logger.info(
        "Streaming webhook complete: %d changes to %d URLs: %s",
        len(changes),
        len(results),
        ", ".join(f"{r['url']} -> {r['status']}" for r in results),
    )
    return results


@router.post(
    "/upload-library-db",
    summary="Upload a new library.db file",
    responses={
        200: {"description": "Upload successful"},
        400: {
            "description": "Invalid SQLite database, or below the absolute row floor "
            "(`LIBRARY_DB_MIN_ROWS`). The floor variant's JSON `detail` carries "
            "`row_count` and `required_min_rows`."
        },
        401: {"description": "Missing authorization"},
        403: {"description": "Invalid or missing token"},
        409: {
            "description": "Refused (use force=true): the upload drops more than "
            f"{LIBRARY_ROW_DROP_TOLERANCE:.0%} of the rows in the currently-served copy. "
            "`detail` is `{error, regressions, hint}`."
        },
        500: {"description": "Server-side fault writing the file or the object store"},
    },
    dependencies=[Depends(require_admin_token)],
)
async def upload_library_db(
    file: UploadFile,
    force: bool = False,
    settings: Settings = Depends(get_settings),
    object_store: ObjectStore = Depends(get_object_store),
):
    """Replace the library.db file with an uploaded SQLite database.

    Two size guards run before anything is written (LML#1313), because until this
    ticket the endpoint accepted any file that parsed and kept no way back:

    * **Absolute floor** -- fewer than ``LIBRARY_DB_MIN_ROWS`` rows in ``library``
      is a **400** naming both the observed and the required count. Set the
      setting to ``0`` to opt out.
    * **Relative guard** -- dropping more than ``LIBRARY_ROW_DROP_TOLERANCE`` of
      the rows in the copy this replica is currently serving is a **409** carrying
      the same ``regressions`` record shape ``/admin/upload-streaming-db`` uses.

    ``?force=true`` overrides both, loudly, for a legitimate large shrink. A
    rejection writes nothing: the served file stays byte-identical, the stored
    object is untouched, and the scratch upload file is removed.

    On the way through, the outgoing stored object is preserved under
    ``library.db.previous`` via the store's server-side
    :meth:`~storage.object_store.ObjectStore.copy` (never a get-then-put through
    this process), giving one generation to restore from -- see
    ``docs/deployment.md`` for the restore procedure.

    The uploaded file is validated, written to the durable object store (the
    canonical copy other replicas and the next boot's lifespan fetch read —
    WXYC#837), and then hot-swapped into the serving replica's local file: the
    current DB connection is closed (clearing the TTL caches) and the file is
    atomically replaced so the next request picks up the new catalog.

    The storage backend (:class:`~storage.object_store.ObjectStore`) is selected
    once per deployment: a Railway Bucket in prod, a local directory in dev/tests.
    The endpoint is mode-agnostic — it always both ``put``s to the store and does
    the local hot-swap. In local mode the store's directory *is* the DB's parent,
    so the ``put`` and the ``os.replace`` land the same file (idempotent); in
    bucket mode the ``put`` is the remote canonical write and the ``os.replace``
    is this replica's local refresh. The N>=2 caveat (a hot-swap only refreshes
    the replica that served the request; others refresh at next restart) is a
    runbook note, not code (epic #834, PR 4).
    """
    db_path = settings.resolved_library_db_path
    # ``.upload.tmp`` (not ``.tmp``) so this scratch file never collides with
    # ``LocalDirStore.put``'s own internal ``<name>.tmp`` in local mode, where the
    # store's directory is the DB's parent: a shared name would let the store's
    # os.replace rename this file out from under the local hot-swap below (#837).
    # Kept in ``db_path.parent`` so the final os.replace is a same-filesystem
    # atomic rename.
    tmp_path = db_path.parent / f"{db_path.name}.upload.tmp"

    # Write uploaded file to temp location
    try:
        content = await file.read()
        tmp_path.write_bytes(content)
    except Exception as e:
        logger.error(f"Failed to write uploaded file: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to write file: {e}") from e

    # Validate it's a valid SQLite database with a 'library' table
    try:
        conn = sqlite3.connect(str(tmp_path))
        row_count = conn.execute("SELECT count(*) FROM library").fetchone()[0]
        conn.close()
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid SQLite database: {e}",
        ) from e

    # Size guards (LML#1313). Both run before any write, so a rejection leaves the
    # served file and the stored object exactly as they were.
    min_rows = settings.library_db_min_rows
    if min_rows > 0 and row_count < min_rows:
        if force:
            logger.warning(
                "library.db upload carries %d rows, below the %d-row floor, "
                "but force=true; publishing anyway.",
                row_count,
                min_rows,
            )
        else:
            tmp_path.unlink(missing_ok=True)
            logger.warning(
                "Rejected library.db upload: %d rows is below the %d-row floor.",
                row_count,
                min_rows,
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "library.db is below the absolute row floor",
                    "row_count": row_count,
                    "required_min_rows": min_rows,
                    "hint": "re-run the producer against a healthy source, "
                    "lower LIBRARY_DB_MIN_ROWS (0 disables the floor), "
                    "or pass force=true to override this upload",
                },
            )

    prior_rows = _library_row_count(db_path)
    row_regressions = _check_count_regression(
        {LIBRARY_ROW_METRIC: prior_rows},
        {LIBRARY_ROW_METRIC: row_count},
        tolerance=LIBRARY_ROW_DROP_TOLERANCE,
        metrics=(LIBRARY_ROW_METRIC,),
    )
    if row_regressions:
        if force:
            logger.warning(
                "library.db upload regresses the served row count but force=true; "
                "publishing anyway. Regressions: %s",
                row_regressions,
            )
        else:
            tmp_path.unlink(missing_ok=True)
            logger.warning(
                "Rejected library.db upload: row-count regression vs the served copy. %s",
                row_regressions,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "library.db row-count regression",
                    "regressions": row_regressions,
                    "hint": "re-run the producer against a healthy source, "
                    "or pass force=true to override this upload",
                },
            )

    # Compute streaming diff before closing/replacing the old DB
    changes: list[dict] = []
    if settings.streaming_webhook_urls:
        old_ids = _get_streaming_ids(db_path)
        new_ids = _get_streaming_ids(tmp_path)
        changes = _compute_streaming_diff(old_ids, new_ids)

    # Preserve the outgoing object before it is overwritten (LML#1313). Server-side
    # in the store, so the ~16MB blob never transits this process. Attempted rather
    # than gated on exists() so there is no window between the check and the copy;
    # absence just means this is the first upload. A copy fault aborts with 500
    # *before* anything is written — the upload retries on the next producer run,
    # which is cheaper than publishing with no way back.
    try:
        await object_store.copy(LIBRARY_DB_FILENAME, LIBRARY_DB_PREVIOUS_FILENAME)
    except ObjectNotFoundError:
        logger.info(
            "No stored %s to preserve; skipping the %s backup (first upload).",
            LIBRARY_DB_FILENAME,
            LIBRARY_DB_PREVIOUS_FILENAME,
        )
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        logger.error(f"Failed to back up the stored library.db before replacing it: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to back up the current library.db: {e}"
        ) from e

    # Durable canonical write first (WXYC#837). Put-first so a store failure
    # aborts with 500 *before* we mutate the serving replica's on-disk DB — no
    # half-applied swap that would leave this replica ahead of the bucket (and of
    # the next boot / other replicas).
    try:
        await object_store.put(LIBRARY_DB_FILENAME, tmp_path)
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        logger.error(f"Failed to store library.db in the object store: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to store library.db: {e}") from e

    # Close current database connection (clears the TTL caches via LibraryDB.close)
    await close_library_db()

    # Atomic local hot-swap so this replica serves the new file immediately.
    os.replace(str(tmp_path), str(db_path))
    logger.info(f"Library database replaced: {db_path} ({row_count} rows)")

    # Fire streaming webhooks in the background (don't block the upload response)
    if settings.streaming_webhook_urls and changes:
        task = asyncio.create_task(
            _send_streaming_webhooks(
                settings.streaming_webhook_urls,
                settings.etl_notify_key,
                changes,
            )
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    response: dict = {
        "status": "ok",
        "row_count": row_count,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if settings.streaming_webhook_urls and changes:
        response["webhook"] = {"status": "pending", "changes_count": len(changes)}
    return JSONResponse(content=response)


@router.post(
    "/upload-streaming-db",
    summary="Upload a streaming_availability.db backup",
    responses={
        200: {"description": "Upload successful"},
        400: {"description": "Invalid SQLite database (failed the 'albums' probe)"},
        401: {"description": "Missing authorization"},
        403: {"description": "Invalid or missing token"},
        409: {
            "description": "Refused (use force=true): either a coverage regression vs the "
            "stored object, or the stored object is present but unreadable. The JSON `detail` "
            "carries an `error` discriminator; the regression variant also includes a "
            "`regressions` list."
        },
        500: {"description": "Server-side fault writing or reading the file"},
    },
    dependencies=[Depends(require_admin_token)],
)
async def upload_streaming_db(
    file: UploadFile,
    force: bool = False,
    object_store: ObjectStore = Depends(get_object_store),
):
    """Store a streaming_availability.db backup in the object store.

    This is the canonical copy the daily library-sync reads (LML#672), so the
    upload is a full-object replace guarded against coverage regression. It is
    first validated as SQLite with an 'albums' table (400 on failure), then the
    guard runs and rejects the upload with **409** when either:

    * any of {apple_url, spotify_url, deezer_url, albums, track_results} drops
      below prior * (1 - tolerance) or goes non-zero -> zero versus the object
      currently stored -- ``detail`` is ``{error, regressions, hint}``; or
    * the stored object exists but cannot be read, so there is no baseline to
      compare against -- ``detail`` is ``{error, hint}`` (no ``regressions``).

    Branch on ``detail['error']``; ``regressions`` is only present for the first
    case. Pass ``?force=true`` to override either rejection (logged loudly).

    The storage backend (:class:`~storage.object_store.ObjectStore`) is
    selected once per deployment (WXYC#836): a Railway Bucket in prod, a local
    directory in dev/tests. The coverage guard's semantics are identical in both
    modes -- a **missing** object (S3 404 / absent file) reads as the all-zero
    first-upload baseline and is accepted, while a present-but-**unreadable**
    object fails closed at 409. ``put`` is atomic per object (S3 PUT; the local
    store does tmp + ``os.replace``), so no replace dance is needed here.
    """
    with tempfile.TemporaryDirectory(prefix="lml-streaming-upload-") as td:
        tdir = Path(td)
        # The upload scratch file MUST end in ``.tmp`` and the baseline temp below
        # must NOT: test_unreadable_uploaded_tmp_after_validation_is_500 monkeypatches
        # ``_streaming_coverage`` to fault only on ``.tmp`` paths, which is how it
        # exercises a post-validation read fault on the *upload* (classified 500 by
        # the ``except`` around ``_streaming_coverage(upload_tmp)`` below) without
        # also faulting the *baseline* read. Don't rename these two temp files.
        upload_tmp = tdir / "upload.tmp"

        try:
            content = await file.read()
            upload_tmp.write_bytes(content)
            # Free the ~53MB upload buffer before we fetch the baseline object,
            # which materializes another full copy in memory (LML memory pressure).
            del content
        except Exception as e:
            logger.error(f"Failed to write uploaded streaming DB: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to write file: {e}") from e

        try:
            conn = sqlite3.connect(str(upload_tmp))
            row_count = conn.execute("SELECT count(*) FROM albums").fetchone()[0]
            conn.close()
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid SQLite database: {e}",
            ) from e

        # Coverage-regression guard (LML#672): compare against the object currently
        # stored (not an uploader-supplied baseline). A stale-content writer that
        # genuinely lost coverage is rejected on the regression and re-applies on
        # its next cycle. (Eventual rejection across cycles, not a lock across
        # concurrent uploads -- the two writers here, a weekly cron and a rare
        # manual run, are effectively non-concurrent.)
        try:
            baseline_bytes = await object_store.get(STREAMING_DB_FILENAME)
        except ObjectNotFoundError:
            # No stored object -> the first-upload baseline (all-zero coverage);
            # the regression check skips zero baselines, so the upload is accepted.
            old_cov = dict.fromkeys(_STREAMING_COVERAGE_METRICS, 0)
        else:
            baseline_tmp = tdir / "baseline.db"
            baseline_tmp.write_bytes(baseline_bytes)
            try:
                old_cov = _streaming_coverage(baseline_tmp)
            except StreamingCoverageUnreadableError:
                # The stored object is present but unreadable, so there is no
                # baseline to compare against. Fail closed rather than fail open:
                # treating it as a first upload would accept any thin replacement.
                # force=true overrides.
                if force:
                    logger.warning(
                        "Stored streaming DB unreadable but force=true; "
                        "replacing without a coverage baseline."
                    )
                    old_cov = dict.fromkeys(_STREAMING_COVERAGE_METRICS, 0)
                else:
                    logger.warning("Rejected streaming upload: stored streaming DB unreadable.")
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "existing streaming DB unreadable; refusing to replace blind",
                            "hint": "retry once the stored object is readable, "
                            "or pass force=true to override",
                        },
                    ) from None

        try:
            new_cov = _streaming_coverage(upload_tmp)
        except StreamingCoverageUnreadableError as e:
            # The upload already passed the albums-count validation above, so this
            # is a server-side read fault on a file we just wrote and validated, not
            # a bad client upload -- surface it as 500 so a retry-on-5xx caller
            # retries.
            raise HTTPException(
                status_code=500,
                detail=f"Uploaded streaming DB became unreadable after validation: {e}",
            ) from e

        regressions = _check_count_regression(old_cov, new_cov)
        if regressions:
            if force:
                logger.warning(
                    "Streaming upload regresses coverage but force=true; replacing anyway. "
                    "Regressions: %s",
                    regressions,
                )
            else:
                logger.warning(
                    "Rejected streaming upload: coverage regression vs stored object. %s",
                    regressions,
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "streaming coverage regression",
                        "regressions": regressions,
                        "hint": "re-run as a round-trip (download -> modify -> upload), "
                        "or pass force=true to override",
                    },
                )

        await object_store.put(STREAMING_DB_FILENAME, upload_tmp)

    logger.info("Streaming database backed up to the object store (%d albums)", row_count)

    return JSONResponse(
        content={
            "status": "ok",
            "row_count": row_count,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )


@router.get(
    "/download-streaming-db",
    summary="Download the current streaming_availability.db backup",
    responses={
        200: {
            "description": "Streaming database file",
            "content": {"application/octet-stream": {}},
        },
        401: {"description": "Missing authorization"},
        403: {"description": "Invalid or missing token"},
        404: {"description": "streaming_availability.db not present in the store"},
    },
    dependencies=[Depends(require_admin_token)],
)
async def download_streaming_db(
    object_store: ObjectStore = Depends(get_object_store),
):
    """Stream the current streaming_availability.db from the object store.

    Symmetric with `POST /admin/upload-streaming-db`. Lets the daily
    library-sync pipeline (WXYC/discogs-etl) read the file directly from the
    store instead of round-tripping it through a GitHub Release. The whole
    object (~53MB) is buffered and returned in one response; that egress is
    negligible at the daily sync cadence.
    """
    try:
        data = await object_store.get(STREAMING_DB_FILENAME)
    except ObjectNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"{STREAMING_DB_FILENAME} not found in the store",
        ) from None

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"content-disposition": f'attachment; filename="{STREAMING_DB_FILENAME}"'},
    )


# ---------------------------------------------------------------------------
# Tombstone recovery (LML#510)
# ---------------------------------------------------------------------------
#
# When Discogs returns 404 for a release / artist id, the cache writes a
# tombstone row (`not_found = TRUE`) so subsequent reads short-circuit
# instead of re-burning the rate-limit budget on the same 404. False
# tombstones happen — most plausibly during a Discogs incident that returns
# 404s for valid ids. The endpoint below lets on-call clear them.
#
# The `WHERE id = $1 AND not_found = TRUE` guard at the cache-service layer
# means a real row can never be deleted through this surface, even with a
# typo'd id. Auth is via ADMIN_TOKEN — distinct from LML_API_KEY so
# routine LML callers don't gain incident-grade write access.

TombstoneEntityType = Literal["release", "artist"]


@router.delete(
    "/discogs/tombstone/{entity_type}/{entity_id}",
    summary="Clear an LML#510 tombstone so the next call re-fetches from Discogs",
    responses={
        200: {"description": "Tombstoned row deleted; L1 cache entry evicted"},
        401: {"description": "Missing authorization"},
        403: {"description": "Invalid or missing token"},
        404: {"description": "Row not found, or row exists but is not a tombstone"},
        503: {"description": "Discogs cache pool not configured"},
    },
    dependencies=[Depends(require_admin_token)],
)
async def delete_tombstone(
    entity_type: TombstoneEntityType = PathParam(..., description="`release` or `artist`"),
    entity_id: int = PathParam(..., description="Discogs id"),
    cache_service: DiscogsCacheService | None = Depends(get_discogs_cache_service_from_pool),
):
    """Delete a tombstoned row + evict the L1 entry.

    Three response codes so a recovery script can branch without parsing
    log volume:

    * `200 {deleted: true, id, type}` — a tombstoned row matched and was
      deleted; the next request will re-fetch from Discogs.
    * `404 {detail: "row exists but is not a tombstone", id, type}` — the
      id exists with `not_found = FALSE`; either the operator typo'd or
      the tombstone was already cleared.
    * `404 {detail: "no row for this id", id, type}` — id doesn't exist.

    `?refresh=true` (LML#498) does NOT cover this: refresh evicts L1
    only, and a tombstone lives in L2 (PG); we read it back and re-serve
    it. This endpoint is the L2 surface.
    """
    if cache_service is None:
        raise HTTPException(
            status_code=503,
            detail="Discogs cache pool not configured (DATABASE_URL_DISCOGS unset?)",
        )

    try:
        outcome = await cache_service.delete_tombstone(entity_type, entity_id)
    except CacheUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    if outcome == "not_found":
        raise HTTPException(
            status_code=404,
            detail={
                "detail": "no row for this id",
                "id": entity_id,
                "type": entity_type,
            },
        )
    if outcome == "exists_not_tombstone":
        raise HTTPException(
            status_code=404,
            detail={
                "detail": "row exists but is not a tombstone",
                "id": entity_id,
                "type": entity_type,
            },
        )

    # outcome == "deleted": L2 row gone, now drop the L1 entry so a
    # subsequent request actually re-traverses L2/L3 instead of returning
    # the cached None from before the tombstone was cleared.
    cached_func = (
        DiscogsService.get_release
        if entity_type == "release"
        else DiscogsService.get_artist_details
    )
    try:
        evict_cached(cached_func, entity_id)
    except Exception as e:
        # Don't fail the response — the L2 delete already succeeded, and
        # L1's TTL will expire naturally. Log so an operator notices if
        # the decorator surface ever drifts.
        logger.warning("L1 evict failed after tombstone delete: %s", e)

    return JSONResponse(content={"deleted": True, "id": entity_id, "type": entity_type})
