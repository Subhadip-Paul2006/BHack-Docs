"""OpenTelemetry instrumentation, execution context and SLO targets (OBS-001…OBS-013).

OpenTelemetry is the vendor-neutral framework (OBS-001) but is *optional* at
import time: everything in this module degrades to no-op tracing / plain
float counting when the ``opentelemetry`` packages are not installed, so the
package still imports in minimal test environments.

Redaction invariant (OBS-013): spans, metrics and context objects carry only
references (IDs, digests, status) — never payload bytes, keys, embeddings or
case content.
"""

from __future__ import annotations

import functools
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, TypeVar

__all__ = [
    "ExecutionContext",
    "tracer",
    "meter",
    "bind_context",
    "instrument_task",
    "METRIC_GROUPS",
    "SLO_TARGETS",
    "TARGET_UNBASELINED",
]

_LOG = logging.getLogger("ps05.infra.observability")
_T = TypeVar("_T")

# ---------------------------------------------------------------------------
# Optional OpenTelemetry access (guarded — module works without otel installed)
# ---------------------------------------------------------------------------

_OTEL_TRACE_AVAILABLE = True
_OTEL_METRICS_AVAILABLE = True
_tracer = None
_meter = None
_tracer_lock = threading.Lock()


def _get_otel_tracer():
    global _tracer, _OTEL_TRACE_AVAILABLE
    with _tracer_lock:
        if _tracer is None and _OTEL_TRACE_AVAILABLE:
            try:
                from opentelemetry import trace  # type: ignore[import-not-found]

                _tracer = trace.get_tracer("ps05", "0.1.0")
            except Exception:  # pragma: no cover - otel not installed / broken
                _OTEL_TRACE_AVAILABLE = False
                _LOG.debug("opentelemetry.trace unavailable; tracing degraded to no-op")
        return _tracer


def _get_otel_meter():
    global _meter, _OTEL_METRICS_AVAILABLE
    with _tracer_lock:
        if _meter is None and _OTEL_METRICS_AVAILABLE:
            try:
                from opentelemetry import metrics  # type: ignore[import-not-found]

                _meter = metrics.get_meter("ps05", "0.1.0")
            except Exception:  # pragma: no cover - otel not installed / broken
                _OTEL_METRICS_AVAILABLE = False
                _LOG.debug("opentelemetry.metrics unavailable; metering degraded to no-op")
        return _meter


def tracer():
    """Lazily obtained OpenTelemetry tracer (OBS-001). No-op fallback tracer
    object when otel is absent: spans are simply not recorded."""
    t = _get_otel_tracer()
    if t is not None:
        return t
    return _NoopTracer()


def meter():
    """Lazily obtained OpenTelemetry meter. Returns ``None`` when otel is
    unavailable so callers can guard; prefer :func:`count_metric` helpers
    that already no-op."""
    return _get_otel_meter()


class _NoopSpan:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_attributes(self, attrs: Dict[str, Any]) -> None:
        return None

    def record_exception(self, *a, **kw) -> None:
        return None

    def end(self) -> None:
        return None

    def get_span_context(self):
        return _NoopSpanContext()


class _NoopSpanContext:
    trace_id = 0
    span_id = 0
    is_valid = False


class _NoopTracer:
    def start_as_current_span(self, *a, **kw):
        return _NoopSpan()

    def start_span(self, *a, **kw) -> _NoopSpan:
        return _NoopSpan()


# ---------------------------------------------------------------------------
# Execution context (OBS-002)
# ---------------------------------------------------------------------------


@dataclass
class ExecutionContext:
    """The one context propagated unchanged across
    API -> DB -> outbox -> broker -> Celery task -> child task -> engine
    adapter -> model-inference batch -> finding write -> indexing -> report
    compilation. No engine may start a fresh trace inside an already-traced
    task: re-bind instead of re-create."""

    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    correlation_id: Optional[str] = None
    analysis_run_id: Optional[str] = None
    case_id: Optional[str] = None
    evidence_id: Optional[str] = None
    job_id: Optional[str] = None
    task_id: Optional[str] = None
    worker_id: Optional[str] = None
    engine_id: Optional[str] = None
    model_checkpoint_hash: Optional[str] = None  # digest reference only, never the checkpoint bytes
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_log_fields(self) -> Dict[str, Any]:
        """Subset safe for structured logs (OBS-003/OBS-013): IDs and digests
        only — no payloads, no keys, no embeddings."""
        return {k: v for k, v in self.__dict__.items() if k != "extra" and v is not None}

    def to_span_attributes(self) -> Dict[str, Any]:
        return {
            "ps05.trace_id": self.trace_id or "",
            "ps05.correlation_id": self.correlation_id or "",
            "ps05.analysis_run_id": self.analysis_run_id or "",
            "ps05.case_id": self.case_id or "",
            "ps05.evidence_id": self.evidence_id or "",
            "ps05.job_id": self.job_id or "",
            "ps05.task_id": self.task_id or "",
            "ps05.worker_id": self.worker_id or "",
            "ps05.engine_id": self.engine_id or "",
            "ps05.model_checkpoint_hash": self.model_checkpoint_hash or "",
        }

    def child(self, **overrides: Any) -> "ExecutionContext":
        """New context for a downstream hop that inherits the parent's trace
        identity (trace_id/correlation_id never change) but may narrow or
        add hop-local IDs (task_id, evidence_id, …)."""
        base = {k: v for k, v in self.__dict__.items() if k != "extra"}
        base.update({k: v for k, v in overrides.items() if v is not None})
        return ExecutionContext(**base)

    def as_span_attributes(self) -> Dict[str, Any]:
        return self.to_span_attributes()


_CONTEXT_THREAD: threading.local = threading.local()


def bind_context(
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    analysis_run_id: Optional[str] = None,
    evidence_id: Optional[str] = None,
    job_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    engine_id: Optional[str] = None,
    model_checkpoint_hash: Optional[str] = None,
    **ids: Any,
) -> ExecutionContext:
    """Build the execution context and bind it to the current thread.

    Extra IDs (``case_id=..., task_id=...``) pass through via ``**ids``.
    """
    ctx = ExecutionContext(
        trace_id=trace_id,
        span_id=span_id,
        correlation_id=correlation_id,
        analysis_run_id=analysis_run_id,
        evidence_id=evidence_id,
        job_id=job_id,
        worker_id=worker_id,
        engine_id=engine_id,
        model_checkpoint_hash=model_checkpoint_hash,
        **ids,
    )
    _CONTEXT_THREAD.ctx = ctx
    return ctx


def current_context() -> ExecutionContext:
    """The context bound on this thread, or an empty one. An empty context is
    the signal to propagate nothing, not to invent a new trace (OBS-002)."""
    return getattr(_CONTEXT_THREAD, "ctx", ExecutionContext())


# ---------------------------------------------------------------------------
# Task instrumentation
# ---------------------------------------------------------------------------


def instrument_task(name: str, executor: Optional[Callable] = None) -> Any:
    """Decorator that opens a span for one task execution and propagates the
    thread-bound :class:`ExecutionContext` into span attributes.

    Usage (module level)::

        instrument_task("video_avsync.infer")(my_function)
        # or
        @instrument_task("video_avsync.infer")
        def my_function(...): ...

    The executor argument is accepted (and used when given) so the same
    symbol covers both the ``(name, executor)`` factory form and the plain
    ``(name)`` form — convenient for Celery task registration sites."""
    if executor is not None:
        return _make_instrumented(name, executor)

    def decorate(fn: Callable) -> Any:
        return _make_instrumented(name, fn)

    return decorate


def _make_instrumented(name: str, fn: Callable) -> Any:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ctx = current_context()
        span = tracer().start_as_current_span(f"ps05.task.{name}")
        try:
            if hasattr(span, "set_attributes") and not isinstance(span, _NoopSpan):
                span.set_attributes(ctx.to_span_attributes())
            return fn(*args, **kwargs)
        finally:
            try:
                span.end()
            except Exception:  # pragma: no cover
                pass

    wrapper.instrumented_name = name  # type: ignore[attr-defined]
    return wrapper


# ---------------------------------------------------------------------------
# Metric groups (OBS-004)
# ---------------------------------------------------------------------------

#: Canonical metric names grouped by the § 23.13 OBS-004 table. Names are the
#: single source of truth ops dashboards/alerting reference; the groups are
#: documented so no ad-hoc near-duplicate metric names creep in.
METRIC_GROUPS: Dict[str, Dict[str, str]] = {
    "api": {
        "api_request_latency_seconds": "API request latency (histogram, p50/p95/p99)",
        "api_error_rate": "API error rate",
        "api_concurrent_requests": "Concurrent API request count",
        "api_upload_throughput_bytes_per_s": "Upload throughput",
    },
    "workflow": {
        "workflow_queued_job_count": "Queued job count",
        "workflow_queue_wait_time_seconds": "Queue wait time",
        "workflow_job_execution_time_seconds": "Job execution time",
        "workflow_completion_rate": "Completion rate",
        "workflow_retry_count": "Retry count",
        "workflow_dead_letter_count": "DLQ count",
        "workflow_stale_lease_count": "Stale-lease count",
        "workflow_partial_success_rate": "PARTIAL_SUCCESS rate",
    },
    "worker": {
        "worker_availability": "Availability per class",
        "worker_cpu_percent": "CPU %",
        "worker_ram_percent": "Host RAM %",
        "worker_gpu_utilization_percent": "GPU utilisation % per device",
        "worker_vram_used_bytes": "VRAM used/total per device",
        "worker_gpu_oom_count": "GPU OOM events per device (feeds gpu_oom_rate_per_device SLO)",
        "worker_disk_scratch_free_bytes": "Disk scratch free",
        "worker_process_crash_count": "Process crash count",
        "worker_task_timeout_count": "Task-timeout count",
    },
    "db_broker": {
        "db_pool_saturation": "Pool saturation",
        "db_txn_failures": "Transaction failure rate",
        "db_lock_waits": "Lock-wait count",
        "db_deadlocks": "Deadlock count",
        "broker_availability": "Broker up/down",
        "broker_queue_lag": "Queue lag",
        "broker_event_delivery_lag_seconds": "platform_outbox PENDING age (OBS-010)",
    },
    "pipeline": {
        "pipeline_evidence_throughput": "Exhibits per hour",
        "pipeline_chunk_completion_rate": "Chunk completion rate",
        "pipeline_parser_failure_rate": "Parser failure rate",
        "pipeline_model_inference_failure_rate": "Model-inference failure rate",
        "pipeline_findings_persisted_vs_generated": "Persisted-vs-generated divergence (OBS-012)",
        "pipeline_index_sync_lag_seconds": "Index-sync lag (OBS-012)",
        "pipeline_worm_readback_failures": "WORM readback verification failures (OBS-012)",
    },
}


def all_metric_names() -> list[str]:
    names = []
    for group in METRIC_GROUPS.values():
        names.extend(group)
    return names


# ---------------------------------------------------------------------------
# SLO targets (OBS-005 / OBS-011)
# ---------------------------------------------------------------------------

#: Sentinel for any SLO line whose baseline benchmark (§ 13) has not yet been
#: run. Every dashboard line must render as ``TARGET_UNBASELINED`` until its
#: target is stamped with a baseline-run version.
TARGET_UNBASELINED = "TARGET_UNBASELINED"

#: SLO candidates. **TARGETS PENDING BASELINE BENCHMARKS** — none of these
#: values is a guaranteed number; each must be baselined by a § 13 benchmark
#: run and re-stamped with that run's version before it is treated as an
#: enforced SLO. Until then they render as ``TARGET_UNBASELINED`` (OBS-011).
SLO_TARGETS: Dict[str, Dict[str, Any]] = {
    "api_p95_latency_seconds": {
        "target": 0.5,
        "baseline": None,
        "status": TARGET_UNBASELINED,
        "note": "TARGETS PENDING BASELINE BENCHMARKS",
    },
    "workflow_queue_wait_seconds_per_class": {
        "target": 300.0,
        "baseline": None,
        "status": TARGET_UNBASELINED,
        "note": "TARGETS PENDING BASELINE BENCHMARKS; per-class",
    },
    "broker_event_delivery_lag_seconds": {
        "target": 60.0,
        "baseline": None,
        "status": TARGET_UNBASELINED,
        "note": "TARGETS PENDING BASELINE BENCHMARKS",
    },
    "gpu_oom_rate_per_device": {
        "target": 0.01,
        "baseline": None,
        "status": TARGET_UNBASELINED,
        "note": "TARGETS PENDING BASELINE BENCHMARKS",
    },
    "dead_letter_age_seconds": {
        "target": 3600.0,
        "baseline": None,
        "status": TARGET_UNBASELINED,
        "note": "TARGETS PENDING BASELINE BENCHMARKS",
    },
    "pipeline_index_sync_lag_seconds": {
        "target": 600.0,
        "baseline": None,
        "status": TARGET_UNBASELINED,
        "note": "TARGETS PENDING BASELINE BENCHMARKS",
    },
    "worm_readback_failure_rate": {
        "target": 0.0,
        "baseline": None,
        "status": TARGET_UNBASELINED,
        "note": "TARGETS PENDING BASELINE BENCHMARKS",
    },
}


def is_baselined(metric: str) -> bool:
    entry = SLO_TARGETS.get(metric)
    return bool(entry) and entry.get("baseline") is not None


def slo_status(metric: str) -> str:
    """Dashboard-facing status string: the sentinel until baselined."""
    return "OK" if is_baselined(metric) else TARGET_UNBASELINED
