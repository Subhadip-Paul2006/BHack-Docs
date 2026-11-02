"""Investigation and task state machines (TRD § 23.4).

Two append-only state machines:

* :class:`InvestigationState` — the ``case_investigation`` lifecycle
  (§ 23.4.1 / § 23.4.2). Terminal states: ``REPORTED``, ``FAILED``,
  ``CANCELLED``.
* :class:`TaskState` — the ``platform_task_records`` lifecycle
  (§ 23.4.3). Terminal states: ``SUCCEEDED``, ``FAILED_PERMANENT``,
  ``DEAD_LETTERED``, ``CANCELLED``, ``SKIPPED_OPTIONAL``.

``guard_transition`` validates a single move and raises
:class:`IllegalTransition` on an illegal one. ``record_transition``
validates and then appends an append-only event dict to the module
transition log (REO-002: every transition is an auditable row).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, FrozenSet


class InvestigationState(str, Enum):
    CREATED = "CREATED"
    INGESTING = "INGESTING"
    INTEGRITY_VERIFICATION = "INTEGRITY_VERIFICATION"
    WORM_COMMITTED = "WORM_COMMITTED"
    EVIDENCE_REGISTERED = "EVIDENCE_REGISTERED"
    TRIAGE_PENDING = "TRIAGE_PENDING"
    ANALYSIS_PLANNED = "ANALYSIS_PLANNED"
    PROCESSING = "PROCESSING"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    ANALYSIS_COMPLETED = "ANALYSIS_COMPLETED"
    REVIEW_PENDING = "REVIEW_PENDING"
    REVIEW_COMPLETED = "REVIEW_COMPLETED"
    REPORT_PENDING = "REPORT_PENDING"
    REPORTED = "REPORTED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class TaskState(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    CHECKPOINTED = "CHECKPOINTED"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    DEAD_LETTERED = "DEAD_LETTERED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    SKIPPED_OPTIONAL = "SKIPPED_OPTIONAL"


_INVESTIGATION_TERMINAL: FrozenSet[InvestigationState] = frozenset(
    {InvestigationState.REPORTED, InvestigationState.FAILED, InvestigationState.CANCELLED}
)
_TASK_TERMINAL: FrozenSet[TaskState] = frozenset(
    {
        TaskState.SUCCEEDED,
        TaskState.FAILED_PERMANENT,
        TaskState.DEAD_LETTERED,
        TaskState.CANCELLED,
        TaskState.SKIPPED_OPTIONAL,
    }
)

# Every pre-terminal investigation state accepts CANCEL_REQUESTED
# (§ 23.4.2 "any pre-terminal -> CANCEL_REQUESTED").
_CANCELABLE = frozenset(InvestigationState) - _INVESTIGATION_TERMINAL - frozenset(
    {InvestigationState.CANCEL_REQUESTED}
)

ALLOWED_INVESTIGATION_TRANSITIONS: Dict[InvestigationState, FrozenSet[InvestigationState]] = {
    InvestigationState.CREATED: frozenset({InvestigationState.INGESTING}),
    InvestigationState.INGESTING: frozenset({InvestigationState.INTEGRITY_VERIFICATION}),
    InvestigationState.INTEGRITY_VERIFICATION: frozenset(
        {InvestigationState.WORM_COMMITTED, InvestigationState.FAILED}
    ),
    InvestigationState.WORM_COMMITTED: frozenset({InvestigationState.EVIDENCE_REGISTERED}),
    InvestigationState.EVIDENCE_REGISTERED: frozenset({InvestigationState.TRIAGE_PENDING}),
    InvestigationState.TRIAGE_PENDING: frozenset({InvestigationState.ANALYSIS_PLANNED}),
    InvestigationState.ANALYSIS_PLANNED: frozenset({InvestigationState.PROCESSING}),
    InvestigationState.PROCESSING: frozenset(
        {
            InvestigationState.PARTIAL_SUCCESS,
            InvestigationState.ANALYSIS_COMPLETED,
            InvestigationState.FAILED,
        }
    ),
    InvestigationState.PARTIAL_SUCCESS: frozenset({InvestigationState.REVIEW_PENDING}),
    InvestigationState.ANALYSIS_COMPLETED: frozenset({InvestigationState.REVIEW_PENDING}),
    InvestigationState.REVIEW_PENDING: frozenset({InvestigationState.REVIEW_COMPLETED}),
    InvestigationState.REVIEW_COMPLETED: frozenset({InvestigationState.REPORT_PENDING}),
    InvestigationState.REPORT_PENDING: frozenset({InvestigationState.REPORTED}),
    # CANCEL_REQUESTED -> CANCELLED (confirmed) or back to any cancelable
    # pre-terminal state ("cancel denied, resume" in § 23.4.1).
    InvestigationState.CANCEL_REQUESTED: frozenset(
        {InvestigationState.CANCELLED} | _CANCELABLE
    ),
    InvestigationState.REPORTED: frozenset(),
    InvestigationState.FAILED: frozenset(),
    InvestigationState.CANCELLED: frozenset(),
}
for _state in _CANCELABLE:
    ALLOWED_INVESTIGATION_TRANSITIONS[_state] = (
        ALLOWED_INVESTIGATION_TRANSITIONS[_state] | frozenset({InvestigationState.CANCEL_REQUESTED})
    )

ALLOWED_TASK_TRANSITIONS: Dict[TaskState, FrozenSet[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.READY, TaskState.CANCELLED, TaskState.SKIPPED_OPTIONAL}),
    TaskState.READY: frozenset({TaskState.CLAIMED, TaskState.CANCELLED, TaskState.SKIPPED_OPTIONAL}),
    TaskState.CLAIMED: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.RUNNING: frozenset(
        {
            TaskState.CHECKPOINTED,
            TaskState.SUCCEEDED,
            TaskState.FAILED_RETRYABLE,
            TaskState.FAILED_PERMANENT,
            TaskState.BLOCKED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.CHECKPOINTED: frozenset({TaskState.RUNNING}),
    TaskState.FAILED_RETRYABLE: frozenset({TaskState.RETRY_WAIT}),
    TaskState.RETRY_WAIT: frozenset({TaskState.READY}),
    # FAILED_PERMANENT is terminal (§ 23.4.3 diagram) yet the DLQ reaper may
    # park it (max retries exhausted -> DEAD_LETTERED, § 23.4.3 table).
    TaskState.FAILED_PERMANENT: frozenset({TaskState.DEAD_LETTERED}),
    # BLOCKED: required dependency failed terminally. If the dependency's
    # own retry later succeeds, the dependency monitor re-readies the
    # blocked task (REO-001 blocking invariant, § 23.3.3 rule 1).
    TaskState.BLOCKED: frozenset({TaskState.READY}),
    TaskState.DEAD_LETTERED: frozenset(),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.SKIPPED_OPTIONAL: frozenset(),
}


class IllegalTransition(Exception):
    """Raised when a state move is not in the machine's transition table."""

    def __init__(self, current: Any, next_: Any) -> None:
        self.current = current
        self.next = next_
        super().__init__(f"Illegal transition: {current} -> {next_}")


def _table_for(state: Any) -> Dict[Any, FrozenSet[Any]]:
    if isinstance(state, InvestigationState):
        return ALLOWED_INVESTIGATION_TRANSITIONS
    if isinstance(state, TaskState):
        return ALLOWED_TASK_TRANSITIONS
    raise TypeError(f"state must be InvestigationState or TaskState, got {type(state)!r}")


def guard_transition(current: Any, next_: Any) -> None:
    """Validate ``current -> next_``; raise :class:`IllegalTransition` otherwise."""
    table = _table_for(current)
    allowed = table.get(current, frozenset())
    if next_ not in allowed:
        raise IllegalTransition(current, next_)


def legal_terminal(state: Any) -> bool:
    """True when ``state`` is a terminal state of its machine."""
    if isinstance(state, InvestigationState):
        return state in _INVESTIGATION_TERMINAL
    if isinstance(state, TaskState):
        return state in _TASK_TERMINAL
    raise TypeError(f"state must be InvestigationState or TaskState, got {type(state)!r}")


# Append-only transition log (in-memory view of ``platform_task_state_events`` /
# custody-event rows; production rows are written in the same PostgreSQL
# transaction as the state write — REO-002).
_TRANSITION_LOG: list[Dict[str, Any]] = []


def get_transition_log() -> list[Dict[str, Any]]:
    """Return the append-only transition log (read-only view; never mutated in place)."""
    return list(_TRANSITION_LOG)


def record_transition(
    actor: str,
    current: Any,
    next_: Any,
    context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Validate the move and append an event dict to the transition log.

    Returns the event dict that was appended (the log is append-only;
    events are never rewritten or deleted — supersession of terminal
    states is a separate linked record, never an overwrite, REO-002).
    """
    guard_transition(current, next_)
    event: Dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "actor": actor,
        "state_kind": "investigation" if isinstance(current, InvestigationState) else "task",
        "from_state": str(current),
        "to_state": str(next_),
        "context": dict(context or {}),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "sequence_no": len(_TRANSITION_LOG) + 1,
    }
    _TRANSITION_LOG.append(event)
    return event
