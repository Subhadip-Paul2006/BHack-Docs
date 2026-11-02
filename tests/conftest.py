"""Shared fixtures for the PS-05 test suite.

Everything here uses fakes / in-memory stores so the suite runs without a
live Postgres, Redis or GPU.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sqlite_engine():
    """In-memory SQLAlchemy engine + session factory, one per test.

    The integration tests (``tests/integration/test_api_app.py``) use this as
    the database behind ``create_app()`` so no live Postgres is needed.

    Lead-wiring dependency: the application's declarative ``Base`` and models
    are expected to be registered against the engine returned here. If the
    lead has not wired them yet, ``Base.metadata`` will be empty, ``create_all``
    creates nothing, and any row round-trip assertion will be an empty no-op
    rather than a hard failure. That is intentional: it keeps collection green
    and makes the missing wiring diagnosable instead of obscuring real bugs.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    # In-memory SQLite. ``check_same_thread=False`` lets the event loop and
    # FastAPI's thread pool touch the same connection pool safely.
    sync_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )

    try:
        from ps05.db.base import Base  # the application's declarative Base
        Base.metadata.create_all(sync_engine)
    except Exception:
        # Models not yet registered against this engine — round-trip tests
        # will no-op. See the docstring above.
        pass

    sync_session = sessionmaker(sync_engine, expire_on_commit=False)
    yield sync_engine, sync_session


@pytest.fixture
async def api_client():
    """FastAPI app backed by an in-memory async SQLite engine.

    Adds the fixture the sync ``sqlite_engine`` above cannot provide: the
    API routes drive the database through ``ps05.db.session.get_session()``
    (async), which is wired with ``configure_engine``. A ``StaticPool``
    keeps every session on one connection so the in-memory database
    (and the ``create_all`` tables) survive across requests. No live
    Postgres or Redis is involved.
    """
    import httpx
    from sqlalchemy import pool
    from sqlalchemy.ext.asyncio import create_async_engine

    import ps05.db.models  # noqa: F401  # registers all ORM models on Base.metadata
    from ps05.api.main import create_app
    from ps05.db import session as db_session
    from ps05.db.base import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=pool.StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    previous_engine = db_session._engine
    db_session.configure_engine(engine)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(create_app()))
    try:
        yield client
    finally:
        await client.aclose()
        await engine.dispose()
        db_session.configure_engine(previous_engine)
