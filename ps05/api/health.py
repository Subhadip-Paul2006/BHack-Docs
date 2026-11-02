"""Health & readiness endpoints."""

from fastapi import APIRouter
from sqlalchemy import text

from ps05.db.session import get_engine
from ps05.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness: the API process is up. Never fails on downstream systems."""
    settings = get_settings()
    return {"status": "ok", "env": settings.env}


@router.get("/readyz")
async def readyz() -> dict:
    """Readiness: API is up and its database is reachable."""
    settings = get_settings()
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        db_ok = False
    return {
        "status": "ready" if db_ok else "not_ready",
        "env": settings.env,
        "database": "ok" if db_ok else "unreachable",
        "storage_backend": settings.storage_backend,
    }
