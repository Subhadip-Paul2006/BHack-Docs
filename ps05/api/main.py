"""FastAPI application factory.

Run with::

    uvicorn ps05.api.main:app --host 127.0.0.1 --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from ps05 import __version__
from ps05.api.routers import all_routers
from ps05.db.session import dispose_engine
from ps05.logging_setup import configure_logging
from ps05.settings import get_settings


def create_app() -> FastAPI:
    """Build and configure the FastAPI app (idempotent factory)."""
    settings = get_settings()
    configure_logging(settings.env, settings.debug)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await dispose_engine()

    application = FastAPI(
        title="PS-05 Digital Evidence Authenticity & Integrity Verification Platform",
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
    )

    for router in all_routers():
        application.include_router(router)

    @application.get("/", include_in_schema=False)
    async def root() -> dict:
        return {"name": application.title, "version": application.version}

    return application


app = create_app()
