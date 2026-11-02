"""Inbox / processed-message registry (TRD § 23.6.2).

Consuming bounded contexts claim each event row exactly-once by
``event_id`` (natural key = the outbox ``event_id``): broker
redelivery of an already-processed event is a safe no-op (REO-006).
Where ordering matters (e.g. ``sequence_no`` within one aggregate),
the consumer processes strictly in order and blocks on missing
predecessors rather than reordering.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional


@dataclass
class InboxRecord:
    """One ``platform_inbox`` row (§ 23.6.2 DDL)."""

    event_id: str
    consumer_context: str
    received_at: datetime
    processed_at: Optional[datetime] = None
    dedupe_key: Optional[str] = None


class InboxRegistry:
    """In-memory model of the ``platform_inbox`` table.

    ``mark_processed`` is the claim step: an event already present for
    the same ``(consumer_context, dedupe_key)`` is reported as a
    duplicate no-op, never re-applied.
    """

    def __init__(self) -> None:
        self._rows: Dict[str, InboxRecord] = {}
        self._dedupe: Dict[tuple, str] = {}

    def seen(self, event_id: str, consumer_context: str = "") -> bool:
        """True when this consumer has already processed the event."""
        record = self._rows.get(event_id)
        if record is None or record.processed_at is None:
            return False
        if consumer_context and record.consumer_context != consumer_context:
            return False
        return True

    def claim(self, event_id: str, consumer_context: str, dedupe_key: Optional[str] = None) -> bool:
        """INSERT … ON CONFLICT DO NOTHING. Returns True when this call
        claims the row for processing; False when it is a duplicate
        redelivery (safe no-op, REO-006)."""
        if event_id in self._rows:
            return False
        record = InboxRecord(
            event_id=event_id,
            consumer_context=consumer_context,
            received_at=datetime.now(timezone.utc),
            dedupe_key=dedupe_key,
        )
        self._rows[event_id] = record
        if dedupe_key is not None:
            dedupe_key = f"{consumer_context}:{dedupe_key}"
            if dedupe_key in self._dedupe:
                # Coarser-dedupe collision: the earlier row owns it.
                return False
            self._dedupe[dedupe_key] = event_id
        return True

    def mark_processed(self, event_id: str) -> Optional[InboxRecord]:
        """Complete processing of a claimed event. Idempotent: marking an
        already-processed event twice returns the record but does not
        double-apply anything on the consumer side."""
        record = self._rows.get(event_id)
        if record is None:
            return None
        if record.processed_at is None:
            record.processed_at = datetime.now(timezone.utc)
        return record
