"""PS-05 engine adapters — hexagonal port over the durable execution surface.

Every engine in this package sits behind the TRD § 22 / § 23.15 canonical
engine matrix: a hexagonal adapter over a shared evidence-stream port.  Each
raw-execution responsibility is owned by exactly one engine (TRD § 23.15
matrix invariant, REO-013); later engines that need an earlier engine's raw
output consume the emitted Pydantic findings / data contract, never a
duplicated executor.

Contracts owned here:

* :mod:`ps05.engines.registry` — ``EngineDescriptor`` / ``EngineRegistry`` and
  the TRD § 23.15 queue-routing table.
* :mod:`ps05.engines.base` — ``BaseAdapter``, ``Finding``, ``RunContext``,
  ``make_task`` (the durable-task factory the celery glue registers as
  ``task_*``).
* Modality engines: :mod:`ps05.engines.triage` (universal 10-step triage),
  :mod:`ps05.engines.cmms` (container-level metadata snapshots),
  :mod:`ps05.engines.ontf` (defensive PCAP/PCAPNG dissection),
  :mod:`ps05.engines.cchdf` (CCHDF orchestrator over independently re-opened
  readers, TRD § 23.17 / REO-014).

Consumed (not owned) contracts:

* ``ps05.shared.streaming.EvidenceStreamHandle`` / ``ChunkManifest`` (Rex).
* ``ps05.core.errors.ErrorClass`` / ``ps05.core.state_machines.TaskState``
  (Suki) — imported with a documented local fallback so this package is
  testable before those packages land.

This pass is schema-only: no ML inference runs; models are stubs that emit
schema-conformant outputs.  No benchmark numbers, AUCs or improvement
claims appear anywhere here.
"""

from .base import (
    BaseAdapter,
    EngineExecutionError,
    ForensicTaskError,
    Finding,
    FindingStatus,
    RunContext,
    get_read_surface,
    make_task,
    open_reader,
    read_prefix,
    read_scanned,
)
from .cchdf import CCHDFOrchestrator, CCHDF_DESCRIPTOR, build_cchdf_stage_task
from .cmms import CMMSAdapter, CMMS_DESCRIPTOR
from .ontf import ONTFAdapter, ONTF_DESCRIPTOR
from .registry import (
    CANONICAL_DESCRIPTORS,
    EngineBinding,
    EngineDescriptor,
    EngineRegistrationError,
    EngineRegistry,
    ExecType,
    QUEUE_ROUTING,
    build_default_registry,
    route_queues,
)
from .triage import TriageAdapter, TRIAGE_DESCRIPTOR

__all__ = [
    "BaseAdapter",
    "CCHDFOrchestrator",
    "CCHDF_DESCRIPTOR",
    "CANONICAL_DESCRIPTORS",
    "CMMSAdapter",
    "CMMS_DESCRIPTOR",
    "EngineBinding",
    "EngineDescriptor",
    "EngineExecutionError",
    "EngineRegistrationError",
    "EngineRegistry",
    "ExecType",
    "Finding",
    "FindingStatus",
    "ForensicTaskError",
    "ONTFAdapter",
    "ONTF_DESCRIPTOR",
    "RunContext",
    "TRIAGE_DESCRIPTOR",
    "TriageAdapter",
    "QUEUE_ROUTING",
    "build_cchdf_stage_task",
    "build_default_registry",
    "get_read_surface",
    "make_task",
    "open_reader",
    "read_prefix",
    "read_scanned",
    "route_queues",
]
