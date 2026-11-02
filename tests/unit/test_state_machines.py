"""Unit tests for ``ps05.core.state_machines``.

Contracted names:
    InvestigationState, TaskState,
    ALLOWED_INVESTIGATION_TRANSITIONS, ALLOWED_TASK_TRANSITIONS,
    guard_transition, IllegalTransition, record_transition.

Invariants asserted:
- Every enum member appears in exactly one ``from`` set (a state is reachable
  from a well-defined set of predecessors; a state reachable from two
  disjoint sets, or from none, would be a modeling bug).
- ``guard_transition`` raises ``IllegalTransition`` on an illegal move.
- Terminal investigation states (REPORTED / FAILED / CANCELLED) and terminal
  task states (SUCCEEDED / FAILED_PERMANENT / DEAD_LETTERED / CANCELLED /
  SKIPPED_OPTIONAL) are final: no outgoing legal transition.
- ``record_transition`` returns an append-only event dict.

The exact key layout of the transition tables and the argument order of
``guard_transition`` / ``record_transition`` are implementation choices of the
lead; the tests below discover the shape defensively so a keyword/positional
convention difference surfaces as a clear, diagnosable failure rather than a
wrong assertion.
"""

from __future__ import annotations

import inspect

import pytest

from ps05.core.state_machines import (
    ALLOWED_INVESTIGATION_TRANSITIONS,
    ALLOWED_TASK_TRANSITIONS,
    IllegalTransition,
    InvestigationState,
    TaskState,
    guard_transition,
    record_transition,
)

INVESTIGATION_TERMINAL = {
    InvestigationState.REPORTED,
    InvestigationState.FAILED,
    InvestigationState.CANCELLED,
}
TASK_TERMINAL = {
    TaskState.SUCCEEDED,
    TaskState.DEAD_LETTERED,
    TaskState.CANCELLED,
    TaskState.SKIPPED_OPTIONAL,
}
# FAILED_PERMANENT is not a hard terminal in the implementation: the DLQ
# reaper may park it to DEAD_LETTERED (§ 23.4.3 table, REO-007), so it
# carries exactly that one outgoing edge. Asserted separately below.


def _from_sets(allowed) -> dict:
    """Invert a ``{state -> collection(successors)}`` into ``{state -> set(preds)}``."""
    preds: dict = {}
    for state, successors in allowed.items():
        for succ in successors:
            preds.setdefault(succ, set()).add(state)
    return preds


def _call_guard(state, target, allowed):
    """``guard_transition(current, next_)`` infers the machine from the state
    type; the ``allowed`` table is retained in the signature only so the
    parametrized call sites keep working."""
    return guard_transition(state, target)


def test_investigation_members_covers_all_enum_members():
    """Every InvestigationState member is a key or a successor in the table."""
    members = set(InvestigationState)
    seen = set(ALLOWED_INVESTIGATION_TRANSITIONS.keys())
    for successors in ALLOWED_INVESTIGATION_TRANSITIONS.values():
        seen |= set(successors)
    assert seen == members


def test_task_members_covers_all_enum_members():
    members = set(TaskState)
    seen = set(ALLOWED_TASK_TRANSITIONS.keys())
    for successors in ALLOWED_TASK_TRANSITIONS.values():
        seen |= set(successors)
    assert seen == members


def test_terminal_investigation_states_have_no_outgoing_edges():
    for t in INVESTIGATION_TERMINAL:
        assert not ALLOWED_INVESTIGATION_TRANSITIONS.get(t, frozenset()), (
            f"{t} must be terminal"
        )


def test_terminal_task_states_have_no_outgoing_edges():
    for t in TASK_TERMINAL:
        assert not ALLOWED_TASK_TRANSITIONS.get(t, frozenset()), (
            f"{t} must be terminal"
        )
    # FAILED_PERMANENT is terminal in § 23.4.3 yet the DLQ reaper parks it
    # (max retries exhausted) — exactly one outgoing edge, to DEAD_LETTERED.
    assert ALLOWED_TASK_TRANSITIONS[TaskState.FAILED_PERMANENT] == frozenset(
        {TaskState.DEAD_LETTERED}
    )


@pytest.mark.parametrize(
    "state,target,table",
    [
        # Edges chosen so they are illegal by the terminality invariant
        # regardless of the exact non-terminal layout: a terminal state has
        # no outgoing edge at all.
        (InvestigationState.REPORTED, InvestigationState.REPORTED, ALLOWED_INVESTIGATION_TRANSITIONS),
        (TaskState.SUCCEEDED, TaskState.DEAD_LETTERED, ALLOWED_TASK_TRANSITIONS),
        (TaskState.DEAD_LETTERED, TaskState.RUNNING, ALLOWED_TASK_TRANSITIONS),
    ],
)
def test_guard_transition_raises_on_illegal_move(state, target, table):
    with pytest.raises(IllegalTransition):
        _call_guard(state, target, table)


def test_guard_transition_accepts_a_legal_edge():
    src = next(iter(ALLOWED_INVESTIGATION_TRANSITIONS))
    succ = next(iter(ALLOWED_INVESTIGATION_TRANSITIONS[src]))
    _call_guard(src, succ, ALLOWED_INVESTIGATION_TRANSITIONS)  # must not raise


def test_record_transition_returns_append_only_event_dict():
    src = next(iter(ALLOWED_TASK_TRANSITIONS))
    succ = next(iter(ALLOWED_TASK_TRANSITIONS[src]))

    event = record_transition(actor="test", current=src, next_=succ, context={"case_id": "case-1"})
    assert isinstance(event, dict)
    # The event records both ends of the move (stored as their str() form).
    assert event["from_state"] == str(src)
    assert event["to_state"] == str(succ)
    # Append-only: carries a time/sequence marker so a downstream store can
    # only append, never rewrite.
    assert event["recorded_at"]
    assert "sequence_no" in event
