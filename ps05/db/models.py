"""Canonical ORM models for the PS-05 platform (TRD § 23.11 table set).

All 22 platform tables are defined here on the shared ``Base`` metadata.
Status columns are declared as plain ``sa.String`` with the expected value
set documented in the column comment; the lead wires enum types later.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from ps05.db.base import Base

# PostgreSQL JSONB, falling back to JSON for non-Postgres engines (tests).
_JSON = postgresql.JSONB().with_variant(JSON(), "sqlite")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _TimestampsMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


# ----------------------------------------------------------------------------
# Investigation & evidence plane
# ----------------------------------------------------------------------------


class CaseInvestigation(_TimestampsMixin, Base):
    """Root case / investigation record (lifecycle § 23.4.1)."""

    __tablename__ = "case_investigations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True), unique=True, default=uuid.uuid4, nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # CREATED | INGESTING | INTEGRITY_VERIFICATION | WORM_COMMITTED |
    # EVIDENCE_REGISTERED | TRIAGE_PENDING | ANALYSIS_PLANNED | PROCESSING |
    # PARTIAL_SUCCESS | ANALYSIS_COMPLETED | REVIEW_PENDING | REVIEW_COMPLETED |
    # REPORT_PENDING | REPORTED | CANCEL_REQUESTED | CANCELLED | FAILED
    status: Mapped[str] = mapped_column(
        String(64), nullable=False, default="CREATED", comment="lifecycle state § 23.4.1"
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(128))
    jurisdiction: Mapped[Optional[str]] = mapped_column(
        String(64), comment="legal jurisdiction code, e.g. INDIA_BSA_2023"
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class EvidenceObject(_TimestampsMixin, Base):
    __tablename__ = "evidence_objects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("case_investigations.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    exhibit_label: Mapped[str] = mapped_column(String(128), nullable=False)
    original_filename: Mapped[Optional[str]] = mapped_column(String(500))
    mime_type: Mapped[Optional[str]] = mapped_column(String(200))
    file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Pre-decode dual hashing (§ 19.2): both digests are recorded before any WORM commit.
    sha256: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    sha3_512: Mapped[Optional[str]] = mapped_column(String(128))
    storage_vault_urn: Mapped[Optional[str]] = mapped_column(
        String(512), comment="urn:ceipl:vault:... resolved after WORM commit"
    )
    # CREATED | UPLOADING | HASH_VERIFIED | WORM_COMMITTED | REGISTERED |
    # QUARANTINED
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="CREATED")


class AcquisitionRecord(Base):
    __tablename__ = "acquisition_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("evidence_objects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    acquired_by: Mapped[Optional[str]] = mapped_column(String(128))
    acquisition_method: Mapped[Optional[str]] = mapped_column(String(128))
    source_device: Mapped[Optional[str]] = mapped_column(String(255))
    notes: Mapped[Optional[str]] = mapped_column(Text)


class EvidenceManifest(Base):
    __tablename__ = "evidence_manifests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("evidence_objects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    manifest_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="e.g. PRE_DECODE_DUAL_HASH, WORM_COMMIT, PARSER_FIELD_PROVENANCE",
    )
    manifest_sha256: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    content: Mapped[Any] = mapped_column(_JSON, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class DerivedArtifact(_TimestampsMixin, Base):
    __tablename__ = "derived_artifacts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("evidence_objects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True), ForeignKey("analysis_runs.id"), index=True
    )
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("platform_task_records.task_id"),
        index=True,
    )
    engine_id: Mapped[Optional[str]] = mapped_column(String(128))
    artifact_type: Mapped[Optional[str]] = mapped_column(String(128))
    storage_urn: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    # UNIQUE per § 23.5: duplicate delivery of a completed task is a no-op.
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255), unique=True)
    generation_note: Mapped[Optional[str]] = mapped_column(
        String(255), comment="free-form generation note"
    )


class ArtifactProvenanceDag(Base):
    """Lineage edge: child artifact derived from parent artifact (§ 4.3, § 11.8.2)."""

    __tablename__ = "artifact_provenance_dag"
    __table_args__ = (UniqueConstraint("child_artifact_id", "parent_artifact_id", name="uq_provenance_edge"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    child_artifact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("derived_artifacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_artifact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("derived_artifacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    edge_type: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class AnalysisRun(_TimestampsMixin, Base):
    """One row per § 23.5 analysis generation (immutable correlation)."""

    __tablename__ = "analysis_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("case_investigations.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("evidence_objects.id", ondelete="SET NULL"),
        index=True,
    )
    # "GEN-0" first run, "GEN-1" re-run ... — explicit re-run identifier.
    generation: Mapped[str] = mapped_column(String(32), nullable=False, default="GEN-0")
    configuration_version: Mapped[Optional[str]] = mapped_column(String(128))
    # CREATED | PLANNED | RUNNING | COMPLETED | PARTIAL_SUCCESS | FAILED | CANCELLED
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="CREATED")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ----------------------------------------------------------------------------
# Orchestration plane
# ----------------------------------------------------------------------------


class PlatformWorkflowDefinition(_TimestampsMixin, Base):
    __tablename__ = "platform_workflow_definitions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_workflow_name_version"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    spec: Mapped[Any] = mapped_column(_JSON, nullable=False, comment="versioned DAG spec § 23.3")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ACTIVE", comment="ACTIVE | DEPRECATED | RETIRED"
    )


class PlatformWorkflowNode(Base):
    __tablename__ = "platform_workflow_nodes"
    __table_args__ = (UniqueConstraint("workflow_id", "node_id", name="uq_workflow_node"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("platform_workflow_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[str] = mapped_column(String(255), nullable=False)
    engine_id: Mapped[str] = mapped_column(String(128), nullable=False)
    stage_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Queue / workload class (§ 23.9).
    queue_class: Mapped[Optional[str]] = mapped_column(String(128))
    # Declared retry policy (§ 23.10): max_attempts, max_elapsed_retry_duration, backoff.
    retry_policy: Mapped[Optional[Any]] = mapped_column(_JSON)
    # Declared resource profile reference (§ 23.9, RES-002).
    resource_profile_ref: Mapped[Optional[str]] = mapped_column(String(255))
    # HARD prerequisites block on SUCCEEDED; SOFT may record a gap.
    depends_on: Mapped[Optional[Any]] = mapped_column(
        _JSON, comment="list of node_ids; requirement kind per edge: HARD | SOFT"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class PlatformTaskRecord(_TimestampsMixin, Base):
    """Canonical task record (§ 23.4.4). One row per durable task node."""

    __tablename__ = "platform_task_records"
    __table_args__ = (
        # § 23.5: duplicate delivery rejected by UNIQUE(idempotency_key, attempt_number).
        UniqueConstraint("idempotency_key", "attempt_number", name="uq_task_idempotency_attempt"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("analysis_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("case_investigations.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("evidence_objects.id", ondelete="SET NULL"),
        index=True,
    )
    parent_task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("platform_task_records.task_id", ondelete="SET NULL"),
        index=True,
    )
    engine_id: Mapped[str] = mapped_column(String(128), nullable=False)
    stage_id: Mapped[str] = mapped_column(String(255), nullable=False)
    input_artifact_ids: Mapped[Optional[Any]] = mapped_column(
        _JSON, comment="list of UUIDs of consumed artifacts"
    )
    input_manifest_hashes: Mapped[Optional[Any]] = mapped_column(
        _JSON, comment="per-input SHA-256 digests (CEIPL canonical form)"
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    task_deadline: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="hard wall-clock deadline for this attempt"
    )
    worker_id: Mapped[Optional[str]] = mapped_column(String(128), comment="NULL until claimed")
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    checkpoint_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("platform_checkpoints.id", ondelete="SET NULL"),
        comment="last completed durable checkpoint (§ 23.7)",
    )
    output_artifact_ids: Mapped[Optional[Any]] = mapped_column(
        _JSON, comment="list of UUIDs produced, only after SUCCEEDED"
    )
    model_version: Mapped[Optional[str]] = mapped_column(
        String(255), comment="e.g. VideoMAE-v1.2-sha256:<hex>; NULL for non-ML stages"
    )
    configuration_version: Mapped[Optional[str]] = mapped_column(String(128))
    # PENDING | READY | CLAIMED | RUNNING | CHECKPOINTED | SUCCEEDED |
    # FAILED_RETRYABLE | RETRY_WAIT | FAILED_PERMANENT | DEAD_LETTERED |
    # BLOCKED | CANCELLED | SKIPPED_OPTIONAL  (§ 23.4.3)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    error_class: Mapped[Optional[str]] = mapped_column(
        String(64), comment="§ 23.10 taxonomy code"
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(128), comment="machine detail code")
    # Optimistic concurrency guard, bumped on every committed transition.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class PlatformTaskStateEvent(Base):
    """Append-only task lifecycle audit trail (§ 23.4.3)."""

    __tablename__ = "platform_task_state_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("platform_task_records.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_status: Mapped[Optional[str]] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    worker_id: Mapped[Optional[str]] = mapped_column(String(128))
    event_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class PlatformOutbox(Base):
    """Transactional outbox (§ 23.6.1): business write + event in one TX."""

    __tablename__ = "platform_outbox"

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True), primary_key=True, default=uuid.uuid4
    )
    aggregate_type: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="e.g. case_investigation, platform_task_records"
    )
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True, native_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, comment="EV_* domain event")
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True), index=True
    )
    case_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True, native_uuid=True))
    payload: Mapped[Any] = mapped_column(_JSON, nullable=False)
    # Monotonic per (aggregate_type, aggregate_id).
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # PENDING | PUBLISHED | FAILED
    dispatch_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING"
    )

    __table_args__ = (
        UniqueConstraint(
            "aggregate_type", "aggregate_id", "sequence_no", name="uq_outbox_sequence"
        ),
    )


class PlatformInbox(Base):
    """Processed-message registry / dedupe store (§ 23.6.2)."""

    __tablename__ = "platform_inbox"

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True), primary_key=True
    )
    consumer_context: Mapped[str] = mapped_column(String(128), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("consumer_context", "dedupe_key", name="uq_inbox_dedupe"),
    )


class PlatformTaskLease(Base):
    """Live lease row, written in the same TX as the task's CLAIMED transition."""

    __tablename__ = "platform_task_lease"

    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("platform_task_records.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class PlatformTaskLeaseEvent(Base):
    """Recovery audit trail (§ 23.6.4): every lease expiry -> reassignment."""

    __tablename__ = "platform_task_lease_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("platform_task_records.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # ACQUIRED | HEARTBEAT | EXPIRED | REASSIGNED | RELEASED
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    prior_worker_id: Mapped[Optional[str]] = mapped_column(String(128))
    new_worker_id: Mapped[Optional[str]] = mapped_column(String(128))
    prior_checkpoint_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True)
    )
    reaped_by: Mapped[Optional[str]] = mapped_column(
        String(128), comment="actor that reaped, e.g. reaper-<worker>"
    )
    event_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class PlatformEngineExecutionProfile(_TimestampsMixin, Base):
    """Versioned resource profiles (§ 23.9, RES-002)."""

    __tablename__ = "platform_engine_execution_profiles"
    __table_args__ = (
        UniqueConstraint("engine_id", "profile_version", name="uq_engine_profile_version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    engine_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    workload_class: Mapped[Optional[str]] = mapped_column(String(128))
    max_memory_mb: Mapped[Optional[int]] = mapped_column(Integer)
    max_batch_size: Mapped[Optional[int]] = mapped_column(Integer)
    gpu_class: Mapped[Optional[str]] = mapped_column(String(64))
    task_deadline_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    # Free-form resource budget payload (RES-001).
    resources: Mapped[Optional[Any]] = mapped_column(_JSON)
    # ACTIVE | DEPRECATED
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")


# ----------------------------------------------------------------------------
# Results & output plane
# ----------------------------------------------------------------------------


class Finding(_TimestampsMixin, Base):
    """Cross-engine forensic finding (extends § 4.1 FORENSIC_FINDINGS)."""

    __tablename__ = "findings"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_findings_idempotency"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("case_investigations.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("evidence_objects.id", ondelete="SET NULL"),
        index=True,
    )
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("analysis_runs.id", ondelete="SET NULL"),
        index=True,
    )
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("platform_task_records.task_id", ondelete="SET NULL"),
        index=True,
    )
    engine_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # e.g. MANIPULATION_INDICATOR, CONTRADICTION, FRAME_GAP_OBSERVED.
    finding_type: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INFORMATIONAL",
        comment="INFORMATIONAL | LOW | MEDIUM | HIGH | CRITICAL",
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[Optional[Any]] = mapped_column(_JSON, comment="structured evidence payload")
    model_version: Mapped[Optional[str]] = mapped_column(String(255))
    configuration_version: Mapped[Optional[str]] = mapped_column(String(128))
    input_artifact_digest: Mapped[Optional[str]] = mapped_column(String(128))
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255))
    # OPEN | CORROBORATED | REJECTED | ADJUDICATED
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="OPEN")


class ModelExecutionProvenance(Base):
    """REO-003: every ML stage records model/checkpoint + input binding."""

    __tablename__ = "model_execution_provenance"

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("platform_task_records.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="versioned model/checkpoint ID, e.g. sha256-bound"
    )
    checkpoint_hash: Mapped[Optional[str]] = mapped_column(String(128))
    framework: Mapped[Optional[str]] = mapped_column(String(64))
    device: Mapped[Optional[str]] = mapped_column(
        String(32), comment="cuda | cpu (per PS05_DEVICE / fallback)"
    )
    input_batch_hash: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class PlatformCheckpoint(Base):
    """Durable stage-level checkpoint (§ 23.7)."""

    __tablename__ = "platform_checkpoints"
    __table_args__ = (UniqueConstraint("task_id", "checkpoint_key", name="uq_checkpoint_key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("platform_task_records.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # cp_upload_chunk | cp_predecode_hash | cp_worm_commit | ... (§ 23.7 table)
    checkpoint_key: Mapped[str] = mapped_column(String(128), nullable=False)
    stage_id: Mapped[Optional[str]] = mapped_column(String(255))
    payload: Mapped[Optional[Any]] = mapped_column(_JSON, comment="persisted chunk outputs")
    # Complete only when output is persisted + verified + lineage-linked.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    persisted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class QuarantineOutput(Base):
    """Quarantined outputs / reconciliation states (§ 22.12, § 23.10, STG-002)."""

    __tablename__ = "quarantine_outputs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("case_investigations.case_id", ondelete="SET NULL"),
        index=True,
    )
    evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("evidence_objects.id", ondelete="SET NULL"),
        index=True,
    )
    # HASH_MISMATCH | ORPHAN_OBJECT | PHANTOM_RECORD | DEAD_LETTERED_OUTPUT | ...
    quarantine_class: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    original_storage_urn: Mapped[Optional[str]] = mapped_column(String(512))
    payload: Mapped[Optional[Any]] = mapped_column(_JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class IndexingJob(_TimestampsMixin, Base):
    """RAG / search indexing jobs (§ 23.13); search is a projection, not record."""

    __tablename__ = "indexing_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("case_investigations.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    index_type: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="e.g. CASE_RAG, FINDING_FULLTEXT"
    )
    source_ref: Mapped[Optional[str]] = mapped_column(String(512))
    progress: Mapped[Optional[Any]] = mapped_column(_JSON)
    # PENDING | RUNNING | COMPLETED | FAILED | STALE
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ReportSnapshot(_TimestampsMixin, Base):
    """Sealed forensic report snapshots (extends § 21.9 ufredf_reports)."""

    __tablename__ = "report_snapshots"
    __table_args__ = (
        UniqueConstraint("case_id", "report_version", name="uq_report_version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("case_investigations.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True, native_uuid=True),
        ForeignKey("analysis_runs.id", ondelete="SET NULL"),
        comment="which investigation generation this report reflects",
    )
    report_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    json_ir_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    signed_pdf_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    # UNSIGNED | SIGNED | SUPERSEDED
    signature_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="UNSIGNED"
    )
    certificate_type: Mapped[Optional[str]] = mapped_column(
        String(128), comment="e.g. SECTION_63_BSA"
    )
    rendered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
