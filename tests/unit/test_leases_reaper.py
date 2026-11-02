"""Unit tests for ``ps05.core.leases``: lease claim/renew/staleness + reaper.

Verifies the § 23.6.4 lease/recovery contract with fake lease records and no
Celery: claim sets ``lease_expires_at = now + ttl``; renew extends the expiry;
staleness is only True at/after expiry; ``StaleTaskReaper.reap`` returns
exactly the stale tasks still under their attempt ceiling, parks tasks at/past
the ceiling on the exceeded (operator-intervention) path, and reassign bumps the
attempt and appends a recovery-audit record.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ps05.core.leases import LeaseManager, LeaseRecord, StaleTaskReaper


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _stale_claim(manager: LeaseManager, task_id: str, worker: str, attempt: int, now: datetime) -> LeaseRecord:
    """Claim a fresh task then force its lease to be already expired."""
    rec = manager.claim(task_id, worker, timedelta(seconds=60), attempt_number=attempt)
    rec.lease_expires_at = now - timedelta(seconds=1)
    rec.heartbeat_at = now - timedelta(seconds=1)
    return rec


def test_claim_sets_expiry_to_now_plus_ttl():
    manager = LeaseManager()
    rec = manager.claim("task-1", "worker-1", timedelta(seconds=10))
    # claim sets heartbeat_at = now and lease_expires_at = now + ttl, so the
    # gap between the two is exactly the requested TTL.
    assert rec.lease_expires_at - rec.heartbeat_at == timedelta(seconds=10)
    assert rec.worker_id == "worker-1"
    assert rec.attempt_number == 1
    # A freshly-claimed lease is live: it is not stale right now.
    assert manager.is_stale(rec, datetime.now(timezone.utc)) is False


def test_renew_extends_expiry():
    manager = LeaseManager()
    rec = manager.claim("task-1", "worker-1", timedelta(seconds=10))
    expiry_before = rec.lease_expires_at
    assert manager.renew("task-1", timedelta(seconds=99)) is True
    assert rec.lease_expires_at > expiry_before
    assert rec.lease_expires_at - rec.heartbeat_at == timedelta(seconds=99)
    # Renewing an unknown task is a no-op that reports False.
    assert manager.renew("never-claimed", timedelta(seconds=99)) is False


def test_is_stale_only_after_expiry():
    manager = LeaseManager()
    now = _now()
    rec = LeaseRecord("task-1", "worker-1", now + timedelta(seconds=5), now, 1)
    assert manager.is_stale(rec, now) is False
    assert manager.is_stale(rec, now + timedelta(seconds=5)) is True  # exactly at expiry
    assert manager.is_stale(rec, now + timedelta(seconds=6)) is True


def test_reap_returns_only_stale_under_ceiling():
    manager = LeaseManager()
    now = _now()
    _stale_claim(manager, "t-reap", "worker-1", attempt=1, now=now)       # under ceiling -> reaped
    _stale_claim(manager, "t-max", "worker-2", attempt=3, now=now)        # at ceiling -> exceeded path
    live = manager.claim("t-live", "worker-3", timedelta(seconds=60))     # not stale -> untouched

    reaper = StaleTaskReaper(manager, max_attempts=3)
    reaped = reaper.reap(now)

    assert reaped == ["t-reap"]
    # The at-ceiling task is released, not re-assigned (operator/DLQ heads-up).
    assert manager.get("t-max") is None
    # The live task is left alone.
    assert manager.get("t-live") is live
    assert live.worker_id == "worker-3"


def test_reap_exceeded_path_records_max_attempts_audit():
    manager = LeaseManager()
    now = _now()
    _stale_claim(manager, "t-max", "worker-2", attempt=3, now=now)
    reaper = StaleTaskReaper(manager, max_attempts=3)
    reaper.reap(now)
    audit = reaper.recovery_audit
    heads_up = [a for a in audit if a.get("task_id") == "t-max"]
    assert len(heads_up) == 1
    assert heads_up[0]["kind"] == "lease_expired_max_attempts"
    assert heads_up[0]["error_class"] == "WORKER_LOST"


def test_reassign_bumps_attempt_and_appends_audit():
    manager = LeaseManager()
    now = _now()
    _stale_claim(manager, "t-reap", "worker-old", attempt=1, now=now)
    reaper = StaleTaskReaper(manager, max_attempts=3)
    reaper.reap(now)

    new_rec = manager.get("t-reap")
    assert new_rec is not None
    assert new_rec.attempt_number == 2            # bumped from 1
    assert new_rec.worker_id != "worker-old"      # reassigned to a new worker

    audit = [a for a in reaper.recovery_audit if a.get("kind") == "lease_reassigned"]
    assert len(audit) == 1
    entry = audit[0]
    # Recovery-audit record: task id, prior->new worker, attempt, reason (error class).
    assert entry["task_id"] == "t-reap"
    assert entry["prior_worker_id"] == "worker-old"
    assert entry["new_worker_id"] == new_rec.worker_id
    assert entry["attempt_number"] == 2
    assert entry["error_class"] == "WORKER_LOST"
