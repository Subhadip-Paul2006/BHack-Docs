"""Unit tests for ``ps05.core.dlq`` and ``ps05.core.admission``.

Verifies: the DLQ routes every PERMANENT error class to quarantine and every
RETRYABLE class to retry; redrive is dual-control (refused without operator /
context); quarantine records are retrievable by task id. Admission: per-case /
per-user / per-engine caps each block a second over-limit request; queue-capacity
overflow sheds a tagged non-critical request before a critical one; the CPU
fallback decision; and queue-aging / fairness ordering.
"""

from __future__ import annotations

import pytest

from ps05.core.admission import AdmissionController, AdmissionRequest
from ps05.core.dlq import DeadLetterQueue
from ps05.core.errors import PERMANENT, RETRYABLE


# ---------------------------------------------------------------------------
# DLQ
# ---------------------------------------------------------------------------


def test_classify_routes_permanent_and_retryable():
    for cls in PERMANENT:
        assert DeadLetterQueue().classify(cls) == "permanent", f"{cls} must be quarantined"
    for cls in RETRYABLE:
        assert DeadLetterQueue().classify(cls) == "retryable", f"{cls} must be retried"


def test_redrive_requires_operator():
    dlq = DeadLetterQueue()
    dlq.quarantine("task-1", classify_error_class())
    with pytest.raises(Exception) as exc:
        dlq.redrive(["task-1"], operator="", context={"basis": "operator review"})
    assert isinstance(exc.value, (PermissionError, ValueError))


def test_redrive_requires_context():
    dlq = DeadLetterQueue()
    dlq.quarantine("task-1", classify_error_class())
    with pytest.raises(Exception) as exc:
        dlq.redrive(["task-1"], operator="examiner-a", context={})
    assert isinstance(exc.value, (PermissionError, ValueError))


def test_redrive_success_with_operator_and_context():
    dlq = DeadLetterQueue()
    dlq.quarantine("task-1", classify_error_class())
    released = dlq.redrive(["task-1"], operator="examiner-a", context={"basis": "reviewed"})
    assert len(released) == 1
    assert released[0].disposition == "redriven"
    # The task is released out of quarantine.
    assert dlq.pending() == []


def test_quarantine_retrievable_by_task_id():
    dlq = DeadLetterQueue()
    record = dlq.quarantine("task-7", classify_error_class(), error_code="DIGEST_MISMATCH", reason="integrity")
    assert record.task_id == "task-7"
    found = [r for r in dlq.pending() if r.task_id == "task-7"]
    assert len(found) == 1
    assert found[0] is record


def classify_error_class():
    """Return a concrete PERMANENT class for quarantine fixtures."""
    return next(iter(PERMANENT))


# ---------------------------------------------------------------------------
# Admission: concurrency caps
# ---------------------------------------------------------------------------


def _cap_request(**overrides) -> AdmissionRequest:
    base = dict(
        task_id="t-1",
        case_id="case-1",
        user_id="user-1",
        engine_id="engine-1",
        workload_class="analysis",
    )
    base.update(overrides)
    return AdmissionRequest(**base)


def test_per_case_cap_enforced_on_second_request():
    ctrl = AdmissionController(case_concurrency=1, user_concurrency=5, engine_concurrency=5)
    first = ctrl.admit([_cap_request(task_id="t-1")])
    second = ctrl.admit([_cap_request(task_id="t-2")])
    assert first[0].admitted is True
    assert second[0].admitted is False
    assert "case_concurrency_exceeded" in second[0].reasons


def test_per_user_cap_enforced_on_second_request():
    ctrl = AdmissionController(case_concurrency=5, user_concurrency=1, engine_concurrency=5)
    first = ctrl.admit([_cap_request(task_id="t-1")])
    second = ctrl.admit([_cap_request(task_id="t-2")])
    assert first[0].admitted is True
    assert second[0].admitted is False
    assert "user_concurrency_exceeded" in second[0].reasons


def test_per_engine_cap_enforced_on_second_request():
    ctrl = AdmissionController(case_concurrency=5, user_concurrency=5, engine_concurrency=1)
    first = ctrl.admit([_cap_request(task_id="t-1")])
    second = ctrl.admit([_cap_request(task_id="t-2")])
    assert first[0].admitted is True
    assert second[0].admitted is False
    assert "engine_concurrency_exceeded" in second[0].reasons


# ---------------------------------------------------------------------------
# Admission: queue-capacity overflow sheds non-critical before critical
# ---------------------------------------------------------------------------


def test_overflow_sheds_tagged_non_critical_before_critical():
    ctrl = AdmissionController(
        class_queue_capacity={"rag_indexing": 1},
        case_concurrency=5,
        user_concurrency=5,
        engine_concurrency=5,
    )
    shed = AdmissionRequest(
        task_id="t-shed",
        case_id="c",
        user_id="u",
        engine_id="e",
        workload_class="rag_indexing",
        is_critical=False,
    )
    critical = AdmissionRequest(
        task_id="t-critical",
        case_id="c",
        user_id="u",
        engine_id="e",
        workload_class="rag_indexing",
        is_critical=True,
    )
    results = ctrl.admit([shed, critical])
    by_task = {r.task_id: r for r in results}
    # Critical is admitted; the overflowed slot sheds the non-critical request.
    assert by_task["t-critical"].admitted is True
    assert by_task["t-shed"].admitted is False
    assert any("queue_capacity_exceeded" in reason for reason in by_task["t-shed"].reasons)


# ---------------------------------------------------------------------------
# Admission: CPU-fallback routing
# ---------------------------------------------------------------------------


def _gpu_task(vram: int = 1024) -> AdmissionRequest:
    return AdmissionRequest(
        task_id="t-gpu",
        case_id="c",
        user_id="u",
        engine_id="e",
        workload_class="ml",
        required_vram_bytes=vram,
    )


def test_cpu_fallback_when_gpu_unavailable_and_cpu_supported():
    # No VRAM available, but the model has a validated CPU path -> fall back.
    ctrl = AdmissionController(gpu_budget=lambda required: 0.0)
    assert ctrl.route_to_cpu(_gpu_task(), model_supports_cpu=True) is True


def test_no_cpu_fallback_when_gpu_available():
    # GPU has enough VRAM -> stay on the GPU, never silently degrade to CPU.
    ctrl = AdmissionController(gpu_budget=lambda required: float(required))
    assert ctrl.route_to_cpu(_gpu_task(), model_supports_cpu=True) is False


def test_no_cpu_fallback_when_model_lacks_cpu_path():
    # GPU unavailable AND the model has no validated CPU path -> hard GPU-gated.
    ctrl = AdmissionController(gpu_budget=lambda required: 0.0)
    assert ctrl.route_to_cpu(_gpu_task(), model_supports_cpu=False) is False


# ---------------------------------------------------------------------------
# Admission: queue-aging / fairness ordering
# ---------------------------------------------------------------------------


def test_long_waiting_audio_ranks_ahead_of_newer_video():
    # One shared case slot: only the request that sorts first is admitted.
    ctrl = AdmissionController(
        case_concurrency=1,
        user_concurrency=5,
        engine_concurrency=5,
        queue_aging_window=100.0,
    )
    # The audio job has a low base priority but has waited a very long time,
    # so its queue-aging bonus lifts it ahead of the newer video job that has
    # a higher base priority but has barely been in the queue.
    audio = AdmissionRequest(
        task_id="t-audio",
        case_id="c",
        user_id="u",
        engine_id="e",
        workload_class="audio_queue",
        priority=0,
        queued_at=0.0,
    )
    video = AdmissionRequest(
        task_id="t-video",
        case_id="c",
        user_id="u",
        engine_id="e",
        workload_class="video_queue",
        priority=5,
        queued_at=900.0,
    )
    # audio effective = 0 + (1000-0)/100 = 10.0
    # video effective = 5 + (1000-900)/100 = 6.0  -> audio ranks ahead.
    results = ctrl.admit([video, audio], now=1000.0)
    by_task = {r.task_id: r for r in results}
    assert by_task["t-audio"].admitted is True
    assert by_task["t-video"].admitted is False
    assert "case_concurrency_exceeded" in by_task["t-video"].reasons
