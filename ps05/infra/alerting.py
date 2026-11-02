"""Alert rules and SLO-readiness reporting (OBS-005 / OBS-011).

Every alert threshold is a *target pending baseline benchmarks*: a rule
carries its target but no assertion that the target is met. The readiness
report renders each SLO line as ``TARGET_UNBASELINED`` until a § 13 baseline
run stamps a baseline value on it — never a passing green checkmark.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ps05.infra.observability import SLO_TARGETS, TARGET_UNBASELINED

__all__ = ["AlertRule", "ALERT_RULES", "rule", "evaluate_current_slo_readiness"]


@dataclass(frozen=True)
class AlertRule:
    """One alert rule: fire when ``metric`` exceeds ``threshold`` sustained
    over ``window_s`` seconds. ``threshold`` is a target, not a guarantee —
    until the metric is baselined the rule is advisory only (OBS-011)."""

    name: str
    metric: str
    threshold: float
    window_s: int
    comparison: str = "gt"  # "gt" | "lt"
    severity: str = "warning"

    def fired(self, observed: float) -> bool:
        if self.comparison == "gt":
            return observed > self.threshold
        if self.comparison == "lt":
            return observed < self.threshold
        raise ValueError(f"unknown comparison {self.comparison!r}")


def rule(
    name: str,
    metric: str,
    threshold: float,
    window_s: int,
    comparison: str = "gt",
    severity: str = "warning",
) -> AlertRule:
    """Define (and register) an alert rule. The returned rule is the single
    source of truth for the rule identity; it is also appended to
    :data:`ALERT_RULES` so dashboards can enumerate them."""
    r = AlertRule(
        name=name,
        metric=metric,
        threshold=threshold,
        window_s=window_s,
        comparison=comparison,
        severity=severity,
    )
    if r not in ALERT_RULES:
        ALERT_RULES.append(r)
    return r


#: Operator/infra alert rules. Thresholds are TARGETS PENDING BASELINE
#: BENCHMARKS; tune them only after a § 13 baseline run stamps the matching
#: SLO entry, and never claim the target is met from this file alone.
ALERT_RULES: List[AlertRule] = []
rule("api_p95_latency_high", "api_request_latency_seconds", 0.5, 300, "gt", "warning")
rule(
    "workflow_queue_wait_high",
    "workflow_queue_wait_time_seconds",
    300.0,
    300,
    "gt",
    "warning",
)
rule(
    "broker_event_delivery_lag_high",
    "broker_event_delivery_lag_seconds",
    60.0,
    300,
    "gt",
    "critical",
)
rule(
    "gpu_oom_rate_high",
    "worker_gpu_utilization_percent",
    100.0,
    600,
    "gt",
    "warning",
)
rule("dead_letter_backlog", "workflow_dead_letter_count", 0, 600, "gt", "critical")
rule("stale_lease_backlog", "workflow_stale_lease_count", 0, 600, "gt", "critical")
rule(
    "index_sync_lag_high",
    "pipeline_index_sync_lag_seconds",
    600.0,
    300,
    "gt",
    "warning",
)
rule(
    "worm_readback_failures",
    "pipeline_worm_readback_failures",
    0,
    60,
    "gt",
    "critical",
)
rule(
    "findings_persisted_divergence",
    "pipeline_findings_persisted_vs_generated",
    1.0,
    600,
    "lt",
    "critical",
)


def evaluate_current_slo_readiness() -> List[Tuple[str, Any, Optional[Any], str]]:
    """Return one row per SLO candidate:
    ``(metric, target, baseline_or_None, status)``.

    Every row's ``status`` is :data:`TARGET_UNBASELINED` until its baseline
    field is set by a recorded § 13 benchmark run — no guaranteed numbers.
    """
    rows: List[Tuple[str, Any, Optional[Any], str]] = []
    for metric, entry in SLO_TARGETS.items():
        baseline = entry.get("baseline")
        status = "BASELINED" if baseline is not None else TARGET_UNBASELINED
        rows.append((metric, entry.get("target"), baseline, status))
    return rows
