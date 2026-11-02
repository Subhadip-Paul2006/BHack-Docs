"""ps05.infra — resource scheduling data, observability and structured logging.

Modules:

- :mod:`ps05.infra.resources` — versioned ``WorkloadProfile`` records,
  ``ResourceProfileRegistry``, the ``GpuBudget`` / ``GpuAdmission`` VRAM
  budget (never a single-card assumption), backpressure limits, load-shedder
  and queue aging (TRD § 23.9, RES-001…RES-007).
- :mod:`ps05.infra.observability` — optional OpenTelemetry tracer/meter,
  thread-bound ``ExecutionContext`` propagation (OBS-002), metric-group names
  (OBS-004) and SLO targets flagged pending baseline (OBS-005/OBS-011).
- :mod:`ps05.infra.logging` — structured JSON logging with a hard redaction
  invariant: payloads, private keys, biometric embeddings and case content
  never reach a log line by default (OBS-003/OBS-013).
- :mod:`ps05.infra.alerting` — alert rules and the SLO-readiness report,
  where every un-baselined target renders as ``TARGET_UNBASELINED``.
"""

from ps05.infra.alerting import (
    ALERT_RULES,
    AlertRule,
    evaluate_current_slo_readiness,
    rule,
)
from ps05.infra.logging import (
    SENSITIVE_BLOCKLIST,
    log_event,
    redact,
    setup_structured_logging,
)
from ps05.infra.observability import (
    METRIC_GROUPS,
    SLO_TARGETS,
    TARGET_UNBASELINED,
    ExecutionContext,
    bind_context,
    instrument_task,
    meter,
    tracer,
)
from ps05.infra.resources import (
    KNOWN_PROFILE_REFS,
    BackpressureLimits,
    GpuAdmission,
    GpuBudget,
    GpuDevice,
    LoadShedder,
    QueueAgeTracker,
    ResourceProfileRegistry,
    WorkloadProfile,
    fairness_rank,
    parse_profile_ref,
)

__all__ = [
    # resources
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
    # observability
    "ExecutionContext",
    "tracer",
    "meter",
    "bind_context",
    "instrument_task",
    "METRIC_GROUPS",
    "SLO_TARGETS",
    "TARGET_UNBASELINED",
    # logging
    "SENSITIVE_BLOCKLIST",
    "setup_structured_logging",
    "redact",
    "log_event",
    # alerting
    "AlertRule",
    "ALERT_RULES",
    "rule",
    "evaluate_current_slo_readiness",
]
