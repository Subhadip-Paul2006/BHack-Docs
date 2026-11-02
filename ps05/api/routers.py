"""API routers: health + minimal cases CRUD wired to the ORM models."""

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ps05.api.health import router as health_router
from ps05.db.models import CaseInvestigation
from ps05.db.session import get_session


class CaseCreate(BaseModel):
    title: str
    jurisdiction: str | None = None
    created_by: str | None = None


class CaseOut(BaseModel):
    case_id: uuid.UUID
    title: str
    status: str
    created_at: str

    model_config = {"protected_namespaces": ()}


def _to_out(case: CaseInvestigation) -> CaseOut:
    return CaseOut(
        case_id=case.case_id,
        title=case.title,
        status=case.status,
        created_at=case.created_at.isoformat() if case.created_at else "",
    )


def create_cases_router() -> APIRouter:
    """Build a fresh cases router (one instance per app so routes attach once)."""
    router = APIRouter(prefix="/cases", tags=["cases"])

    @router.get("", response_model=list[CaseOut])
    async def list_cases() -> list[CaseOut]:
        async with get_session() as session:
            result = await session.execute(
                select(CaseInvestigation).order_by(CaseInvestigation.created_at.desc())
            )
            cases = result.scalars().all()
        return [_to_out(c) for c in cases]

    @router.post("", status_code=201, response_model=CaseOut)
    async def create_case(payload: CaseCreate) -> CaseOut:
        """Create a new case; returns ``case_id``. One unit of work per call."""
        case = CaseInvestigation(
            case_id=uuid.uuid4(),
            title=payload.title,
            jurisdiction=payload.jurisdiction,
            created_by=payload.created_by,
            status="CREATED",
        )
        async with get_session() as session:
            session.add(case)
            await session.commit()
            await session.refresh(case)
        return _to_out(case)

    @router.get("/{case_id}", response_model=CaseOut)
    async def get_case(case_id: uuid.UUID) -> CaseOut:
        async with get_session() as session:
            result = await session.execute(
                select(CaseInvestigation).where(CaseInvestigation.case_id == case_id)
            )
            case = result.scalar_one_or_none()
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        return _to_out(case)

    return router


# Module-level convenience router (default settings) used by create_app.
cases_router = create_cases_router()


def all_routers() -> list[APIRouter]:
    """Routers attached to the app, in order."""
    return [health_router, cases_router]
