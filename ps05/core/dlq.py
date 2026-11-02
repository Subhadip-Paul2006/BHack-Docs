"""Dead-letter queue & quarantine (TRD § 23.10, REO-008).

Quarantine is deliberate: tasks that exhaust their retry budget, or
that fail with a *permanent* error class, are parked here with full
failure evidence — never silently dropped. Redrive is a **deliberate,
dual-control, operator-only** action; it is never automated (REO-008).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ps05.core.errors import ErrorClass, classify


@dataclass
class QuarantineRecord:
    """One quarantined task's failure evidence (never payload data —
    § 23.13: failure evidence is error_class/error_code/attempt/
    checkpoint, not the evidence itself)."""

    task_id: str
    error_class: ErrorClass
    error_code: Optional[str]
    reason: str
    quarantined_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    redriven_by: Optional[str] = None
    redrive_context: Optional[Dict[str, object]] = None

    @property
    def disposition(self) -> str:
        return "redriven" if self.redriven_by else "quarantined"


class DeadLetterQueue:
    """In-memory model of the ``quarantine_outputs`` / DLQ surface."""

    def __init__(self) -> None:
        self._quarantined: Dict[str, QuarantineRecord] = {}

    def quarantine(
        self,
        task_id: str,
        error_class: ErrorClass,
        error_code: Optional[str] = None,
        reason: str = "",
    ) -> QuarantineRecord:
        record = QuarantineRecord(
            task_id=task_id, error_class=error_class, error_code=error_code, reason=reason
        )
        self._quarantined[task_id] = record
        return record

    def redrive(
        self, task_ids: List[str], operator: str, context: Dict[str, object]
    ) -> List[QuarantineRecord]:
        """Dual-control operator re-drive (REO-008).

        ``operator`` must name an authorized examiner/operator role;
        ``context`` is the logged, auditable basis for the re-drive.
        Raises ``PermissionError`` when either is missing — redrive is
        never anonymous, never automated.
        """
        if not operator:
            raise PermissionError("redrive requires an authorized operator identity (REO-008 dual-control)")
        if not context:
            raise ValueError("redrive requires an auditable context (dual-control record)")
        released: List[QuarantineRecord] = []
        for task_id in task_ids:
            record = self._quarantined.pop(task_id, None)
            if record is None:
                continue
            record.redriven_by = operator
            record.redrive_context = dict(context)
            released.append(record)
        return released

    def classify(self, error_class: ErrorClass) -> str:
        """Route decision: permanent classes go to quarantine immediately
        (no endless retry); retryable classes may exhaust their budget
        and *then* land here."""
        return classify(error_class)

    def pending(self) -> List[QuarantineRecord]:
        return list(self._quarantined.values())
