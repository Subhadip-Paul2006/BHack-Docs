"""Worker leases & recovery (TRD § 23.6.4).

Lease acquisition writes ``platform_task_lease(task_id, worker_id,
lease_expires_at)`` in the **same** transaction as the task's move to
``CLAIMED`` — no two workers can hold a live lease for one task
(REO-007). The Stale-Task Reaper finds expired leases on still-
``CLAIMED``/``RUNNING`` tasks, marks them ``WORKER_LOST``, and
reassigns under the **same** idempotency key with ``attempt_number``
incremented — safe because every stage is idempotent at
checkpoint granularity (§ 23.7): a recovered task resumes from the last
completed durable checkpoint, never from in-memory progress (REO-009).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional


@dataclass
class LeaseRecord:
    """One ``platform_task_lease`` row (§ 23.6.4)."""

    task_id: str
    worker_id: str
    lease_expires_at: datetime
    heartbeat_at: datetime
    attempt_number: int
    checkpoint_id: Optional[str] = None

    def is_stale(self, now: datetime) -> bool:
        """A worker that cannot renew before ``lease_expires_at`` has an
        **expired** lease, not an in-flight one (§ 23.6.4)."""
        return now >= self.lease_expires_at


class LeaseManager:
    """Holds the live lease map for tasks in ``CLAIMED``/``RUNNING``."""

    def __init__(self) -> None:
        self._leases: Dict[str, LeaseRecord] = {}

    def claim(self, task_id: str, worker_id: str, lease_ttl: timedelta, *, attempt_number: int = 1, checkpoint_id: Optional[str] = None) -> LeaseRecord:
        """Acquire the lease in the same transaction as the ``CLAIMED`` move.

        Raising here would abort the claim transaction; a second worker
        racing for the same task is rejected by the DB unique constraint
        (single live lease per task, REO-007) — modelled by refusing to
        overwrite a live lease.
        """
        existing = self._leases.get(task_id)
        if existing is not None and not existing.is_stale(datetime.now(timezone.utc)):
            raise ValueError(f"live lease already held by {existing.worker_id!r} for task {task_id!r}")
        now = datetime.now(timezone.utc)
        record = LeaseRecord(
            task_id=task_id,
            worker_id=worker_id,
            lease_expires_at=now + lease_ttl,
            heartbeat_at=now,
            attempt_number=attempt_number,
            checkpoint_id=checkpoint_id,
        )
        self._leases[task_id] = record
        return record

    def renew(self, task_id: str, lease_ttl: timedelta) -> bool:
        """Refresh ``heartbeat_at`` and extend ``lease_expires_at``.
        Cheap, batched per worker — never per-task-per-second at the
        API tier (§ 23.6.4)."""
        record = self._leases.get(task_id)
        if record is None:
            return False
        now = datetime.now(timezone.utc)
        record.heartbeat_at = now
        record.lease_expires_at = now + lease_ttl
        return True

    def release(self, task_id: str) -> None:
        self._leases.pop(task_id, None)

    def get(self, task_id: str) -> Optional[LeaseRecord]:
        return self._leases.get(task_id)

    def is_stale(self, lease: LeaseRecord, now: datetime) -> bool:
        """§ 23.6.4: an expired lease means the worker is dead, not that
        the task is still in flight."""
        return lease.is_stale(now)


class StaleTaskReaper:
    """Death detection + safe reassignment (§ 23.6.4 expiry rules)."""

    def __init__(
        self,
        manager: LeaseManager,
        max_attempts: int = 3,
        reassign_ttl: timedelta = timedelta(seconds=60),
    ) -> None:
        self._manager = manager
        self._max_attempts = max_attempts
        self._reassign_ttl = reassign_ttl
        self._audit: List[Dict[str, object]] = []

    @property
    def recovery_audit(self) -> List[Dict[str, object]]:
        """``platform_task_lease_events`` trail: who reaped, when, prior
        and new ``worker_id``, prior ``checkpoint_id``. Part of the
        custody log; retained under the case retention / legal-hold
        policy (§ 23.11, § 15.2)."""
        return list(self._audit)

    def reap(self, now: datetime) -> List[str]:
        """Find expired leases on live tasks; returns the reaped task_ids.

        Honors ``max_attempts``: a task at or past its attempt ceiling
        is released for the DLQ path (``FAILED_PERMANENT -> DEAD_LETTERED``),
        never retried again (no unbounded retry loop, § 23.6.4)."""
        stale = [rec for rec in self._manager._leases.values() if rec.is_stale(now)]
        reaped: List[str] = []
        for rec in stale:
            if rec.attempt_number >= self._max_attempts:
                self._manager.release(rec.task_id)
                self._audit.append(
                    {
                        "task_id": rec.task_id,
                        "kind": "lease_expired_max_attempts",
                        "error_class": "WORKER_LOST",
                        "reaped_by": "StaleTaskReaper",
                        "prior_worker_id": rec.worker_id,
                        "prior_checkpoint_id": rec.checkpoint_id,
                        "at": now.isoformat(),
                    }
                )
                continue
            audit = self.reassign(rec.task_id, new_worker=f"reaper-{now.isoformat()}")
            reaped.append(rec.task_id)
        return reaped

    def reassign(self, task_id: str, new_worker: str) -> Dict[str, object]:
        """Re-queue under the same idempotency key, attempt incremented,
        granted a fresh lease window. Returns the recovery-audit dict
        appended to the lease-events trail."""
        prior = self._manager._leases.get(task_id)
        attempt_number = (prior.attempt_number + 1) if prior else 1
        checkpoint_id = prior.checkpoint_id if prior else None
        self._manager.release(task_id)
        record = self._manager.claim(
            task_id, new_worker, self._reassign_ttl, attempt_number=attempt_number, checkpoint_id=checkpoint_id
        )
        audit: Dict[str, object] = {
            "task_id": task_id,
            "kind": "lease_reassigned",
            "error_class": "WORKER_LOST",
            "reaped_by": "StaleTaskReaper",
            "prior_worker_id": prior.worker_id if prior else None,
            "new_worker_id": new_worker,
            "attempt_number": attempt_number,
            "prior_checkpoint_id": checkpoint_id,
            "lease_expires_at": record.lease_expires_at.isoformat(),
            "at": record.heartbeat_at.isoformat(),
        }
        self._audit.append(audit)
        return audit
