"""Engine descriptor + registry with the TRD § 23.15 queue-routing table.

The registry is the single owner of the raw-execution matrix (TRD § 23.15):
one ``EngineDescriptor`` per engine, registered against exactly one
``BaseAdapter`` implementation.  The matrix invariant — *no two engines own
the same raw-execution responsibility for one input modality* (REO-013) — is
enforced structurally: each ``(stage_id, queue_name)`` routing row belongs to
a single engine and registration is keyed by a globally-unique ``engine_id``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Mapping, Sequence

if TYPE_CHECKING:
    from .base import BaseAdapter


class ExecType(str, Enum):
    """Execution-class tag for the § 23.15 engine matrix rows."""

    CPU = "cpu"
    GPU = "gpu"
    IO = "io"
    PARSER = "parser"


@dataclass(frozen=True)
class EngineDescriptor:
    """One row of the TRD § 23.15 canonical engine execution matrix.

    The descriptor is the single source of truth for that engine's queue,
    workload class, retry classes, checkpoint, model/config versions and
    resource-profile reference.  It is frozen: runtime code may read it,
    never mutate it.
    """

    engine_id: str
    stage_id: str
    exec_type: ExecType
    queue_name: str
    workload_class: str
    profile_ref: str
    timeout_s: int
    retry_error_classes: tuple[str, ...]
    checkpoint: str
    model_version: str
    config_version: str


# ---------------------------------------------------------------------------
# TRD § 23.15 queue routing — workload_class -> queue name
# (the matrix row's `queue routing` column, made explicit as a table so the
# celery glue can build queue/routing config from one owner).
# ---------------------------------------------------------------------------
QUEUE_ROUTING: Mapping[str, tuple[str, ...]] = {
    "audio_heavy": ("AUDIO_DSP",),
    "video_triage": ("DEFENSIVE_PARSER_SANDBOX",),
    "network_dissection": ("DEFENSIVE_PARSER_SANDBOX", "NETWORK_FILESYSTEM_FORENSIC"),
    "metadata_snap": ("CPU_FORENSIC_HEAVY",),
    "eiv_integrity": ("DEFENSIVE_PARSER_SANDBOX", "CPU_FORENSIC_HEAVY"),
    "cchdf_triage": ("DEFENSIVE_PARSER_SANDBOX",),
}

# Multi-queue view (subset of QUEUE_ROUTING with more than one queue, i.e.
# the network_dissection and eiv_integrity rows of the § 23.15 matrix):
MULTI_QUEUE_ROUTING: Mapping[str, tuple[str, ...]] = {
    workload_class: queues
    for workload_class, queues in QUEUE_ROUTING.items()
    if len(queues) > 1
}


def route_queues(workload_class: str) -> tuple[str, ...]:
    """Queue names for a ``workload_class`` per TRD § 23.15."""
    return QUEUE_ROUTING.get(workload_class, ())


# ---------------------------------------------------------------------------
# Canonical § 23.15 descriptors for the engines this pass implements.
# ---------------------------------------------------------------------------
# § 23.10 error-class names used in `retry_error_classes` (the TRD § 23.10
# taxonomy strings; the task factory maps to the ps05.core.errors enum).
# ``TRANSIENT_STORAGE_FAILURE`` is the engine's own retriable failure class
# per its matrix row; permanent classes are *excluded* by the factory's
# retry posture, so listing only the retriable class here is the norm.
_TRANSIENT = ("TRANSIENT_STORAGE_FAILURE",)

CANONICAL_DESCRIPTORS: tuple[EngineDescriptor, ...] = (
    EngineDescriptor(
        engine_id="triage",
        stage_id="universal_triage",
        exec_type=ExecType.PARSER,
        queue_name="DEFENSIVE_PARSER_SANDBOX",
        workload_class="cchdf_triage",
        profile_ref="RES_DEFENSIVE_PARSER_SANDBOX",
        timeout_s=60,
        retry_error_classes=_TRANSIENT,
        checkpoint="cp_container_metadata",
        model_version="TRIAGE_v1",
        config_version="TRIAGE_v1",
    ),
    EngineDescriptor(
        engine_id="cmms",
        stage_id="metadata_snap",
        exec_type=ExecType.CPU,
        queue_name="CPU_FORENSIC_HEAVY",
        workload_class="metadata_snap",
        profile_ref="RES_CPU_FORENSIC_HEAVY",
        timeout_s=300,
        retry_error_classes=_TRANSIENT,
        checkpoint="cp_container_metadata",
        model_version="CMMS_v1",
        config_version="CMMS_v1",
    ),
    EngineDescriptor(
        engine_id="ontf",
        stage_id="network_dissection",
        exec_type=ExecType.CPU,
        queue_name="DEFENSIVE_PARSER_SANDBOX",
        workload_class="network_dissection",
        profile_ref="RES_DEFENSIVE_PARSER_SANDBOX",
        timeout_s=600,
        retry_error_classes=_TRANSIENT,
        checkpoint="cp_pcap_dissection_batch",
        model_version="ONTF_v1",
        config_version="ONTF_v1",
    ),
    EngineDescriptor(
        engine_id="cchdf",
        stage_id="cchdf_triage",
        exec_type=ExecType.CPU,
        queue_name="DEFENSIVE_PARSER_SANDBOX",
        workload_class="cchdf_triage",
        profile_ref="RES_DEFENSIVE_PARSER_SANDBOX",
        timeout_s=900,
        # Matrix row: INPUT_INVALID and PERMANENT_PARSER_FAILURE are
        # never-retried classes; TRANSIENT_STORAGE_FAILURE is the one
        # retriable failure class of the CCHDF/PSEA row.
        retry_error_classes=_TRANSIENT,
        checkpoint="cp_container_metadata",
        model_version="CCHDF_v1",
        config_version="CCHDF_v1",
    ),
)

_BY_ENGINE_ID: Mapping[str, EngineDescriptor] = {d.engine_id: d for d in CANONICAL_DESCRIPTORS}

# Named descriptor accessors (adapters import these instead of re-deriving):
TRIAGE_DESCRIPTOR = _BY_ENGINE_ID["triage"]
CMMS_DESCRIPTOR = _BY_ENGINE_ID["cmms"]
ONTF_DESCRIPTOR = _BY_ENGINE_ID["ontf"]
CCHDF_DESCRIPTOR = _BY_ENGINE_ID["cchdf"]


def descriptor_for_engine(engine_id: str) -> EngineDescriptor:
    """The canonical § 23.15 descriptor for a known engine id."""
    try:
        return _BY_ENGINE_ID[engine_id]
    except KeyError:
        raise KeyError(f"no canonical descriptor registered for engine_id={engine_id!r}") from None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EngineBinding:
    """A registered engine: descriptor + its sole adapter implementation."""

    descriptor: EngineDescriptor
    adapter: "BaseAdapter"


class EngineRegistrationError(Exception):
    """Raised on duplicate-engine or duplicate-modality registration."""


class EngineRegistry:
    """Owns the engine ↔ adapter map (TRD § 22.1 in-process API reference).

    Invariants enforced here:

    * one ``engine_id`` maps to exactly one adapter (raw-executor ownership —
      REO-013: a later engine consumes an earlier engine's findings, it never
      re-registers a second executor for the same modality);
    * two engines may not claim the same ``(stage_id, queue_name)`` routing
      row — the § 23.15 matrix is one-owner-per-row;
    * lookup is O(1) by ``engine_id``.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, EngineBinding] = {}
        self._routing_rows: set[tuple[str, str]] = set()

    # -- registration ------------------------------------------------------
    def register(self, desc: EngineDescriptor, adapter: "BaseAdapter") -> EngineBinding:
        if desc.engine_id in self._bindings:
            raise EngineRegistrationError(
                f"engine_id={desc.engine_id!r} already registered; raw-execution "
                f"responsibility may not have two executors (TRD § 23.15 REO-013)"
            )
        row = (desc.stage_id, desc.queue_name)
        if row in self._routing_rows:
            raise EngineRegistrationError(
                f"routing row {row} is already owned by another engine; "
                f"two engines may not own the same modality raw-executor (TRD § 23.15)"
            )
        self._routing_rows.add(row)
        binding = EngineBinding(descriptor=desc, adapter=adapter)
        self._bindings[desc.engine_id] = binding
        return binding

    # -- lookup ------------------------------------------------------------
    def get(self, engine_id: str) -> EngineBinding:
        try:
            return self._bindings[engine_id]
        except KeyError:
            raise EngineRegistrationError(
                f"engine_id={engine_id!r} is not registered"
            ) from None

    def engine_ids(self) -> tuple[str, ...]:
        return tuple(self._bindings)

    def __len__(self) -> int:
        return len(self._bindings)


# ---------------------------------------------------------------------------
# Default registry: the canonical descriptors + the adapters this pass ships.
# ---------------------------------------------------------------------------
def build_default_registry() -> EngineRegistry:
    """The out-of-box registry with every canonical engine registered.

    The celery glue (app-owned) additionally registers ``task_<engine_id>``
    from each binding's adapter via ``ps05.engines.base.make_task``.
    """
    from .cchdf import CCHDFOrchestrator
    from .cmms import CMMSAdapter
    from .ontf import ONTFAdapter
    from .triage import TriageAdapter

    registry = EngineRegistry()
    for desc in CANONICAL_DESCRIPTORS:
        if desc.engine_id == "cchdf":
            adapter: BaseAdapter = CCHDFOrchestrator()
        elif desc.engine_id == "triage":
            adapter = TriageAdapter()
        elif desc.engine_id == "cmms":
            adapter = CMMSAdapter()
        elif desc.engine_id == "ontf":
            adapter = ONTFAdapter()
        else:  # pragma: no cover - exhaustiveness guard for future canonical rows
            raise EngineRegistrationError(f"no adapter bound for {desc.engine_id!r}")
        registry.register(desc, adapter)
    return registry
