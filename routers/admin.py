"""Admin endpoints for service management."""

import logging
import os
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from config.settings import Settings, get_settings
from core.dependencies import close_library_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


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

    # Close current database connection
    await close_library_db()

    # Atomic replace
    os.replace(str(tmp_path), str(db_path))
    logger.info(f"Library database replaced: {db_path} ({row_count} rows)")

    return JSONResponse(
        content={
            "status": "ok",
            "row_count": row_count,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
