"""Task record (TRD § 23.4.4 ``platform_task_records``).

One row per durable task node in an analysis run. Every field is the
canonical schema; the record is what the workflow engine materialises
before a task is admitted to a queue.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from ps05.core.idempotency import DigestLike, build_idempotency_key


class TaskRecord(BaseModel):
    """Mirrors the ``platform_task_records`` columns (§ 23.4.4)."""

    task_id: UUID = Field(default_factory=uuid4)
    analysis_run_id: UUID
    case_id: UUID
    evidence_id: Optional[UUID] = None  # NULL only for case-level tasks
    parent_task_id: Optional[UUID] = None
    engine_id: str
    stage_id: str
    input_artifact_ids: List[UUID] = Field(default_factory=list)
    input_manifest_hashes: Dict[str, str] = Field(default_factory=dict)  # per-input SHA-256
    idempotency_key: str
    attempt_number: int = 1
    max_attempts: int
    task_deadline: datetime
    worker_id: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    checkpoint_id: Optional[UUID] = None
    output_artifact_ids: List[UUID] = Field(default_factory=list)
    model_version: Optional[str] = None
    configuration_version: str
    status: str = "PENDING"  # TaskState value (§ 23.4.3)
    error_class: Optional[str] = None  # § 23.10 taxonomy code
    error_code: Optional[str] = None


def new_task(
    analysis_run_id: UUID,
    case_id: UUID,
    engine_id: str,
    stage_id: str,
    max_attempts: int,
    task_deadline: datetime,
    configuration_version: str,
    *,
    evidence_id: Optional[UUID] = None,
    parent_task_id: Optional[UUID] = None,
    input_artifact_ids: Optional[List[UUID]] = None,
    input_manifest_hashes: Optional[Dict[str, str]] = None,
    model_version: Optional[str] = None,
    model_checkpoint_version: Optional[str] = None,
    run_generation: int = 0,
    task_id: Optional[UUID] = None,
) -> TaskRecord:
    """Create a fresh ``TaskRecord`` with its § 23.5 idempotency key.

    The key is built from the input-artifact digests (``input_manifest_hashes``)
    so that a re-run under a new generation or a changed model/config yields
    a *distinct* record (REO-004).
    """
    input_artifact_ids = input_artifact_ids or []
    input_manifest_hashes = input_manifest_hashes or {}
    digest: DigestLike = input_manifest_hashes if input_manifest_hashes else [
        str(aid) for aid in input_artifact_ids
    ]
    key = build_idempotency_key(
        case_id=str(case_id),
        evidence_id=str(evidence_id) if evidence_id else None,
        engine_id=engine_id,
        stage_id=stage_id,
        input_artifact_digest=digest,
        config_version=configuration_version,
        model_checkpoint_version=model_checkpoint_version or model_version,
        run_generation=run_generation,
    )
    return TaskRecord(
        task_id=task_id or uuid4(),
        analysis_run_id=analysis_run_id,
        case_id=case_id,
        evidence_id=evidence_id,
        parent_task_id=parent_task_id,
        engine_id=engine_id,
        stage_id=stage_id,
        input_artifact_ids=input_artifact_ids,
        input_manifest_hashes=input_manifest_hashes,
        idempotency_key=key,
        attempt_number=1,
        max_attempts=max_attempts,
        task_deadline=task_deadline,
        model_version=model_version,
        configuration_version=configuration_version,
        status="PENDING",
    )
