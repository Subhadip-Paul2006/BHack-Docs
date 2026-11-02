"""Async engine + session management.

One session per unit of work: ``get_session()`` opens, yields and closes a
single ``AsyncSession``. Sessions are never shared across tasks or requests.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from ps05.settings import get_settings

# Lazily-created production engine. Tests may replace it via
# ``configure_engine`` so fixtures control the engine lifetime.
_engine: Optional[AsyncEngine] = None


def configure_engine(engine: AsyncEngine) -> None:
    """Point the module at a caller-provided engine (test hook)."""
    global _engine
    _engine = engine


def get_engine() -> AsyncEngine:
    """Return the async engine.

    Built from ``Settings.effective_database_url``; when no ``DATABASE_URL``
    is configured it defaults to in-memory SQLite (``sqlite+aiosqlite``).
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.effective_database_url,
            echo=settings.database_echo,
            pool_pre_ping=True,
        )
    return _engine


async def dispose_engine() -> None:
    """Dispose of the current engine, if any (used at app shutdown/tests)."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a fresh ``AsyncSession``; one session per unit of work."""
    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
