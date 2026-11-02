"""Admission control (TRD § 23.9, RES-004 / RES-005).

Decides whether a new task of a workload class is admitted — the
scheduler, not the worker, is the admission authority. Pure logic; this
module deliberately does **not** import anything from ``ps05.infra``
(framework-agnostic by contract): GPU capacity is supplied by the
caller as a :data:`GpuBudget` callable wired to live VRAM telemetry
(OBS-005).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

#: (required_vram_bytes) -> available_vram_bytes on a suitable device
#: right now. Never assumes homogeneous VRAM across a fleet (RES-005).
GpuBudget = Callable[[int], float]


@dataclass
class AdmissionRequest:
    """One task asking for admission (§ 23.4.4 record + § 23.9 class)."""

    task_id: str
    case_id: str
    user_id: str
    engine_id: str
    workload_class: str
    priority: int = 0
    queued_at: float = 0.0  # enqueue timestamp; drives queue aging
    required_vram_bytes: int = 0
    model_supports_cpu: bool = False
    is_critical: bool = True  # non-critical (indexing / RAG) is shed first


@dataclass
class AdmissionResult:
    task_id: str
    admitted: bool
    route: Optional[str] = None  # "GPU" | "CPU"
    reasons: List[str] = field(default_factory=list)


class AdmissionController:
    """Per-case / per-user / per-engine concurrency caps, bounded-queue
    capacity, GPU admission and priority + fairness + queue-aging
    (RES-004). Under overload, non-critical work (indexing, RAG
    projection) is shed first.

    The controller owns its in-flight counters and the GPU budget; the
    budget callable is injected (constructor or :meth:`set_gpu_budget`)
    so the module stays framework-agnostic.
    """

    def __init__(
        self,
        *,
        case_concurrency: int = 8,
        user_concurrency: int = 4,
        engine_concurrency: int = 8,
        class_queue_capacity: Optional[Dict[str, int]] = None,
        queue_aging_window: float = 300.0,
        gpu_budget: Optional[GpuBudget] = None,
    ) -> None:
        self._case_cap = case_concurrency
        self._user_cap = user_concurrency
        self._engine_cap = engine_concurrency
        self._class_cap: Dict[str, int] = dict(class_queue_capacity or {})
        self._queue_aging_window = queue_aging_window
        self._gpu_budget: GpuBudget = gpu_budget or (lambda _required_vram_bytes: float("inf"))
        # In-flight counters keyed by ("case"|"user"|"engine"|"class", id).
        self._active: Dict[tuple, int] = {}

    # ------------------------------------------------------------------
    def set_gpu_budget(self, gpu_budget: GpuBudget) -> None:
        """Bind the live VRAM telemetry callable (OBS-005)."""
        self._gpu_budget = gpu_budget

    def active_count(self, key: tuple) -> int:
        return self._active.get(key, 0)

    # ------------------------------------------------------------------
    def route_to_cpu(self, task: AdmissionRequest, model_supports_cpu: bool) -> bool:
        """Pure GPU->CPU routing decision (RES-005).

        CPU fallback is allowed **only** where the model genuinely has a
        validated CPU path; a model without one is hard-GPU-gated, not
        silently degraded.
        """
        if task.required_vram_bytes == 0:
            return True
        available = self._gpu_budget(task.required_vram_bytes)
        if available >= task.required_vram_bytes:
            return False
        return model_supports_cpu

    # ------------------------------------------------------------------
    def admit(self, requests: List[AdmissionRequest], *, now: Optional[float] = None) -> List[AdmissionResult]:
        """Admit as many of ``requests`` as capacity allows.

        Scheduling order: critical work before shed work, then
        priority + queue-aging (older jobs age up — a low-priority case
        is not held indefinitely while high-priority work runs,
        RES-004), ties broken by queue-entry age. Each admitted task
        increments the in-flight counters so later requests see them.
        """
        now = time.time() if now is None else now
        order = sorted(
            requests,
            key=lambda r: (
                0 if r.is_critical else 1,
                -self._effective_priority(r, now),
                r.queued_at,
                r.task_id,
            ),
        )
        results: List[AdmissionResult] = []
        for request in order:
            result = self._admit_one(request)
            if result.admitted:
                self._active[("case", request.case_id)] = self._active.get(("case", request.case_id), 0) + 1
                self._active[("user", request.user_id)] = self._active.get(("user", request.user_id), 0) + 1
                self._active[("engine", request.engine_id)] = self._active.get(("engine", request.engine_id), 0) + 1
                self._active[("class", request.workload_class)] = self._active.get(("class", request.workload_class), 0) + 1
            results.append(result)
        return results

    def release(self, request: AdmissionRequest) -> None:
        """Decrement the in-flight counters when a task leaves the queue
        (success, failure, shed) — capacity is per-queue, not per-case
        lifetime."""
        for key in (
            ("case", request.case_id),
            ("user", request.user_id),
            ("engine", request.engine_id),
            ("class", request.workload_class),
        ):
            self._active[key] = max(0, self._active.get(key, 1) - 1)

    # ------------------------------------------------------------------
    def _effective_priority(self, request: AdmissionRequest, now: float) -> float:
        if self._queue_aging_window > 0:
            age_bonus = max(0.0, now - request.queued_at) / self._queue_aging_window
        else:
            age_bonus = 0.0
        return request.priority + age_bonus

    def _admit_one(self, request: AdmissionRequest) -> AdmissionResult:
        reasons: List[str] = []

        class_cap = self._class_cap.get(request.workload_class)
        if class_cap is not None and self._active.get(("class", request.workload_class), 0) >= class_cap:
            reasons.append(f"queue_capacity_exceeded:{request.workload_class}")
        if self._active.get(("case", request.case_id), 0) >= self._case_cap:
            reasons.append("case_concurrency_exceeded")
        if self._active.get(("user", request.user_id), 0) >= self._user_cap:
            reasons.append("user_concurrency_exceeded")
        if self._active.get(("engine", request.engine_id), 0) >= self._engine_cap:
            reasons.append("engine_concurrency_exceeded")

        if self.route_to_cpu(request, request.model_supports_cpu):
            route = "CPU"
        elif request.required_vram_bytes > 0:
            route = "GPU"
        elif reasons:
            route = None
        else:
            route = "CPU"

        if route is None or reasons:
            return AdmissionResult(request.task_id, admitted=False, route=route, reasons=reasons)
        return AdmissionResult(request.task_id, admitted=True, route=route)
