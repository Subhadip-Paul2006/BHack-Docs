"""Unit tests for ``ps05.core.checkpoints``: digest linkage + 13 stage names.

Verifies the § 23.7 durable-checkpoint contract: ``save`` refuses a checkpoint
with a missing/empty input or output digest (``CheckpointIntegrityError``); a
valid dual-digest checkpoint succeeds; ``resume`` returns the last completed
checkpoint or None; and the module exports exactly the 13 canonical TRD
stage-name constants with the exact string values.
"""

from __future__ import annotations

import ps05.core.checkpoints as checkpoints
from ps05.core.checkpoints import (
    CheckpointIntegrityError,
    CheckpointStore,
)


def test_save_rejects_missing_output_digest():
    store = CheckpointStore()
    try:
        store.save("cp-1", "stage", {}, input_digest="abc", output_digest="")
        raised = False
    except CheckpointIntegrityError:
        raised = True
    assert raised, "empty output digest must raise (incomplete checkpoint)"


def test_save_rejects_missing_input_digest():
    store = CheckpointStore()
    try:
        store.save("cp-1", "stage", {}, input_digest="", output_digest="abc")
        raised = False
    except CheckpointIntegrityError:
        raised = True
    assert raised, "missing input linkage must raise (incomplete checkpoint)"


def test_valid_checkpoint_succeeds():
    store = CheckpointStore()
    rec = store.save("cp-1", "stage", {"chunk": 1}, input_digest="in", output_digest="out", task_id="task-1")
    assert rec.checkpoint_id == "cp-1"
    assert rec.is_complete is True
    assert rec.verify() is True


def test_resume_returns_last_completed_checkpoint():
    store = CheckpointStore()
    store.save("cp-a", "stage-a", {}, "in-a", "out-a", task_id="task-9")
    store.save("cp-b", "stage-b", {}, "in-b", "out-b", task_id="task-9")
    last = store.resume("task-9")
    assert last is not None
    assert last.checkpoint_id == "cp-b"


def test_resume_none_when_no_checkpoint():
    store = CheckpointStore()
    assert store.resume("unknown-task") is None


def _stage_constant_names() -> dict:
    """Name -> expected string value for all 13 TRD § 23.7 stage constants."""
    return {
        "CP_UPLOAD_CHUNK": "cp_upload_chunk",
        "CP_PREDECODE_HASH": "cp_predecode_hash",
        "CP_WORM_COMMIT": "cp_worm_commit",
        "CP_CONTAINER_METADATA": "cp_container_metadata",
        "CP_VIDEO_CHUNK": "cp_video_chunk",
        "CP_AUDIO_STREAM_EXTRACTION": "cp_audio_stream_extraction",
        "CP_FRAME_SAMPLING": "cp_frame_sampling",
        "CP_MODEL_INFERENCE_BATCH": "cp_model_inference_batch",
        "CP_PCAP_DISSECTION_BATCH": "cp_pcap_dissection_batch",
        "CP_FILESYSTEM_PARTITION": "cp_filesystem_partition",
        "CP_FINDING_NORMALIZATION": "cp_finding_normalization",
        "CP_VECTOR_INDEX_BATCH": "cp_vector_index_batch",
        "CP_REPORT_COMPILATION": "cp_report_compilation",
    }


def test_module_exports_exactly_13_stage_constants():
    expected = _stage_constant_names()
    assert len(expected) == 13
    # Every constant is exported by the module with its exact string value.
    for name, value in expected.items():
        assert getattr(checkpoints, name) == value
    assert set(checkpoints.CANONICAL_CHECKPOINTS) == set(expected.values())
