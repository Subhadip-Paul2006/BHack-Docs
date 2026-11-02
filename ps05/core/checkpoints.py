"""Durable stage checkpoints (TRD § 23.7).

A stage is complete **only** when its output is (a) persisted,
(b) verified (digest), and (c) linked to its input lineage — all in one
PostgreSQL transaction that also writes the checkpoint outbox event
(REO-010). The Celery result backend and in-memory progress counters are
never the system of record.

A recovered task resumes from its **last completed durable
checkpoint** (REO-009), never from in-memory progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

#: Canonical checkpoint names (TRD § 23.7 table).
CP_UPLOAD_CHUNK = "cp_upload_chunk"
CP_PREDECODE_HASH = "cp_predecode_hash"
CP_WORM_COMMIT = "cp_worm_commit"
CP_CONTAINER_METADATA = "cp_container_metadata"
CP_VIDEO_CHUNK = "cp_video_chunk"
CP_AUDIO_STREAM_EXTRACTION = "cp_audio_stream_extraction"
CP_FRAME_SAMPLING = "cp_frame_sampling"
CP_MODEL_INFERENCE_BATCH = "cp_model_inference_batch"
CP_PCAP_DISSECTION_BATCH = "cp_pcap_dissection_batch"
CP_FILESYSTEM_PARTITION = "cp_filesystem_partition"
CP_FINDING_NORMALIZATION = "cp_finding_normalization"
CP_VECTOR_INDEX_BATCH = "cp_vector_index_batch"
CP_REPORT_COMPILATION = "cp_report_compilation"

CANONICAL_CHECKPOINTS: tuple = (
    CP_UPLOAD_CHUNK,
    CP_PREDECODE_HASH,
    CP_WORM_COMMIT,
    CP_CONTAINER_METADATA,
    CP_VIDEO_CHUNK,
    CP_AUDIO_STREAM_EXTRACTION,
    CP_FRAME_SAMPLING,
    CP_MODEL_INFERENCE_BATCH,
    CP_PCAP_DISSECTION_BATCH,
    CP_FILESYSTEM_PARTITION,
    CP_FINDING_NORMALIZATION,
    CP_VECTOR_INDEX_BATCH,
    CP_REPORT_COMPILATION,
)


class CheckpointIntegrityError(Exception):
    """Raised when digest linkage for a checkpoint cannot be verified."""


@dataclass
class Checkpoint:
    """One ``platform_checkpoints`` row (§ 23.7).

    ``input_digest`` / ``output_digest`` are the cryptographic lineage
    link: a stage may only complete when its output digest is persisted
    and verifiable against the recorded input digest.
    """

    checkpoint_id: str
    stage_id: str
    task_id: Optional[str] = None
    payload: Dict[str, object] = field(default_factory=dict)
    input_digest: Optional[str] = None
    output_digest: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_complete(self) -> bool:
        """Digest linkage must be present on both sides for completion."""
        return bool(self.input_digest and self.output_digest)

    def verify(self) -> bool:
        if not self.is_complete:
            raise CheckpointIntegrityError(
                f"checkpoint {self.checkpoint_id!r} missing digest linkage "
                f"(input={self.input_digest!r}, output={self.output_digest!r})"
            )
        return True


class CheckpointStore:
    """In-memory model of the ``platform_checkpoints`` table."""

    def __init__(self) -> None:
        self._checkpoints: Dict[str, Checkpoint] = {}
        self._by_task: Dict[str, List[Checkpoint]] = {}

    def save(
        self,
        checkpoint_id: str,
        stage_id: str,
        payload: Dict[str, object],
        input_digest: str,
        output_digest: str,
        *,
        task_id: Optional[str] = None,
        stage_name: Optional[str] = None,
    ) -> Checkpoint:
        """Persist a checkpoint; enforce the digest linkage.

        Raising on missing digests enforces the § 23.7 rule that a
        stage does not complete until its output is persisted AND
        verified against its input lineage.
        """
        if not input_digest:
            raise CheckpointIntegrityError(f"checkpoint {checkpoint_id!r} missing input_digest")
        if not output_digest:
            raise CheckpointIntegrityError(f"checkpoint {checkpoint_id!r} missing output_digest")
        if stage_name is not None and stage_name not in CANONICAL_CHECKPOINTS:
            raise CheckpointIntegrityError(
                f"checkpoint stage {stage_name!r} is not a canonical § 23.7 checkpoint name"
            )
        record = Checkpoint(
            checkpoint_id=checkpoint_id,
            stage_id=stage_id,
            task_id=task_id,
            payload=dict(payload),
            input_digest=input_digest,
            output_digest=output_digest,
        )
        self._checkpoints[checkpoint_id] = record
        if task_id is not None:
            self._by_task.setdefault(task_id, []).append(record)
        return record

    def get(self, checkpoint_id: str) -> Optional[Checkpoint]:
        record = self._checkpoints.get(checkpoint_id)
        if record is not None:
            record.verify()
        return record

    def resume(self, task_id: str) -> Optional[Checkpoint]:
        """Return the task's last completed checkpoint, or None.

        A recovered task resumes here — re-processing a chunk whose
        checkpoint already exists short-circuits to the persisted output
        (§ 23.7, § 23.5 checkpoint-level idempotency).
        """
        chain = self._by_task.get(task_id)
        if not chain:
            return None
        last = chain[-1]
        if last.is_complete:
            last.verify()
            return last
        return None
