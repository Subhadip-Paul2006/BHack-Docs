"""Unit tests for ``ps05.core.orchestrator`` over the default investigation DAG.

Verifies, with no Celery/Redis imports, that ``WorkflowEngine`` over
``build_default_investigation_dag()`` reaches ``ANALYSIS_COMPLETED`` only when
every required input of the fan-in succeeded (every recorded investigation
transition re-validated against ``ALLOWED_INVESTIGATION_TRANSITIONS``); that a
failed required spine task hard-fails the investigation and blocks downstream;
that a failed branch task leaves the other branches intact and the fan-in
aggregates ``PARTIAL_SUCCESS`` (never a false ``ANALYSIS_COMPLETED``); and that
cancelling from a pre-terminal state is legal while cancelling from a terminal
state raises ``IllegalTransition``.
"""

from __future__ import annotations

import pytest

from ps05.core.dags import WorkflowDag, build_default_investigation_dag
from ps05.core.errors import ErrorClass
from ps05.core.orchestrator import WorkflowEngine
from ps05.core.state_machines import (
    ALLOWED_INVESTIGATION_TRANSITIONS,
    IllegalTransition,
    InvestigationState,
    TaskState,
    get_transition_log,
    guard_transition,
)

#: The spine, branch, fan-in and report node ids of the default DAG.
SPINE = ("UPLOAD", "PREHASH", "WORMC", "EVIDREG", "TRIAGE")
BRANCHES = ("CMMS", "AIF", "AURAS", "VISTA", "ONTF", "CCHDF", "AFB", "VCI", "VDF", "IMLF", "CSTV")
REPORT_SPINE = ("AGG", "CDVF", "EGRAPH", "REVIEW", "UFREDF")
ALL_NODES = SPINE + BRANCHES + REPORT_SPINE


def _new_engine() -> WorkflowEngine:
    engine = WorkflowEngine()
    engine.plan_investigation("case-1", "evidence-1")
    return engine


def _run_to_fixpoint(engine: WorkflowEngine, fail_node: str, fail_class: ErrorClass = ErrorClass.INPUT_INVALID):
    """Dispatch ready nodes and settle each as SUCCEEDED until nothing is
    left; ``fail_node`` (when it appears) settles as a permanent failure."""
    dag = engine.dag
    done: set[str] = set()
    dispatched: list[str] = []

    def engine_map(node):
        dispatched.append(node.id)

    while True:
        batch = engine.dispatch_ready(dag, done, engine_map)
        if not batch:
            break
        for node_id in batch:
            if node_id == fail_node:
                engine.on_task_finished(node_id, TaskState.FAILED_PERMANENT, error_class=fail_class)
            else:
                engine.on_task_finished(node_id, TaskState.SUCCEEDED)
            done.add(node_id)
    return done, dispatched


def _investigation_events_since(mark: int) -> list[dict]:
    """The investigation-kind transitions recorded by the engine after ``mark``."""
    return [
        e for e in get_transition_log()[mark:]
        if e["state_kind"] == "investigation"
    ]


def _revalidate_investigation_transitions(events: list[dict]) -> None:
    """Re-check every recorded investigation transition against the guard table."""
    assert events, "engine recorded no investigation transitions"
    for event in events:
        current = InvestigationState(event["from_state"].split(".", 1)[1])
        target = InvestigationState(event["to_state"].split(".", 1)[1])
        assert target in ALLOWED_INVESTIGATION_TRANSITIONS[current], (
            f"illegal recorded transition {current} -> {target}"
        )


def test_plan_returns_dag_and_planned_state():
    engine = WorkflowEngine()
    mark = len(get_transition_log())
    state, dag = engine.plan_investigation("case-1", "evidence-1")
    assert state is InvestigationState.ANALYSIS_PLANNED
    assert isinstance(dag, WorkflowDag)
    assert set(dag.nodes) == set(ALL_NODES)
    assert engine.investigation_state is InvestigationState.ANALYSIS_PLANNED
    # Planning itself is not a recorded lifecycle move.
    assert _investigation_events_since(mark) == []


def test_full_success_reaches_analysis_completed():
    engine = _new_engine()
    mark = len(get_transition_log())
    done, dispatched = _run_to_fixpoint(engine, fail_node=None)

    # Every required spine/branch/fan-in/report node was dispatched and settled.
    assert set(dispatched) == set(ALL_NODES)
    assert done == set(ALL_NODES)
    assert engine.investigation_state is InvestigationState.ANALYSIS_COMPLETED
    _revalidate_investigation_transitions(_investigation_events_since(mark))


def test_failed_required_spine_task_fails_investigation_and_blocks_downstream():
    engine = _new_engine()
    dag = engine.dag
    done: set[str] = set()

    # Advance to the spine: UPLOAD dispatches and succeeds; PREHASH dispatches.
    assert engine.dispatch_ready(dag, done, lambda n: None) == ["UPLOAD"]
    engine.on_task_finished("UPLOAD", TaskState.SUCCEEDED)
    done.add("UPLOAD")
    # The first dispatch moves the investigation ANALYSIS_PLANNED -> PROCESSING.
    assert engine.dispatch_ready(dag, done, lambda n: None) == ["PREHASH"]

    # The required spine task (pre_decode_hash) fails permanently; a spine
    # terminal failure hard-fails the whole investigation (PROCESSING -> FAILED).
    engine.on_task_finished("PREHASH", TaskState.FAILED_PERMANENT, error_class=ErrorClass.INTEGRITY_FAILURE)
    assert engine.investigation_state is InvestigationState.FAILED

    # Its failure hard-blocks the downstream required consumers.
    assert engine.would_block_downstream(dag.nodes["PREHASH"], dag) is True
    # Nothing further is ready: the next dispatch is a no-op (WORMC blocked).
    assert engine.dispatch_ready(dag, done, lambda n: None) == []
    assert "WORMC" not in done


def test_failed_branch_task_leaves_other_branches_intact():
    # VDF is a branch task off VISTA; failing it must not touch the sibling
    # branches (VCI / CMMS / ONTF) — they stay ready or completed.
    engine = _new_engine()
    dag = engine.dag
    done: set[str] = set()
    dispatched: list[str] = []

    def engine_map(node):
        dispatched.append(node.id)

    while True:
        batch = engine.dispatch_ready(dag, done, engine_map)
        if not batch:
            break
        for node_id in batch:
            if node_id == "VDF":
                engine.on_task_finished(node_id, TaskState.FAILED_PERMANENT, error_class=ErrorClass.INPUT_INVALID)
            else:
                engine.on_task_finished(node_id, TaskState.SUCCEEDED)
            done.add(node_id)

    # The sibling branches completed normally despite the VDF failure.
    for other in ("VCI", "CMMS", "ONTF"):
        assert other in done
    # A leaf branch failure blocks nothing downstream (no required consumer).
    assert engine.would_block_downstream(dag.nodes["VDF"], dag) is False
    # The investigation did not hard-fail on an off-spine branch.
    assert engine.investigation_state is not InvestigationState.FAILED


def test_required_branch_incomplete_yields_partial_success_not_false_completed():
    # ONTF is a *required* input into CSTV/AGG: failing it must leave the
    # investigation at PARTIAL_SUCCESS — never a false ANALYSIS_COMPLETED
    # while a required branch is incomplete.
    engine = _new_engine()
    dag = engine.dag
    done: set[str] = set()
    dispatched: list[str] = []

    def engine_map(node):
        dispatched.append(node.id)

    while True:
        batch = engine.dispatch_ready(dag, done, engine_map)
        if not batch:
            break
        for node_id in batch:
            if node_id == "ONTF":
                engine.on_task_finished(node_id, TaskState.FAILED_PERMANENT, error_class=ErrorClass.INPUT_INVALID)
            else:
                engine.on_task_finished(node_id, TaskState.SUCCEEDED)
            done.add(node_id)

    # The other branches still ran to completion.
    for other in ("CMMS", "VISTA", "VCI"):
        assert other in done
    # The fan-in settled the overall verdict as PARTIAL_SUCCESS.
    assert engine.investigation_state is InvestigationState.PARTIAL_SUCCESS
    assert engine.investigation_state is not InvestigationState.ANALYSIS_COMPLETED


def test_cancel_from_preterminal_state_is_legal():
    terminal = {InvestigationState.REPORTED, InvestigationState.FAILED, InvestigationState.CANCELLED}
    for state in InvestigationState:
        if state in terminal or state is InvestigationState.CANCEL_REQUESTED:
            continue
        # Every pre-terminal state accepts the cancel request.
        guard_transition(state, InvestigationState.CANCEL_REQUESTED)
        assert InvestigationState.CANCEL_REQUESTED in ALLOWED_INVESTIGATION_TRANSITIONS[state]
    # ...and the requested cancel confirms to the terminal CANCELLED state.
    guard_transition(InvestigationState.CANCEL_REQUESTED, InvestigationState.CANCELLED)


def test_cancel_from_terminal_state_raises():
    for terminal in (InvestigationState.REPORTED, InvestigationState.FAILED, InvestigationState.CANCELLED):
        with pytest.raises(IllegalTransition):
            guard_transition(terminal, InvestigationState.CANCEL_REQUESTED)
        with pytest.raises(IllegalTransition):
            guard_transition(terminal, InvestigationState.CANCELLED)
