"""Declarative base for PS-05 ORM models (SQLAlchemy 2.0 style)."""

from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import MetaData


class Base(DeclarativeBase):
    """Shared ``DeclarativeBase``; all platform tables register on this."""

    # Shared metadata so Alembic autogenerate / ``create_all`` see every model.
    metadata = MetaData()
