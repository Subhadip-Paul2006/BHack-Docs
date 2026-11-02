"""Versioned workload resource profiles, VRAM budgeting, backpressure and load-shedding.

Implements TRD § 23.9 (RES-001…RES-007): versioned `ResourceProfile` records
reconciled from `platform_engine_execution_profiles` (rows referenced by
§ 23.15 `Resource Profile Ref`, e.g. ``AURAS_ARE_v1``), an explicit GPU VRAM
budget that never assumes a homogeneous single card, bounded queue capacity,
per-case / per-user / per-engine concurrency caps, bounded fan-out, queue aging
and load-shedding of non-critical work.

This module owns the resource/scheduling *data*. Admit/reject decisions on
tasks are the responsibility of :mod:`ps05.core.admission`, which consumes the
types exposed here (``WorkloadProfile``, ``GpuBudget``, ``GpuAdmission``,
``BackpressureLimits``, ``fairness_rank``).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

__all__ = [
    "WorkloadProfile",
    "ResourceProfileRegistry",
    "KNOWN_PROFILE_REFS",
    "GpuDevice",
    "GpuBudget",
    "GpuAdmission",
    "BackpressureLimits",
    "LoadShedder",
    "QueueAgeTracker",
    "fairness_rank",
    "parse_profile_ref",
]


# ---------------------------------------------------------------------------
# Workload profiles (RES-002)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkloadProfile:
    """One versioned resource profile, mirroring a
    ``platform_engine_execution_profiles`` row (TRD § 23.9, § 23.11).

    ``model_supports_cpu`` (RES-005) is only ever ``True`` where the specific
    model or algorithm has a *validated* equivalent CPU path; a hard-GPU-gated
    model must stay ``False`` and will never be silently degraded.
    """

    profile_id: str
    version: int
    cpu_cores: float
    ram_bytes: int
    gpu_device: Optional[str]  # None -> CPU-only workload
    vram_bytes: int
    disk_scratch_bytes: int
    file_descriptors: int
    max_input_bytes: int
    est_duration_s: float
    max_active: int
    io_bandwidth: int  # bytes/s
    model_footprint: int  # bytes
    batch_size: int
    model_supports_cpu: bool = False


def parse_profile_ref(ref: str) -> Tuple[str, int]:
    """Split a § 23.15 profile reference (``AURAS_ARE_v1``) into
    ``(profile_id, version)`` = ``("AURAS_ARE", 1)``.

    Raises ``ValueError`` on a ref that does not end in ``_v<int>``.
    """
    m = re.fullmatch(r"(.+)_v(\d+)", ref)
    if not m:
        raise ValueError(f"malformed profile reference: {ref!r} (expected e.g. 'AURAS_ARE_v1')")
    return m.group(1), int(m.group(2))


#: § 23.15 canonical engine execution matrix — `Resource Profile Ref` column.
#: These are *names* (profile references), not numbers: every numeric ceiling
#: lives in the versioned profile record, not in code.
KNOWN_PROFILE_REFS: Tuple[str, ...] = (
    "AURAS_ARE_v1",
    "MVSI_v1",
    "ADDE_v1",
    "AESL_v1",
    "IVE_v1",
    "IRE_v1",
    "AIM_v1",
    "FPA_v1",
    "IMLF_v1",
    "GEO_v1",
    "VRE_v1",
    "AFB_v1",
    "AVSC_v1",
    "VDF_v1",
    "VCI_v1",
    "CCTV_v1",
    "ONTF_v1",
    "CCHDF_v1",
    "CMMS_v1",
    "CSTV_v1",
    "CDVF_v1",
    "RAG_v1",
    "UFD_v1",
    "UFREDF_v1",
    "CEIPL_v1",
)


class ResourceProfileRegistry:
    """Versioned lookup for workload profiles (RES-002).

    Lookups are ``(profile_id, version)``-exact; passing no version resolves
    to the newest registered version. ``register`` never overwrites a distinct
    version — supersede explicitly by registering a higher version.
    """

    def __init__(self) -> None:
        self._profiles: dict[tuple[str, int], WorkloadProfile] = {}
        self._lock = threading.Lock()

    def register(self, profile: WorkloadProfile) -> None:
        with self._lock:
            key = (profile.profile_id, profile.version)
            if key in self._profiles:
                existing = self._profiles[key]
                if existing != profile:
                    raise ValueError(
                        f"profile {profile.profile_id} v{profile.version} already registered "
                        f"with different content; bump the version instead"
                    )
                return
            self._profiles[key] = profile

    def get_profile(self, name: str, version: Optional[int] = None) -> WorkloadProfile:
        """Resolve a profile by name or § 23.15 reference.

        ``name`` may be a bare ``profile_id`` (e.g. ``"AURAS_ARE"``) or a
        versioned reference (``"AURAS_ARE_v1"``). With ``version`` given the
        pair is exact; without it, the newest registered version wins.
        """
        if re.fullmatch(r".+_v\d+", name):
            ref_id, ref_version = parse_profile_ref(name)
            if version is not None and version != ref_version:
                raise ValueError(
                    f"profile ref {name!r} implies version {ref_version}, "
                    f"but version={version} was also given"
                )
            name, version = ref_id, ref_version
        with self._lock:
            if version is None:
                candidates = [p for (pid, _v) in self._profiles if pid == name]
                if not candidates:
                    raise KeyError(f"no registered profile named {name!r}")
                return max(candidates, key=lambda p: p.version)
            try:
                return self._profiles[(name, version)]
            except KeyError:
                raise KeyError(f"no registered profile {name!r} v{version}") from None

    def latest(self, name: str) -> WorkloadProfile:
        return self.get_profile(name, version=None)

    def versions(self, name: str) -> Tuple[int, ...]:
        with self._lock:
            return tuple(sorted(v for (pid, v) in self._profiles if pid == name))

    @classmethod
    def from_db_rows(cls, rows: Iterable[Mapping[str, Any]]) -> "ResourceProfileRegistry":
        """Build a registry from ``platform_engine_execution_profiles`` rows
        (TRD § 23.11). This is the sanctioned source of the actual numbers —
        benchmarks and ops tuning write to the table, not to source code."""
        registry = cls()
        col = {
            "profile_id": "profile_id",
            "version": "version",
            "cpu_cores": "cpu_cores",
            "host_ram_bytes": "ram_bytes",
            "gpu_device_id": "gpu_device",
            "gpu_vram_bytes": "vram_bytes",
            "disk_scratch_bytes": "disk_scratch_bytes",
            "file_descriptor_cap": "file_descriptors",
            "max_input_size_bytes": "max_input_bytes",
            "estimated_duration_seconds": "est_duration_s",
            "max_active_tasks": "max_active",
            "io_bandwidth_bps": "io_bandwidth",
            "model_footprint_bytes": "model_footprint",
            "max_batch_size": "batch_size",
        }
        for row in rows:
            kwargs: dict[str, Any] = {}
            for source_key, field_name in col.items():
                if source_key in row:
                    kwargs[field_name] = row[source_key]
            kwargs.setdefault("model_supports_cpu", bool(row.get("model_supports_cpu", False)))
            registry.register(WorkloadProfile(**kwargs))
        return registry


# ---------------------------------------------------------------------------
# GPU admission (RES-003 / RES-005)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuDevice:
    """One device in a heterogeneous fleet. ``isolation`` records a
    hardware-specific isolation primitive (``"mps"``, ``"mig"`` or ``"none"``)
    — used only where the target device actually supports it (RES-005)."""

    device_id: str
    vram_bytes: int
    isolation: str = "none"


class GpuBudget:
    """Per-device VRAM budget table (RES-003).

    Never assumes a homogeneous or single-card fleet: pass the device list
    (e.g. from live VRAM telemetry, OBS-005) and ``total_vram_bytes`` is
    checked against it. Placement is best-fit over the device list; a task
    is *never* oversubscribed onto a second device.
    """

    def __init__(
        self,
        total_vram_bytes: int,
        devices: Optional[Sequence[GpuDevice]] = None,
    ) -> None:
        if total_vram_bytes <= 0:
            raise ValueError("total_vram_bytes must be positive")
        if devices:
            seen: set[str] = set()
            for d in devices:
                if d.vram_bytes <= 0:
                    raise ValueError(f"device {d.device_id} must have positive VRAM")
                if d.device_id in seen:
                    raise ValueError(f"duplicate device_id {d.device_id!r}")
                seen.add(d.device_id)
            device_total = sum(d.vram_bytes for d in devices)
            if device_total != total_vram_bytes:
                raise ValueError(
                    f"sum of device VRAM ({device_total}) != total_vram_bytes ({total_vram_bytes})"
                )
            self._devices: list[GpuDevice] = list(devices)
        else:
            # No fleet description supplied: model as a single opaque device
            # holding the whole budget. Callers with multi-card or
            # heterogeneous fleets MUST pass ``devices``.
            self._devices = [GpuDevice("gpu", total_vram_bytes)]
        self._free: dict[str, int] = {d.device_id: d.vram_bytes for d in self._devices}
        self._lock = threading.Lock()

    @property
    def devices(self) -> Tuple[GpuDevice, ...]:
        return tuple(self._devices)

    def free_vram(self, device_id: str) -> int:
        with self._lock:
            return self._free[device_id]

    def place(self, vram_bytes: int, preferred_device: Optional[str] = None) -> Optional[str]:
        """Best-fit placement; returns the device id or ``None`` if no single
        device has room (an oversized task is rejected, never split)."""
        if vram_bytes < 0:
            raise ValueError("vram_bytes must be >= 0")
        if vram_bytes == 0:
            return None  # nothing to place
        with self._lock:
            candidates = [
                d for d in self._devices if self._free[d.device_id] >= vram_bytes
            ]
            if preferred_device is not None:
                candidates = [d for d in candidates if d.device_id == preferred_device]
            if not candidates:
                return None
            target = min(candidates, key=lambda d: self._free[d.device_id])
            self._free[target.device_id] -= vram_bytes
            return target.device_id

    def release(self, device_id: str, vram_bytes: int) -> None:
        if vram_bytes < 0:
            raise ValueError("vram_bytes must be >= 0")
        with self._lock:
            self._free[device_id] += vram_bytes

    @classmethod
    def from_telemetry(cls, total_vram_bytes: int, telemetry: Mapping[str, int]) -> "GpuBudget":
        """Build the budget from a live per-device VRAM telemetry snapshot
        (OBS-005); ``telemetry`` maps device_id -> total VRAM bytes."""
        devices = [
            GpuDevice(device_id=d, vram_bytes=v)
            for d, v in sorted(telemetry.items())
        ]
        return cls(total_vram_bytes, devices=devices)


class GpuAdmission:
    """Explicit VRAM gate for GPU inference workloads (RES-003).

    ``try_acquire`` / ``release`` pair: a profile is admitted only if one
    device's free VRAM covers it outright. Placement is tracked so
    ``release`` returns exactly what was taken, per ``key`` (the task/lease
    identity that called ``try_acquire``; ``None`` = LIFO per profile).
    """

    def __init__(self, budget: GpuBudget) -> None:
        self._budget = budget
        self._placements: dict[Optional[str], list[tuple[str, int]]] = {}
        self._lock = threading.Lock()

    @property
    def budget(self) -> GpuBudget:
        return self._budget

    def try_acquire(
        self, profile: WorkloadProfile, key: Optional[str] = None
    ) -> bool:
        if profile.vram_bytes == 0:
            return True  # no VRAM to gate on (CPU-only or pre-declared 0-VRAM profile)
        device_id = self._budget.place(profile.vram_bytes, preferred_device=profile.gpu_device)
        if device_id is None:
            return False  # no single device has room; never oversubscribe a second device
        with self._lock:
            self._placements.setdefault(key, []).append((device_id, profile.vram_bytes))
        return True

    def release(self, profile: WorkloadProfile, key: Optional[str] = None) -> None:
        with self._lock:
            stack = self._placements.get(key)
            if not stack:
                return
            device_id, vram = stack.pop()
        self._budget.release(device_id, vram)

    @staticmethod
    def can_cpu_fallback(profile: WorkloadProfile) -> bool:
        """RES-005: CPU fallback only where the model genuinely supports it."""
        return profile.model_supports_cpu


# ---------------------------------------------------------------------------
# Backpressure, concurrency caps, load-shedding, queue aging (RES-004)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackpressureLimits:
    """Bounded capacities the admission scheduler (``ps05.core.admission``)
    must all pass before admitting a new task of a class: queue depth +
    device availability + per-case cap. Values are operator-deployed
    (per-class budgets), never one blind limit."""

    queue_capacity: Mapping[str, int]
    per_case_concurrency: int
    per_user_concurrency: int
    per_engine_concurrency: int
    max_fan_out: int  # REO-005 bounded fan-out

    def check(
        self,
        queue: str,
        queue_depth: int,
        case_active: int,
        user_active: int,
        engine_active: int,
        fan_out: int,
    ) -> Tuple[bool, str]:
        """Pure cap check — data only; the admission *decision* and any
        queueing/redrive stays in ``ps05.core.admission``. Returns
        ``(admitted, reason)``."""
        cap = self.queue_capacity.get(queue)
        if cap is None or queue_depth >= cap:
            return False, f"queue_capacity[{queue!r}] reached"
        if case_active >= self.per_case_concurrency:
            return False, "per_case_concurrency reached"
        if user_active >= self.per_user_concurrency:
            return False, "per_user_concurrency reached"
        if engine_active >= self.per_engine_concurrency:
            return False, "per_engine_concurrency reached"
        if fan_out >= self.max_fan_out:
            return False, "max_fan_out reached"
        return True, "ok"


class LoadShedder:
    """Shed non-critical work (indexing, RAG projection, …) first under
    overload — § 23.10 ``RESOURCE_EXHAUSTION``: shed to a later admission
    window, never a requeue storm."""

    def __init__(self, shed_when_full: bool, non_critical_tags: set[str]) -> None:
        self.shed_when_full = shed_when_full
        self.non_critical_tags = set(non_critical_tags)

    def should_shed(self, task_tags: Iterable[str], queue_full: bool) -> bool:
        tags = set(task_tags)
        return (
            self.shed_when_full
            and queue_full
            and bool(tags & self.non_critical_tags)
        )

    def partition(
        self, tasks: Iterable[Mapping[str, Any]], queue_full: bool
    ) -> Tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        """Split tasks into ``(admitted, shed)``. A task is shed only when the
        queue is full, shedding is enabled, and the task carries at least one
        non-critical tag; everything else (including critical tasks at a full
        queue) is held for the next admission window, not dropped."""
        admitted: list[Mapping[str, Any]] = []
        shed: list[Mapping[str, Any]] = []
        for task in tasks:
            if self.should_shed(task.get("tags", ()), queue_full):
                shed.append(task)
            else:
                admitted.append(task)
        return admitted, shed


class QueueAgeTracker:
    """Per-queue wait-aging counter (RES-004): older work ages, not starves.

    Call :meth:`on_wait` once per scheduling tick for a queue that still has
    pending work; its age then feeds :func:`fairness_rank` so a low-priority
    case's backlog is promoted instead of being held behind a fresh
    high-priority burst indefinitely."""

    def __init__(self) -> None:
        self._ages: dict[str, int] = {}
        self._lock = threading.Lock()

    def on_wait(self, queue: str) -> int:
        with self._lock:
            age = self._ages.get(queue, 0) + 1
            self._ages[queue] = age
            return age

    def age(self, queue: str) -> int:
        with self._lock:
            return self._ages.get(queue, 0)

    def reset(self, queue: str) -> None:
        with self._lock:
            self._ages.pop(queue, None)

    def fairness_rank(self, queues: dict[str, int]) -> list[str]:
        """Dispatch order: most-starved (highest age) first, then least
        loaded, then name (deterministic). ``queues`` maps queue name ->
        current depth."""
        with self._lock:
            ages = dict(self._ages)
        return fairness_rank(queues, ages)


def fairness_rank(queues: dict[str, int], ages: Optional[dict[str, int]] = None) -> list[str]:
    """Fairness hook core.admission may call (RES-004 / RES-001): given the
    current depth of each queue, return dispatch order. A large video job in
    one class must not starve audio/DB work, so the deepest queue is
    dispatched *last* within a given starvation tier; starved queues
    (high age) jump ahead of depth."""
    ages = ages or {}
    return sorted(queues, key=lambda q: (-ages.get(q, 0), queues[q], q))
