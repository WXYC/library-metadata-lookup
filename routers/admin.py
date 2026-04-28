"""Admin endpoints for service management."""

import asyncio
import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from config.settings import Settings, get_settings
from core.dependencies import close_library_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


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
    return list(await asyncio.gather(*tasks))


def _validate_auth(
    settings: Settings,
    authorization: str | None,
) -> None:
    """Validate bearer token against ADMIN_TOKEN setting."""
    if not settings.admin_token:
        raise HTTPException(status_code=403, detail="Admin endpoint disabled (no ADMIN_TOKEN set)")

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != settings.admin_token:
        raise HTTPException(status_code=403, detail="Invalid token")


@router.post(
    "/upload-library-db",
    summary="Upload a new library.db file",
    responses={
        200: {"description": "Upload successful"},
        400: {"description": "Invalid SQLite database"},
        401: {"description": "Missing authorization"},
        403: {"description": "Invalid or missing token"},
    },
)
async def upload_library_db(
    file: UploadFile,
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(None),
):
    """Replace the library.db file with an uploaded SQLite database.

    The uploaded file is validated before replacing the existing database.
    The current database connection is closed so the next request picks up
    the new file.
    """
    _validate_auth(settings, authorization)

    db_path = settings.resolved_library_db_path
    tmp_path = db_path.parent / f"{db_path.name}.tmp"

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

    # Compute streaming diff before closing/replacing the old DB
    changes: list[dict] = []
    if settings.streaming_webhook_urls:
        old_ids = _get_streaming_ids(db_path)
        new_ids = _get_streaming_ids(tmp_path)
        changes = _compute_streaming_diff(old_ids, new_ids)

    # Close current database connection
    await close_library_db()

    # Atomic replace
    os.replace(str(tmp_path), str(db_path))
    logger.info(f"Library database replaced: {db_path} ({row_count} rows)")

    # Send streaming webhook after successful swap
    webhook_result = None
    if settings.streaming_webhook_urls and changes:
        webhook_result = await _send_streaming_webhooks(
            settings.streaming_webhook_urls,
            settings.etl_notify_key,
            changes,
        )
        logger.info(
            "Streaming webhook sent: %d changes to %d URLs",
            len(changes),
            len(webhook_result),
        )

    response: dict = {
        "status": "ok",
        "row_count": row_count,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if webhook_result is not None:
        response["webhook"] = webhook_result
    return JSONResponse(content=response)


@router.post(
    "/upload-streaming-db",
    summary="Upload a streaming_availability.db backup",
    responses={
        200: {"description": "Upload successful"},
        400: {"description": "Invalid SQLite database"},
        401: {"description": "Missing authorization"},
        403: {"description": "Invalid or missing token"},
    },
)
async def upload_streaming_db(
    file: UploadFile,
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(None),
):
    """Store a streaming_availability.db backup on the Railway volume.

    This file is not used at runtime — it's a backup of the analysis database
    that contains all streaming search results, track-level data, and Discogs
    match state. Validated as SQLite with an 'albums' table before writing.
    """
    _validate_auth(settings, authorization)

    db_dir = settings.resolved_library_db_path.parent
    db_path = db_dir / "streaming_availability.db"
    tmp_path = db_dir / "streaming_availability.db.tmp"

    try:
        content = await file.read()
        tmp_path.write_bytes(content)
    except Exception as e:
        logger.error(f"Failed to write uploaded streaming DB: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to write file: {e}") from e

    try:
        conn = sqlite3.connect(str(tmp_path))
        row_count = conn.execute("SELECT count(*) FROM albums").fetchone()[0]
        conn.close()
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid SQLite database: {e}",
        ) from e

    os.replace(str(tmp_path), str(db_path))
    logger.info(f"Streaming database backed up: {db_path} ({row_count} albums)")

    return JSONResponse(
        content={
            "status": "ok",
            "row_count": row_count,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
