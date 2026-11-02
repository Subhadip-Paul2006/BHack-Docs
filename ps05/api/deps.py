"""FastAPI dependency providers (settings, async session, celery)."""

from typing import Annotated, AsyncIterator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ps05.db.session import get_session
from ps05.settings import Settings, get_settings
from ps05.workers.celery_app import celery


def settings_dep() -> Settings:
    """Process-wide cached settings."""
    return get_settings()


async def session_dep() -> AsyncIterator[AsyncSession]:
    """Yield one session per request (unit of work); closed before response."""
    async with get_session() as session:
        yield session


def celery_dep() -> object:
    """Configured Celery instance (used for task publishing)."""
    return celery


# Convenience Annotated aliases for endpoint signatures.
SettingsDep = Annotated[Settings, Depends(settings_dep)]
SessionDep = Annotated[AsyncSession, Depends(session_dep)]
CeleryDep = Annotated[object, Depends(celery_dep)]
