"""Workflow-engine facade (TRD § 23.3, § 23.4, § 23.6.4).

``WorkflowEngine`` is the one shared orchestration surface for
analysis-run planning, task dispatch, terminal-failure aggregation and
investigation-state advancement. It carries **no Celery dependency**:
execution side-effects arrive through the caller-supplied ``engine_map``
callback (in production that callback is the Celery dispatch path);
everything else — readiness, blocking, partial-success aggregation,
state transitions — is pure logic.

Conventions:
* A ``task_id`` in this module is the DAG node id: one durable task
  record per node per analysis generation (§ 23.4.4).
* One ``WorkflowEngine`` instance serves one investigation;
  :meth:`plan_investigation` binds it to a ``(case_id, evidence_id)``.

Semantics (§ 23.3.3):
* a required-prerequisite failure **blocks** single-prerequisite
  downstream tasks — they never silently run against missing inputs;
* fan-in nodes (two or more required prerequisites) tolerate a recorded
  gap: they run against available inputs and report ``PARTIAL_SUCCESS``
  instead of ``SUCCEEDED``/``ANALYSIS_COMPLETED``;
* a spine node's terminal failure (integrity / WORM / registration)
  fails the whole investigation;
* an optional-engine failure never fails the investigation, and never
  yields a false overall success.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set, Tuple

from ps05.core.dags import (
    EDGE_REQUIRED,
    TaskNode,
    WorkflowDag,
    build_default_investigation_dag,
)
from ps05.core.errors import ErrorClass, PERMANENT
from ps05.core.state_machines import (
    InvestigationState,
    TaskState,
    guard_transition,
    record_transition,
)

_TASK_TERMINAL = {
    TaskState.SUCCEEDED,
    TaskState.FAILED_PERMANENT,
    TaskState.DEAD_LETTERED,
    TaskState.CANCELLED,
    TaskState.SKIPPED_OPTIONAL,
}

#: Nodes with multiple required prerequisites that tolerate a recorded
#: gap: they run against available inputs and their node-level verdict is
#: PARTIAL_SUCCESS (never SUCCEEDED) when a required input is missing
#: (§ 23.3.3 rule 3, AGG label "PARTIAL_SUCCESS if any required missing").
DEFAULT_FAN_IN_NODES = ("CSTV", "AGG")

#: The single node whose completion settles the *overall* investigation
#: verdict (§ 23.3.3 rule 4). CSTV is a fan-in in form but its PARTIAL
#: verdict only downgrades the investigation indirectly through AGG.
INVESTIGATION_FAN_IN = ("AGG",)


class WorkflowEngine:
    """Facade owning planning, dispatch, and terminal aggregation."""

    def __init__(self) -> None:
        self._dag: Optional[WorkflowDag] = None
        self._case_id: Optional[str] = None
        self._evidence_id: Optional[str] = None
        self._fan_in_nodes: Set[str] = set(DEFAULT_FAN_IN_NODES)
        self._investigation_fan_in: Set[str] = set(INVESTIGATION_FAN_IN)
        self._investigation_state = InvestigationState.CREATED
        self._completed: Set[str] = set()     # SUCCEEDED / SKIPPED_OPTIONAL
        self._failed: Set[str] = set()         # terminal failures (any kind)
        self._task_results: Dict[str, TaskState] = {}
        self._dispatch_count = 0

    # ------------------------------------------------------------------
    @property
    def investigation_state(self) -> InvestigationState:
        return self._investigation_state

    @property
    def dag(self) -> Optional[WorkflowDag]:
        return self._dag

    def plan_investigation(
        self, case_id: str, evidence_id: str
    ) -> Tuple[InvestigationState, WorkflowDag]:
        """Build the DAG for a fresh analysis generation.

        The plan instance is new per generation — a re-plan replaces the
        engine's state and never mutates a prior plan's terminal states
        (§ 23.3.3 rule 5). The investigation enters ``ANALYSIS_PLANNED``
        (a planning outcome, not a recorded lifecycle move: the
        ingest-spine rows own ``CREATED -> ... -> ANALYSIS_PLANNED``
        (§ 23.4.2), so this step only binds the plan to the engine).
        """
        dag = build_default_investigation_dag()
        self._dag = dag
        self._case_id = case_id
        self._evidence_id = evidence_id
        self._completed = set()
        self._failed = set()
        self._task_results = {}
        self._dispatch_count = 0
        self._investigation_state = InvestigationState.ANALYSIS_PLANNED
        return self._investigation_state, dag

    # ------------------------------------------------------------------
    def dispatch_ready(
        self,
        dag: WorkflowDag,
        done: Set[str],
        engine_map: Callable[[TaskNode], object],
    ) -> List[str]:
        """Dispatch every ready, not-yet-dispatched node via ``engine_map``.

        ``done`` is the caller's externally-tracked completed-node set
        (unioned with this engine's own terminal results). Readiness:
        all required deps are *terminally resolved*; a non-fan-in node
        additionally needs none of its required deps to have failed
        (blocking rule, § 23.3.3 rule 1), while a fan-in node runs
        against available inputs with the gap recorded.

        Returns the node ids dispatched, in topological order. The
        first dispatch moves the investigation ``ANALYSIS_PLANNED ->
        PROCESSING`` (§ 23.4.2 "first task CLAIMED").
        """
        self._require_plan()
        dispatched: List[str] = []
        for node_id in dag.topo_order():
            if node_id in done or node_id in self._completed or node_id in self._failed:
                continue
            node = dag.nodes[node_id]
            if not self._node_ready(node, done):
                continue
            engine_map(node)
            dispatched.append(node_id)
            self._dispatch_count += 1
        if dispatched and self._investigation_state == InvestigationState.ANALYSIS_PLANNED:
            record_transition(
                "admission_scheduler",
                InvestigationState.ANALYSIS_PLANNED,
                InvestigationState.PROCESSING,
                {"case_id": self._case_id, "evidence_id": self._evidence_id, "dispatched": dispatched},
            )
            self._investigation_state = InvestigationState.PROCESSING
        return dispatched

    # ------------------------------------------------------------------
    def on_task_finished(
        self,
        task_id: str,
        state: TaskState,
        error_class: Optional[ErrorClass] = None,
    ) -> InvestigationState:
        """Apply a task's terminal state and advance the investigation.

        Non-terminal states are recorded only; the investigation moves
        on terminal results:

        * spine node (terminal-failure) permanent failure ->
          ``PROCESSING -> FAILED``;
        * branch-engine failure -> downstream single-prerequisite
          consumers are blocked, the fan-in tolerates the gap; the
          verdict is settled at the fan-in;
        * fan-in node completing -> ``PARTIAL_SUCCESS`` when any
          required dep did not SUCCEEDED (never a false
          ``ANALYSIS_COMPLETED``), else ``ANALYSIS_COMPLETED``.
        """
        self._require_plan()
        dag = self._dag
        node = dag.nodes.get(task_id)
        if node is None:
            raise KeyError(f"unknown task/node id {task_id!r} for this investigation")

        # Record the task-level event against the last known state. The
        # intermediate lifecycle (PENDING->...->RUNNING) belongs to the
        # task record's own history (§ 23.4.3); here we validate against
        # the state the engine last saw for this node. Duplicate delivery
        # of an already-recorded terminal state is a no-op (REO-004).
        previous = self._task_results.get(task_id)
        if previous is not None and previous == state:
            return self._investigation_state
        if previous is not None:
            base = previous
        elif state == TaskState.DEAD_LETTERED:
            base = TaskState.FAILED_PERMANENT  # the DLQ reaper hop
        elif state == TaskState.SKIPPED_OPTIONAL:
            base = TaskState.PENDING            # skip is a pre-claim move
        else:
            base = TaskState.RUNNING
        guard_transition(base, state)
        record_transition(
            "workflow_engine",
            base,
            state,
            {"task_id": task_id, "error_class": error_class.value if error_class else None},
        )
        self._task_results[task_id] = state

        if state not in _TASK_TERMINAL:
            return self._investigation_state

        failed_state = state in (TaskState.FAILED_PERMANENT, TaskState.DEAD_LETTERED, TaskState.CANCELLED)
        hard_failure = failed_state and (
            state in (TaskState.FAILED_PERMANENT, TaskState.DEAD_LETTERED) or error_class in PERMANENT
        )

        if state in (TaskState.SUCCEEDED, TaskState.SKIPPED_OPTIONAL):
            self._completed.add(task_id)
        elif hard_failure:
            # Blocked single-prerequisite consumers are enforced at
            # dispatch time by _node_ready (required deps ∩ failed);
            # fan-in nodes stay exempt — they tolerate a recorded gap.
            self._failed.add(task_id)

        if hard_failure and node.is_terminal_failure:
            # Spine failure (integrity/WORM/registration) hard-fails the
            # whole investigation (§ 23.3.3 rule 1, § 19.9 whole-case
            # quarantine on integrity failure).
            if self._investigation_state == InvestigationState.PROCESSING:
                record_transition(
                    "workflow_engine",
                    InvestigationState.PROCESSING,
                    InvestigationState.FAILED,
                    {"task_id": task_id, "error_class": error_class.value if error_class else None},
                )
                self._investigation_state = InvestigationState.FAILED
            return self._investigation_state

        # Only the investigation-level fan-in (AGG) settles the overall
        # verdict: PARTIAL_SUCCESS when any required prerequisite did not
        # SUCCEEDED, ANALYSIS_COMPLETED otherwise (§ 23.3.3 rule 4 —
        # never a false overall success). Intermediate fan-ins (CSTV)
        # record their node-level verdict via aggregate_partial_success
        # but do not move the investigation state.
        if state == TaskState.SUCCEEDED and task_id in self._investigation_fan_in:
            verdict = self._fan_in_verdict(node, dag)
            if self._investigation_state == InvestigationState.PROCESSING:
                record_transition("workflow_engine", InvestigationState.PROCESSING, verdict, {"fan_in_node": task_id})
                self._investigation_state = verdict

        return self._investigation_state

    # ------------------------------------------------------------------
    def would_block_downstream(self, node: TaskNode, dag: WorkflowDag) -> bool:
        """Required-prerequisite block check (§ 23.3.3 rule 1)."""
        return dag.would_block_downstream(node, self._failed)

    # ------------------------------------------------------------------
    def _node_ready(self, node: TaskNode, done: Set[str]) -> bool:
        """All required deps terminally resolved; plus, for non-fan-in
        nodes, no required dep failed (a failure hard-blocks them)."""
        required = set(node.required_deps())
        if not required:
            return True
        resolved = done | self._completed | self._failed
        if not required.issubset(resolved):
            return False
        if node.id in self._fan_in_nodes:
            return True
        return not (required & self._failed)

    def _fan_in_verdict(self, node: TaskNode, dag: WorkflowDag) -> InvestigationState:
        required = set(node.required_deps())
        node_results = {dep: self._task_results.get(dep, TaskState.RUNNING).value for dep in required}
        return dag.aggregate_partial_success(node_results, required)

    @staticmethod
    def _downstream_required(node: TaskNode, dag: WorkflowDag) -> List[str]:
        return [dst for src, dst, kind in dag.edges if src == node.id and kind == EDGE_REQUIRED]

    def _require_plan(self) -> None:
        if self._dag is None:
            raise RuntimeError("plan_investigation must be called first")
