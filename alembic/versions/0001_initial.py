"""Initial schema: the full TRD § 23.11 platform table set.

Revision ID: 0001
Revises:
Create Date: 2026-01-15

Creates all 22 platform tables in dependency order. Target dialect is
PostgreSQL (§ 23.11); UUID/JSONB types carry sqlite variants so the same
revision applies to the in-memory SQLite test default.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import JSON
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Native UUID / JSONB on PostgreSQL, plain equivalents elsewhere.
UUID = postgresql.UUID(as_uuid=True).with_variant(sa.UUID(), "sqlite")
JSONB = postgresql.JSONB().with_variant(JSON(), "sqlite")


def _uuid_pk() -> sa.Column:
    return sa.Column(UUID, primary_key=True)


def _ts() -> sa.Column:
    return sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def upgrade() -> None:
    # -- Investigation & evidence plane ------------------------------------
    op.create_table(
        "case_investigations",
        _uuid_pk(),
        sa.Column("case_id", UUID, nullable=False, unique=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", sa.String(64), nullable=False, server_default="CREATED",
                  comment="lifecycle state § 23.4.1"),
        sa.Column("created_by", sa.String(128)),
        sa.Column("jurisdiction", sa.String(64), comment="legal jurisdiction code"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "evidence_objects",
        _uuid_pk(),
        sa.Column("case_id", UUID, sa.ForeignKey("case_investigations.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("exhibit_label", sa.String(128), nullable=False),
        sa.Column("original_filename", sa.String(500)),
        sa.Column("mime_type", sa.String(200)),
        sa.Column("file_size_bytes", sa.BigInteger()),
        sa.Column("sha256", sa.String(64)),
        sa.Column("sha3_512", sa.String(128)),
        sa.Column("storage_vault_urn", sa.String(512),
                  comment="urn:ceipl:vault:... resolved after WORM commit"),
        sa.Column("status", sa.String(64), nullable=False, server_default="CREATED"),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_evidence_objects_case_id", "evidence_objects", ["case_id"])
    op.create_index("ix_evidence_objects_sha256", "evidence_objects", ["sha256"])

    op.create_table(
        "acquisition_records",
        _uuid_pk(),
        sa.Column("evidence_id", UUID, sa.ForeignKey("evidence_objects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("acquired_by", sa.String(128)),
        sa.Column("acquisition_method", sa.String(128)),
        sa.Column("source_device", sa.String(255)),
        sa.Column("notes", sa.Text()),
    )
    op.create_index("ix_acquisition_records_evidence_id", "acquisition_records", ["evidence_id"])

    op.create_table(
        "evidence_manifests",
        _uuid_pk(),
        sa.Column("evidence_id", UUID, sa.ForeignKey("evidence_objects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("manifest_type", sa.String(128), nullable=False,
                  comment="PRE_DECODE_DUAL_HASH | WORM_COMMIT | PARSER_FIELD_PROVENANCE | ..."),
        sa.Column("manifest_sha256", sa.String(64)),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_evidence_manifests_evidence_id", "evidence_manifests", ["evidence_id"])
    op.create_index("ix_evidence_manifests_manifest_sha256", "evidence_manifests", ["manifest_sha256"])

    op.create_table(
        "analysis_runs",
        _uuid_pk(),
        sa.Column("case_id", UUID, sa.ForeignKey("case_investigations.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_id", UUID,
                  sa.ForeignKey("evidence_objects.id", ondelete="SET NULL")),
        sa.Column("generation", sa.String(32), nullable=False, server_default="GEN-0",
                  comment='"GEN-0" first run, "GEN-1" re-run ... (§ 23.5)'),
        sa.Column("configuration_version", sa.String(128)),
        sa.Column("status", sa.String(64), nullable=False, server_default="CREATED",
                  comment="CREATED | PLANNED | RUNNING | COMPLETED | PARTIAL_SUCCESS | FAILED | CANCELLED"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_analysis_runs_case_id", "analysis_runs", ["case_id"])
    op.create_index("ix_analysis_runs_evidence_id", "analysis_runs", ["evidence_id"])

    # task_id FK is added after platform_task_records exists (deferrable on PG).
    op.create_table(
        "derived_artifacts",
        _uuid_pk(),
        sa.Column("evidence_id", UUID, sa.ForeignKey("evidence_objects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("analysis_run_id", UUID,
                  sa.ForeignKey("analysis_runs.id", ondelete="SET NULL")),
        sa.Column("task_id", UUID,
                  comment="FK to platform_task_records.task_id, added after that table"),
        sa.Column("engine_id", sa.String(128)),
        sa.Column("artifact_type", sa.String(128)),
        sa.Column("storage_urn", sa.String(512), nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("idempotency_key", sa.String(255), unique=True,
                  comment="§ 23.5: UNIQUE so duplicate delivery is a no-op"),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_derived_artifacts_evidence_id", "derived_artifacts", ["evidence_id"])
    op.create_index("ix_derived_artifacts_analysis_run_id", "derived_artifacts", ["analysis_run_id"])
    op.create_index("ix_derived_artifacts_task_id", "derived_artifacts", ["task_id"])
    op.create_index("ix_derived_artifacts_sha256", "derived_artifacts", ["sha256"])

    op.create_table(
        "artifact_provenance_dag",
        _uuid_pk(),
        sa.Column("child_artifact_id", UUID, sa.ForeignKey("derived_artifacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_artifact_id", UUID, sa.ForeignKey("derived_artifacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("edge_type", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("child_artifact_id", "parent_artifact_id", name="uq_provenance_edge"),
    )

    # -- Orchestration plane ------------------------------------------------
    op.create_table(
        "platform_workflow_definitions",
        _uuid_pk(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("spec", JSONB, nullable=False, comment="versioned DAG spec § 23.3"),
        sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE",
                  comment="ACTIVE | DEPRECATED | RETIRED"),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("name", "version", name="uq_workflow_name_version"),
    )

    op.create_table(
        "platform_workflow_nodes",
        _uuid_pk(),
        sa.Column("workflow_id", UUID, sa.ForeignKey("platform_workflow_definitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.String(255), nullable=False),
        sa.Column("engine_id", sa.String(128), nullable=False),
        sa.Column("stage_id", sa.String(255), nullable=False),
        sa.Column("queue_class", sa.String(128), comment="queue / workload class § 23.9"),
        sa.Column("retry_policy", JSONB,
                  comment="declared retry policy § 23.10: max_attempts, max_elapsed_retry_duration, backoff"),
        sa.Column("resource_profile_ref", sa.String(255),
                  comment="declared resource profile § 23.9 (RES-002)"),
        sa.Column("depends_on", JSONB,
                  comment="node_ids + per-edge requirement kind (HARD | SOFT)"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("workflow_id", "node_id", name="uq_workflow_node"),
    )
    op.create_index("ix_platform_workflow_nodes_workflow_id", "platform_workflow_nodes", ["workflow_id"])

    # § 23.4.4 canonical task record.
    op.create_table(
        "platform_task_records",
        sa.Column("task_id", UUID, primary_key=True),
        sa.Column("analysis_run_id", UUID, sa.ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("case_id", UUID, sa.ForeignKey("case_investigations.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_id", UUID,
                  sa.ForeignKey("evidence_objects.id", ondelete="SET NULL"),
                  comment="NULL only for case-level tasks"),
        sa.Column("parent_task_id", UUID,
                  sa.ForeignKey("platform_task_records.task_id", ondelete="SET NULL")),
        sa.Column("engine_id", sa.String(128), nullable=False),
        sa.Column("stage_id", sa.String(255), nullable=False),
        sa.Column("input_artifact_ids", JSONB,
                  comment="UUID list of artifacts this stage consumes"),
        sa.Column("input_manifest_hashes", JSONB,
                  comment="per-input SHA-256 digests (CEIPL canonical form)"),
        sa.Column("idempotency_key", sa.String(255), nullable=False,
                  comment="§ 23.5 composite key, UNIQUE"),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("task_deadline", sa.DateTime(timezone=True),
                  comment="hard wall-clock deadline for this attempt"),
        sa.Column("worker_id", sa.String(128), comment="NULL until claimed"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("checkpoint_id", UUID,
                  comment="last completed durable checkpoint § 23.7 (FK added below)"),
        sa.Column("output_artifact_ids", JSONB,
                  comment="UUID list produced, only after SUCCEEDED"),
        sa.Column("model_version", sa.String(255),
                  comment="versioned model/checkpoint ID; NULL for non-ML stages"),
        sa.Column("configuration_version", sa.String(128)),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING",
                  comment="task lifecycle state § 23.4.3"),
        sa.Column("error_class", sa.String(64), comment="§ 23.10 taxonomy code"),
        sa.Column("error_code", sa.String(128), comment="machine detail code"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0",
                  comment="optimistic concurrency counter, bumped per transition"),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("idempotency_key", "attempt_number", name="uq_task_idempotency_attempt"),
    )
    op.create_index("ix_platform_task_records_analysis_run_id", "platform_task_records", ["analysis_run_id"])
    op.create_index("ix_platform_task_records_case_id", "platform_task_records", ["case_id"])
    op.create_index("ix_platform_task_records_evidence_id", "platform_task_records", ["evidence_id"])
    op.create_index("ix_platform_task_records_parent_task_id", "platform_task_records", ["parent_task_id"])

    # Wire the earlier deferrable references now that their targets exist.
    op.execute(
        "ALTER TABLE derived_artifacts ADD CONSTRAINT fk_derived_artifacts_task "
        "FOREIGN KEY (task_id) REFERENCES platform_task_records (task_id) ON DELETE SET NULL"
    )

    # Append-only state events § 23.4.3.
    op.create_table(
        "platform_task_state_events",
        _uuid_pk(),
        sa.Column("task_id", UUID, sa.ForeignKey("platform_task_records.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_platform_task_state_events_task_id", "platform_task_state_events", ["task_id"])

    # Transactional outbox § 23.6.1.
    op.create_table(
        "platform_outbox",
        sa.Column("event_id", UUID, primary_key=True),
        sa.Column("aggregate_type", sa.String(128), nullable=False,
                  comment="e.g. case_investigation, platform_task_records"),
        sa.Column("aggregate_id", UUID, nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False, comment="EV_* domain event"),
        sa.Column("analysis_run_id", UUID),
        sa.Column("case_id", UUID),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("sequence_no", sa.BigInteger(), nullable=False,
                  comment="monotonic per (aggregate_type, aggregate_id)"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("dispatch_status", sa.String(32), nullable=False, server_default="PENDING",
                  comment="PENDING | PUBLISHED | FAILED"),
        sa.UniqueConstraint("aggregate_type", "aggregate_id", "sequence_no", name="uq_outbox_sequence"),
    )
    op.create_index("ix_platform_outbox_analysis_run_id", "platform_outbox", ["analysis_run_id"])
    op.create_index("ix_platform_outbox_case_id", "platform_outbox", ["case_id"])
    op.create_index("ix_platform_outbox_dispatch_status", "platform_outbox", ["dispatch_status"])

    # Processed-message registry § 23.6.2.
    op.create_table(
        "platform_inbox",
        sa.Column("event_id", UUID, primary_key=True,
                  comment="exactly the outbox event_id, natural key"),
        sa.Column("consumer_context", sa.String(128), nullable=False),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("consumer_context", "dedupe_key", name="uq_inbox_dedupe"),
    )

    # Worker leases § 23.6.4.
    op.create_table(
        "platform_task_lease",
        sa.Column("task_id", UUID, primary_key=True,
                  sa.ForeignKey("platform_task_records.task_id", ondelete="CASCADE")),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("lease_granted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    # Lease recovery audit trail § 23.6.4.
    op.create_table(
        "platform_task_lease_events",
        _uuid_pk(),
        sa.Column("task_id", UUID, sa.ForeignKey("platform_task_records.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False,
                  comment="ACQUIRED | HEARTBEAT | EXPIRED | REASSIGNED | RELEASED"),
        sa.Column("prior_worker_id", sa.String(128)),
        sa.Column("new_worker_id", sa.String(128)),
        sa.Column("prior_checkpoint_id", UUID),
        sa.Column("reaped_by", sa.String(128), comment="actor that reaped, e.g. reaper-<worker>"),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_platform_task_lease_events_task_id", "platform_task_lease_events", ["task_id"])

    # Versioned resource profiles § 23.9 (RES-002).
    op.create_table(
        "platform_engine_execution_profiles",
        _uuid_pk(),
        sa.Column("engine_id", sa.String(128), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("workload_class", sa.String(128)),
        sa.Column("max_memory_mb", sa.Integer()),
        sa.Column("max_batch_size", sa.Integer()),
        sa.Column("gpu_class", sa.String(64)),
        sa.Column("task_deadline_seconds", sa.Integer()),
        sa.Column("resources", JSONB, comment="free-form resource budget payload (RES-001)"),
        sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE",
                  comment="ACTIVE | DEPRECATED"),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("engine_id", "profile_version", name="uq_engine_profile_version"),
    )
    op.create_index("ix_platform_engine_execution_profiles_engine_id",
                    "platform_engine_execution_profiles", ["engine_id"])

    # -- Results & output plane ---------------------------------------------
    op.create_table(
        "findings",
        _uuid_pk(),
        sa.Column("case_id", UUID, sa.ForeignKey("case_investigations.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_id", UUID,
                  sa.ForeignKey("evidence_objects.id", ondelete="SET NULL")),
        sa.Column("analysis_run_id", UUID,
                  sa.ForeignKey("analysis_runs.id", ondelete="SET NULL")),
        sa.Column("task_id", UUID,
                  sa.ForeignKey("platform_task_records.task_id", ondelete="SET NULL")),
        sa.Column("engine_id", sa.String(128), nullable=False),
        sa.Column("finding_type", sa.String(128), nullable=False,
                  comment="MANIPULATION_INDICATOR | CONTRADICTION | FRAME_GAP_OBSERVED | ..."),
        sa.Column("severity", sa.String(32), nullable=False, server_default="INFORMATIONAL",
                  comment="INFORMATIONAL | LOW | MEDIUM | HIGH | CRITICAL"),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("detail", JSONB, comment="structured evidence payload"),
        sa.Column("model_version", sa.String(255)),
        sa.Column("configuration_version", sa.String(128)),
        sa.Column("input_artifact_digest", sa.String(128)),
        sa.Column("idempotency_key", sa.String(255),
                  comment="§ 23.5: UNIQUE so duplicate delivery is a no-op"),
        sa.Column("status", sa.String(32), nullable=False, server_default="OPEN",
                  comment="OPEN | CORROBORATED | REJECTED | ADJUDICATED"),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("idempotency_key", name="uq_findings_idempotency"),
    )
    op.create_index("ix_findings_case_id", "findings", ["case_id"])
    op.create_index("ix_findings_evidence_id", "findings", ["evidence_id"])
    op.create_index("ix_findings_analysis_run_id", "findings", ["analysis_run_id"])
    op.create_index("ix_findings_task_id", "findings", ["task_id"])

    # REO-003 model-execution provenance.
    op.create_table(
        "model_execution_provenance",
        _uuid_pk(),
        sa.Column("task_id", UUID, sa.ForeignKey("platform_task_records.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(255), nullable=False,
                  comment="versioned model/checkpoint ID, e.g. sha256-bound"),
        sa.Column("checkpoint_hash", sa.String(128)),
        sa.Column("framework", sa.String(64)),
        sa.Column("device", sa.String(32), comment="cuda | cpu (per PS05_DEVICE / fallback)"),
        sa.Column("input_batch_hash", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_model_execution_provenance_task_id", "model_execution_provenance", ["task_id"])

    # Durable checkpoints § 23.7.
    op.create_table(
        "platform_checkpoints",
        _uuid_pk(),
        sa.Column("task_id", UUID, sa.ForeignKey("platform_task_records.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("checkpoint_key", sa.String(128), nullable=False,
                  comment="cp_upload_chunk | cp_predecode_hash | cp_worm_commit | ... (§ 23.7)"),
        sa.Column("stage_id", sa.String(255)),
        sa.Column("payload", JSONB, comment="persisted chunk outputs"),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.text("false"),
                  comment="complete only when output is persisted + verified + lineage-linked"),
        sa.Column("persisted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("task_id", "checkpoint_key", name="uq_checkpoint_key"),
    )
    op.create_index("ix_platform_checkpoints_task_id", "platform_checkpoints", ["task_id"])

    # Wire task-record -> checkpoint FK now that the checkpoint table exists.
    op.execute(
        "ALTER TABLE platform_task_records "
        "ADD CONSTRAINT fk_task_records_checkpoint FOREIGN KEY (checkpoint_id) "
        "REFERENCES platform_checkpoints (id) ON DELETE SET NULL"
    )

    # Quarantine / reconciliation states § 22.12, § 23.10, STG-002.
    op.create_table(
        "quarantine_outputs",
        _uuid_pk(),
        sa.Column("case_id", UUID,
                  sa.ForeignKey("case_investigations.case_id", ondelete="SET NULL")),
        sa.Column("evidence_id", UUID,
                  sa.ForeignKey("evidence_objects.id", ondelete="SET NULL")),
        sa.Column("quarantine_class", sa.String(64), nullable=False,
                  comment="HASH_MISMATCH | ORPHAN_OBJECT | PHANTOM_RECORD | DEAD_LETTERED_OUTPUT | ..."),
        sa.Column("reason", sa.Text()),
        sa.Column("original_storage_urn", sa.String(512)),
        sa.Column("payload", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_quarantine_outputs_case_id", "quarantine_outputs", ["case_id"])
    op.create_index("ix_quarantine_outputs_evidence_id", "quarantine_outputs", ["evidence_id"])

    # RAG / search indexing § 23.13.
    op.create_table(
        "indexing_jobs",
        _uuid_pk(),
        sa.Column("case_id", UUID, sa.ForeignKey("case_investigations.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("index_type", sa.String(128), nullable=False,
                  comment="CASE_RAG | FINDING_FULLTEXT | ..."),
        sa.Column("source_ref", sa.String(512)),
        sa.Column("progress", JSONB),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING",
                  comment="PENDING | RUNNING | COMPLETED | FAILED | STALE"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_indexing_jobs_case_id", "indexing_jobs", ["case_id"])

    # Sealed report snapshots (extends § 21.9 ufredf_reports).
    op.create_table(
        "report_snapshots",
        _uuid_pk(),
        sa.Column("case_id", UUID, sa.ForeignKey("case_investigations.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("analysis_run_id", UUID,
                  sa.ForeignKey("analysis_runs.id", ondelete="SET NULL"),
                  comment="which investigation generation this report reflects"),
        sa.Column("report_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("json_ir_sha256", sa.String(64)),
        sa.Column("signed_pdf_sha256", sa.String(64)),
        sa.Column("signature_status", sa.String(32), nullable=False, server_default="UNSIGNED",
                  comment="UNSIGNED | SIGNED | SUPERSEDED"),
        sa.Column("certificate_type", sa.String(128), comment="e.g. SECTION_63_BSA"),
        sa.Column("rendered_at", sa.DateTime(timezone=True)),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("case_id", "report_version", name="uq_report_version"),
    )
    op.create_index("ix_report_snapshots_case_id", "report_snapshots", ["case_id"])


def downgrade() -> None:
    op.execute("ALTER TABLE platform_task_records DROP CONSTRAINT IF EXISTS fk_task_records_checkpoint")
    op.execute("ALTER TABLE derived_artifacts DROP CONSTRAINT IF EXISTS fk_derived_artifacts_task")
    op.drop_table("report_snapshots")
    op.drop_table("indexing_jobs")
    op.drop_table("quarantine_outputs")
    op.drop_table("platform_checkpoints")
    op.drop_table("model_execution_provenance")
    op.drop_table("findings")
    op.drop_table("platform_engine_execution_profiles")
    op.drop_table("platform_task_lease_events")
    op.drop_table("platform_task_lease")
    op.drop_table("platform_inbox")
    op.drop_table("platform_outbox")
    op.drop_table("platform_task_state_events")
    op.drop_table("platform_task_records")
    op.drop_table("platform_workflow_nodes")
    op.drop_table("platform_workflow_definitions")
    op.drop_table("artifact_provenance_dag")
    op.drop_table("derived_artifacts")
    op.drop_table("analysis_runs")
    op.drop_table("evidence_manifests")
    op.drop_table("acquisition_records")
    op.drop_table("evidence_objects")
    op.drop_table("case_investigations")
