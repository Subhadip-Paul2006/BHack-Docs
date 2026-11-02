"""Transactional outbox (TRD § 23.6.1).

Business write + outbox-event insert happen in **one** call site; the
publisher models the PostgreSQL side of the boundary. PostgreSQL and
Redis do **not** form a single ACID unit (§ 23.6.3, STG-002) — the
outbox row is the durable commitment, and a *dispatcher* bridges it to
the broker. A committed-but-undelivered row (``PENDING`` after a
broker outage) is a first-class, alertable, redriveable state; it is
never silently dropped.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

#: Delivery states for ``platform_outbox`` rows (§ 23.6.1).
PENDING = "PENDING"
PUBLISHED = "PUBLISHED"
FAILED = "FAILED"


class OutboxStatus(str, Enum):
    PENDING = PENDING
    PUBLISHED = PUBLISHED
    FAILED = FAILED


@dataclass
class OutboxEvent:
    """One ``platform_outbox`` row (§ 23.6.1 DDL)."""

    event_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict
    sequence_no: int
    created_at: datetime
    published_at: Optional[datetime] = None
    delivery_status: str = PENDING
    publish_attempts: int = 0

    def to_row(self) -> Dict[str, object]:
        """Projection matching the ``platform_outbox`` columns."""
        return {
            "event_id": self.event_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "event_type": self.event_type,
            "analysis_run_id": self.payload.get("analysis_run_id"),
            "case_id": self.payload.get("case_id"),
            "payload": self.payload,
            "sequence_no": self.sequence_no,
            "created_at": self.created_at,
            "published_at": self.published_at,
            "delivery_status": self.delivery_status,
        }


class OutboxPublisher:
    """In-memory model of the ``platform_outbox`` table + insert path.

    ``enqueue`` is called *inside the same transaction* as the business
    write it accompanies — in production both are one PostgreSQL unit of
    work (REO-002). The returned ``event_id`` is the natural dedupe key
    on the consuming side (``platform_inbox``, § 23.6.2).
    """

    def __init__(self) -> None:
        self._rows: Dict[str, OutboxEvent] = {}
        self._seq: Dict[Tuple[str, str], int] = {}  # (aggregate_type, aggregate_id) -> last used sequence

    def enqueue(
        self,
        db_tx: object,
        event_type: str,
        aggregate_id: str,
        payload: dict,
        *,
        aggregate_type: str = "case_investigation",
    ) -> str:
        """Insert an outbox row committed with the state change in ``db_tx``.

        Returns the new ``event_id``. ``sequence_no`` is monotonic per
        ``(aggregate_type, aggregate_id)`` (§ 23.6.1 UNIQUE constraint).
        """
        event_id = str(uuid.uuid4())
        key = (aggregate_type, aggregate_id)
        seq = self._seq.get(key, 0) + 1
        self._seq[key] = seq
        row = OutboxEvent(
            event_id=event_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=dict(payload),
            sequence_no=seq,
            created_at=datetime.now(timezone.utc),
            delivery_status=PENDING,
        )
        self._rows[event_id] = row
        return event_id

    def row(self, event_id: str) -> Optional[OutboxEvent]:
        return self._rows.get(event_id)

    def pending_rows(self) -> List[OutboxEvent]:
        return [r for r in self._rows.values() if r.delivery_status == PENDING]

    def unreconciled_rows(self) -> List[OutboxEvent]:
        """``PENDING`` or ``FAILED`` rows — the named reconciliation states
        (STG-003); the reconciler redrives or pages on these, never drops."""
        return [r for r in self._rows.values() if r.delivery_status in (PENDING, FAILED)]


class OutboxDispatcher:
    """Bridges ``PENDING`` outbox rows to the broker (§ 23.6.1).

    ``dispatch_one`` is the broker publish call (e.g. a Celery task
    message). It is invoked per row; ``mark_dispatched`` commits the row
    to ``PUBLISHED`` in a **separate** short transaction (the § 23.6.3
    non-ACID boundary: publish success and the DB mark are two commits).
    """

    def __init__(self, publisher: OutboxPublisher) -> None:
        self._publisher = publisher

    def poll(self, dispatch_one: Callable[[OutboxEvent], None]) -> List[str]:
        """Deliver pending rows ordered by ``(aggregate, sequence_no)``.

        A raise from ``dispatch_one`` leaves the row ``PENDING`` with a
        bumped attempt count so the next poll retries (eventually the
        budget moves it to ``FAILED`` — see :meth:`redrive`).
        Returns the ``event_id``s actually published.
        """
        pending = self._publisher.pending_rows()
        pending.sort(key=lambda r: (r.aggregate_type, r.aggregate_id, r.sequence_no))
        published: List[str] = []
        for row in pending:
            try:
                dispatch_one(row)
            except Exception:
                row.publish_attempts += 1
                continue
            self.mark_dispatched(row.event_id)
            published.append(row.event_id)
        return published

    def mark_dispatched(self, event_id: str) -> None:
        row = self._publisher.row(event_id)
        if row is not None and row.delivery_status != PUBLISHED:
            row.delivery_status = PUBLISHED
            row.published_at = datetime.now(timezone.utc)

    def redrive(self, event_ids: List[str]) -> int:
        """Re-queue parked ``FAILED`` rows (operator-reconciler path,
        STG-003): back to ``PENDING`` so the next poll retries. Never
        silently dropped."""
        revived = 0
        for event_id in event_ids:
            row = self._publisher.row(event_id)
            if row is not None and row.delivery_status == FAILED:
                row.delivery_status = PENDING
                row.publish_attempts = 0
                revived += 1
        return revived

    def park_failed(self, event_id: str) -> None:
        """Park a row after its dispatcher retry budget is exhausted
        (first-class alertable condition, OBS-010)."""
        row = self._publisher.row(event_id)
        if row is not None:
            row.delivery_status = FAILED
