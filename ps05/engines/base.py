"""Base port, finding schema and durable-task factory for PS-05 engine adapters.

TRD § 22 / § 23.15 hexagonal rule: every engine is an adapter over a shared
evidence-stream port.  Each adapter re-opens its *own* cursor / reader over the
immutable WORM object (``EvidenceStreamHandle``, TRD § 23.17 / REO-014) and
never shares file-pointer state with any other engine; no model action may
mutate evidence — adapters only ever see a bounded read surface.

Every emitted :class:`Finding` carries the full provenance record:
``model_version``, ``config_version``, ``input_artifact_hash`` and
``analysis_run_id`` (TRD § 23.15 matrix invariant + § 15.17 trust surface).
Outputs are schema-gated: a model output is a *result*, not a fact — any
adapter output that fails Pydantic validation becomes a permanent
``SCHEMA_VALIDATION_FAILURE`` (never retried), it is never silently emitted.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:  # consumed contracts — owned by Rex / Suki, not this package
    from ps05.shared.streaming import ChunkManifest, EvidenceStreamHandle

# ---------------------------------------------------------------------------
# Consumed contract: ps05.core.errors.ErrorClass (Suki).
# Fallback below is the TRD § 23.10 taxonomy, verbatim, so this package stays
# importable (and offline-testable) in a worktree where ps05.core has not
# landed yet; once ps05.core.errors exists, its enum is used instead.
# ---------------------------------------------------------------------------
try:
    from ps05.core.errors import ErrorClass  # type: ignore[assignment]
except ModuleNotFoundError:  # pragma: no cover - worktree bootstrap fallback
    class ErrorClass(str, Enum):
        INPUT_INVALID = "INPUT_INVALID"
        INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
        AUTHORIZATION_FAILURE = "AUTHORIZATION_FAILURE"
        PERMANENT_PARSER_FAILURE = "PERMANENT_PARSER_FAILURE"
        TRANSIENT_STORAGE_FAILURE = "TRANSIENT_STORAGE_FAILURE"
        DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
        BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
        DEPENDENCY_TIMEOUT = "DEPENDENCY_TIMEOUT"
        RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"
        GPU_OUT_OF_MEMORY = "GPU_OUT_OF_MEMORY"
        WORKER_LOST = "WORKER_LOST"
        MODEL_CHECKPOINT_INVALID = "MODEL_CHECKPOINT_INVALID"
        SCHEMA_VALIDATION_FAILURE = "SCHEMA_VALIDATION_FAILURE"
        CANCELLED = "CANCELLED"
        REQUIRES_EXAMINER_REVIEW = "REQUIRES_EXAMINER_REVIEW"
        UNKNOWN_INTERNAL_FAILURE = "UNKNOWN_INTERNAL_FAILURE"

# Consumed contract: ps05.core.state_machines.TaskState (Suki).  Same
# bootstrap fallback as above: the values are the TRD § 23.10 task states.
try:
    from ps05.core.state_machines import TaskState  # type: ignore[assignment]
except ModuleNotFoundError:  # pragma: no cover - worktree bootstrap fallback
    class TaskState(str, Enum):
        PENDING = "PENDING"
        CLAIMED = "CLAIMED"
        RUNNING = "RUNNING"
        COMPLETED = "COMPLETED"
        FAILED = "FAILED"
        FAILED_RETRYABLE = "FAILED_RETRYABLE"
        DEAD_LETTERED = "DEAD_LETTERED"
        CANCELLED = "CANCELLED"


# ---------------------------------------------------------------------------
# Finding output schema (emitted as Pydantic, never raw numbers)
# ---------------------------------------------------------------------------
class FindingStatus(str, Enum):
    OK = "OK"
    QUARANTINE = "QUARANTINE"        # hostile/unsupported input, inert (§ 22.12)
    DATA_GAP = "DATA_GAP"            # recorded absence, not a defect
    REQUIRES_TOOL = "REQUIRES_TOOL"  # external-tool step marker (offline-safe)


class Finding(BaseModel):
    """One emitted engine finding — the only thing an engine may 'output'.

    Frozen: a finding is an immutable record, not a working buffer.  Every
    field is a provenance field or a schema-typed result; raw model numbers
    belong in ``payload`` and are always explained by ``category``.
    """

    model_config = ConfigDict(frozen=True)

    finding_id: str
    engine_id: str
    stage_id: str
    case_id: str
    evidence_id: str
    analysis_run_id: str
    model_version: str
    config_version: str
    input_artifact_hash: str
    category: str
    confidence: float | None = None
    payload: Mapping[str, Any] = Field(default_factory=dict)
    status: FindingStatus = FindingStatus.OK


# ---------------------------------------------------------------------------
# Typed engine execution errors (TRD § 23.10 classes, mapped by the task factory)
# ---------------------------------------------------------------------------
class EngineExecutionError(Exception):
    """Base for adapter failures that carry an :class:`ErrorClass` mapping.

    The task factory maps a raised instance to ``error_class`` and decides
    retry from the descriptor's ``retry_error_classes`` — permanent classes
    (``INPUT_INVALID``, ``PERMANENT_PARSER_FAILURE``, ``INTEGRITY_FAILURE``,
    …) are *never* retried.
    """

    default_error_class: ClassVar[ErrorClass] = ErrorClass.UNKNOWN_INTERNAL_FAILURE

    def __init__(self, message: str, *, error_class: ErrorClass | None = None) -> None:
        super().__init__(message)
        self.error_class = error_class if error_class is not None else type(self).default_error_class


class InputInvalidError(EngineExecutionError):
    """Corrupt, unsupported or structurally hostile input — never retried."""

    default_error_class = ErrorClass.INPUT_INVALID


class PermanentParserFailure(EngineExecutionError):
    """Malformed / truncated container with no plausible parse — a data
    condition, not a transient failure; never retried (TRD § 23.10)."""

    default_error_class = ErrorClass.PERMANENT_PARSER_FAILURE


class TransientStorageFailure(EngineExecutionError):
    """Object-store / WORM connectivity blip — retriable under § 23.10 policy."""

    default_error_class = ErrorClass.TRANSIENT_STORAGE_FAILURE


class IntegrityFailure(EngineExecutionError):
    """Hash / WORM readback mismatch — permanent, hard-blocks dependents."""

    default_error_class = ErrorClass.INTEGRITY_FAILURE


class ForensicTaskError(Exception):
    """Raised by the durable task factory when an adapter pass fails.

    Carries the § 23.10 ``error_class`` and whether *this task* is retriable
    (``retryable = error_class in descriptor.retry_error_classes`` — permanent
    classes therefore carry ``retryable=False`` and route to the DLQ, never to
    an auto-retry).
    """

    def __init__(
        self,
        *,
        engine_id: str,
        stage_id: str,
        error_class: ErrorClass,
        retryable: bool,
        checkpoint: str,
        attempt: int,
        message: str,
    ) -> None:
        super().__init__(
            f"{engine_id}/{stage_id}: {message} "
            f"[error_class={error_class.value}, retryable={retryable}]"
        )
        self.engine_id = engine_id
        self.stage_id = stage_id
        self.error_class = error_class
        self.retryable = retryable
        self.checkpoint = checkpoint
        self.attempt = attempt
        self.message = message


# ---------------------------------------------------------------------------
# Run context + task record (the celery glue passes both into EngineTask.run)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RunContext:
    """Per-run identity for provenance.  Immutable: adapters may read, never write."""

    case_id: str
    evidence_id: str
    analysis_run_id: str
    input_artifact_hash: str  # SHA-256 of the WORM object (TRD § 19 / CEIPL)
    evidence_stream_uri: str = ""
    declared_signature: str | None = None  # intake-declared container signature, if any


@dataclass
class TaskRecord:
    """Mutable per-task bookkeeping the factory fills in as it progresses.

    The celery glue owns the record; the factory *sets* these fields (status
    transitions, error class, checkpoint) and never deletes or resets them.
    """

    task_id: str
    engine_id: str = ""
    stage_id: str = ""
    status: TaskState | None = None
    error_class: ErrorClass | None = None
    checkpoint_id: str | None = None
    attempts: int = 0
    last_error: str | None = None


# ---------------------------------------------------------------------------
# BaseAdapter — the hexagonal port every engine implements
# ---------------------------------------------------------------------------
class BaseAdapter(ABC):
    """Hexagonal adapter over the shared evidence-stream port (TRD § 23.15).

    Subclasses MUST override ``run``.  Invariant: each adapter re-opens its own
    reader / cursor over the immutable WORM object via
    :meth:`open_reader` (TRD § 23.17 / REO-014) — there is no shared
    file-pointer state across the scanner→adapter boundary, and no read is
    unbounded.  Adapters emit :class:`Finding` instances, not raw numbers.
    """

    engine_id: ClassVar[str]
    stage_id: ClassVar[str]

    def __init__(self, model_version: str, config_version: str) -> None:
        self.model_version = model_version
        self.config_version = config_version

    @abstractmethod
    def run(self, handle: "EvidenceStreamHandle", ctx: RunContext) -> list[Finding]:
        """Execute this engine's owned raw-execution pass and return findings.

        ``handle`` is the re-openable evidence-stream port.  Each call to
        :meth:`open_reader` inside ``run`` gives the adapter its *own*
        independent cursor; no other engine or pass sees or mutates it.
        """
        ...

    # -- provenance helper ------------------------------------------------
    def make_finding(
        self,
        ctx: RunContext,
        category: str,
        payload: Mapping[str, Any] | None = None,
        *,
        confidence: float | None = None,
        status: FindingStatus = FindingStatus.OK,
    ) -> Finding:
        """Build a schema-complete :class:`Finding` with full provenance.

        All provenance fields are filled from ``ctx`` and this adapter's
        versions so every emitted finding records ``model_version``,
        ``config_version``, ``input_artifact_hash`` and ``analysis_run_id``.
        """
        return Finding(
            finding_id=f"fd_{uuid.uuid4().hex}",
            engine_id=self.engine_id,
            stage_id=self.stage_id,
            case_id=ctx.case_id,
            evidence_id=str(ctx.evidence_id),
            analysis_run_id=ctx.analysis_run_id,
            model_version=self.model_version,
            config_version=self.config_version,
            input_artifact_hash=ctx.input_artifact_hash,
            category=category,
            confidence=confidence,
            payload=dict(payload or {}),
            status=status,
        )

    # -- read-surface helpers (re-open invariant) -------------------------
    @staticmethod
    def open_reader(handle: "EvidenceStreamHandle") -> Any:
        """Open *this adapter's own* independent reader (TRD § 23.17).

        ``EvidenceStreamHandle.open_reader()`` returns a file-like, bounded
        read surface over the immutable WORM object.  Each call yields a
        distinct reader: two calls never share pointer state, which is what
        lets CCHDF run its triage and modality passes over independently
        re-opened readers (REO-014 acceptance: two distinct instances).
        """
        return handle.open_reader()

    @staticmethod
    def read_prefix(handle: "EvidenceStreamHandle", limit: int) -> tuple[bytes, list[str]]:
        """Bounded read of the first ``limit`` bytes via a fresh reader.

        Returns ``(bytes, chunk_ids)``; ``chunk_ids`` is the deterministic
        chunk-ID span the read covered (``handle.chunk_ids_in_range``),
        keyed to the handle's ``ChunkManifest`` so every read is digest-
        verifiable (TRD § 23.8).  Never reads past ``limit`` bytes.
        """
        reader = BaseAdapter.open_reader(handle)
        try:
            data = reader.read(limit)
        finally:
            reader.close()
        chunk_ids: list[str] = []
        chunk_ids_in_range = getattr(handle, "chunk_ids_in_range", None)
        if callable(chunk_ids_in_range):
            chunk_ids = list(chunk_ids_in_range(0, len(data)))
        return data, chunk_ids


def open_reader(handle: "EvidenceStreamHandle") -> Any:
    """Module-level alias of :meth:`BaseAdapter.open_reader` (independent cursor)."""
    return BaseAdapter.open_reader(handle)


def read_prefix(
    handle: "EvidenceStreamHandle", limit: int
) -> tuple[bytes, list[str]]:
    """Module-level alias of :meth:`BaseAdapter.read_prefix` (bounded head read)."""
    return BaseAdapter.read_prefix(handle, limit)


@dataclass
class ReadSurface:
    """Borrowed reader + the handle that produced it.

    Use as ``with read_scanned(handle) as surface:`` — the reader is
    always closed on exit (TRD § 23.17: each cursor closes its own
    reader and never touches another pass's reader).
    """

    reader: Any
    handle: Any

    def __enter__(self) -> "ReadSurface":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.reader.close()


def read_scanned(handle: "EvidenceStreamHandle") -> ReadSurface:
    """Context-managed independent reader over ``handle``."""
    return ReadSurface(reader=handle.open_reader(), handle=handle)


def get_read_surface(handle: "EvidenceStreamHandle") -> Any:
    """The raw independent reader itself, for callers that manage close()."""
    return handle.open_reader()


# ---------------------------------------------------------------------------
# Durable task factory (the celery glue registers task_* from each adapter)
# ---------------------------------------------------------------------------
HandleResolver = Callable[[str, RunContext], "EvidenceStreamHandle"]
FindingSink = Callable[[list[Finding], RunContext], None]
CheckpointSink = Callable[[str, TaskRecord, RunContext], None]


class EngineTask:
    """A durable workflow task (TRD § 23.3 / REO-015) for one engine pass.

    This is the object the celery glue registers as ``task_<engine_id>``.
    It is *not* an unbounded inline function: it re-opens the handle,
    checkpoints the stage, maps failures to § 23.10 error classes and raises
    :class:`ForensicTaskError` with the correct retry posture.
    """

    def __init__(
        self,
        name: str,
        desc: Any,  # EngineDescriptor (registry) — any object with the § 23.15 fields
        adapter: BaseAdapter,
        handle_resolver: HandleResolver | None = None,
        finding_sink: FindingSink | None = None,
        checkpoint_sink: CheckpointSink | None = None,
    ) -> None:
        self.name = name
        self.desc = desc
        self.adapter = adapter
        self.handle_resolver = handle_resolver
        self.finding_sink = finding_sink
        self.checkpoint_sink = checkpoint_sink

    # -- celery-glue surface ----------------------------------------------
    @property
    def celery_task_name(self) -> str:
        """The ``task_<engine_id>`` name the app's celery glue registers."""
        return self.name

    # -- execution ---------------------------------------------------------
    def run(
        self,
        evidence_stream: Any,
        ctx: RunContext,
        task_record: TaskRecord,
    ) -> list[Finding]:
        """Execute one owned engine pass durably.

        ``evidence_stream`` is either a pre-built ``EvidenceStreamHandle``
        (in-process / offline testing) or the URI string the glue's
        ``handle_resolver`` turns into one.  A handle is *re-opened* here —
        the factory does not carry file-pointer state in from a previous
        pass.
        """
        handle = self._handle_for(evidence_stream, ctx)
        task_record.engine_id = self.desc.engine_id
        task_record.stage_id = self.desc.stage_id
        task_record.status = TaskState.RUNNING
        task_record.attempts += 1

        try:
            raw_findings = self.adapter.run(handle, ctx)
        except EngineExecutionError as exc:
            self._fail(ctx, task_record, exc)
            raise  # unreachable — _fail raises ForensicTaskError
        except Exception as exc:  # unclassified → bounded UNKNOWN_INTERNAL_FAILURE
            self._fail(ctx, task_record, EngineExecutionError(
                f"unclassified failure in {self.desc.engine_id}: {exc!r}",
            ))
            raise  # unreachable

        # Output-schema gate: a model output is a result, not a fact.
        # A finding that fails validation is a permanent engineering defect
        # (SCHEMA_VALIDATION_FAILURE, never retried), not a silent pass.
        findings: list[Finding] = []
        for item in raw_findings:
            try:
                finding = item if isinstance(item, Finding) else Finding.model_validate(item)
            except ValidationError as exc:
                self._fail(ctx, task_record, EngineExecutionError(
                    f"finding failed output-schema gate: {exc}",
                    error_class=ErrorClass.SCHEMA_VALIDATION_FAILURE,
                ))
                raise  # unreachable
            findings.append(finding)

        # Checkpoint the stage (TRD § 23.7) before the task can be re-queued
        # after a worker-loss event (§ 23.6.4).
        if self.checkpoint_sink is not None:
            self.checkpoint_sink(self.desc.checkpoint, task_record, ctx)
        task_record.checkpoint_id = self.desc.checkpoint
        if self.finding_sink is not None:
            self.finding_sink(findings, ctx)
        task_record.status = TaskState.COMPLETED
        task_record.error_class = None
        task_record.last_error = None
        return findings

    # -- internals ----------------------------------------------------------
    def _handle_for(self, evidence_stream: Any, ctx: RunContext) -> Any:
        if hasattr(evidence_stream, "open_reader"):
            return evidence_stream  # pre-built handle (in-process / test)
        if self.handle_resolver is None:
            self._fail(ctx, TaskRecord(task_id="?"), EngineExecutionError(
                "no handle resolver supplied; the celery glue must provide one "
                "that builds an EvidenceStreamHandle over the WORM object "
                "identified by evidence_stream_uri",
            ))
            raise  # unreachable
        return self.handle_resolver(evidence_stream, ctx)

    def _fail(self, ctx: RunContext, task_record: TaskRecord, exc: EngineExecutionError) -> None:
        error_class = exc.error_class
        retryable = error_class in tuple(self.desc.retry_error_classes)
        task_record.error_class = error_class
        task_record.last_error = str(exc)
        task_record.status = TaskState.FAILED_RETRYABLE if retryable else TaskState.FAILED
        raise ForensicTaskError(
            engine_id=self.desc.engine_id,
            stage_id=self.desc.stage_id,
            error_class=error_class,
            retryable=retryable,
            checkpoint=self.desc.checkpoint,
            attempt=task_record.attempts,
            message=str(exc),
        ) from exc


def make_task(
    desc: Any,
    adapter: BaseAdapter,
    *,
    task_name: str | None = None,
    handle_resolver: HandleResolver | None = None,
    finding_sink: FindingSink | None = None,
    checkpoint_sink: CheckpointSink | None = None,
) -> EngineTask:
    """Build the durable task for one engine's owned pass.

    ``task_name`` defaults to ``task_<engine_id-lower>``; the celery glue
    (owned by the app, not this package) registers that name on its celery
    app and calls ``EngineTask.run(evidence_stream, ctx, task_record)``.
    """
    name = task_name or f"task_{desc.engine_id.lower()}"
    return EngineTask(
        name=name,
        desc=desc,
        adapter=adapter,
        handle_resolver=handle_resolver,
        finding_sink=finding_sink,
        checkpoint_sink=checkpoint_sink,
    )
