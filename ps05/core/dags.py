"""Workflow DAG model and the default investigation DAG (TRD § 23.3).

Encodes § 23.3.2's refined DAG and the § 23.3.3 invariants:

1. Required prerequisite failure **blocks** downstream — they never
   silently run against missing inputs.
2. Optional engine failure does not fail unrelated branches and does
   not fail the overall investigation when the failed engine is off
   the critical path to ``AGG``.
3. Fan-in nodes report ``PARTIAL_SUCCESS`` (never ``SUCCEEDED``) when
   any *required* input is missing or failed-soft.
4. No false overall success: ``ANALYSIS_COMPLETED`` only when every
   required prerequisite of ``AGG`` resolved to ``SUCCEEDED``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set

from ps05.core.state_machines import InvestigationState

# Edge kinds: "required" — a failed prerequisite blocks downstream;
# "optional" — a failed prerequisite is a recorded gap, not a block.
EDGE_REQUIRED = "required"
EDGE_OPTIONAL = "optional"


@dataclass
class TaskNode:
    """One durable workflow node (a task record, § 23.4.4).

    ``is_terminal_failure`` marks a node whose permanent failure is
    terminal for the investigation as a whole (i.e. it hard-blocks the
    whole fan-in), versus a branch failure that only yields
    ``PARTIAL_SUCCESS``.
    """

    id: str
    engine_id: str
    stage_id: str
    deps: List[str] = field(default_factory=list)
    dep_kinds: Dict[str, str] = field(default_factory=dict)  # dep_id -> "required"/"optional"
    is_terminal_failure: bool = False

    def dep_kind(self, dep_id: str) -> str:
        return self.dep_kinds.get(dep_id, EDGE_REQUIRED)

    def required_deps(self) -> List[str]:
        return [d for d in self.deps if self.dep_kinds.get(d, EDGE_REQUIRED) == EDGE_REQUIRED]

    def optional_deps(self) -> List[str]:
        return [d for d in self.deps if self.dep_kinds.get(d, EDGE_REQUIRED) == EDGE_OPTIONAL]


class WorkflowDag:
    """Directed-acyclic task graph with dependency-kind tracking."""

    def __init__(self) -> None:
        self.nodes: Dict[str, TaskNode] = {}
        self.edges: List[tuple] = []  # (from_id, to_id, kind)

    def add_node(
        self,
        node_id: str,
        engine_id: str,
        stage_id: str,
        is_terminal_failure: bool = False,
    ) -> TaskNode:
        node = TaskNode(id=node_id, engine_id=engine_id, stage_id=stage_id, is_terminal_failure=is_terminal_failure)
        self.nodes[node_id] = node
        return node

    def add_edge(self, from_id: str, to_id: str, kind: str = EDGE_REQUIRED) -> None:
        if from_id not in self.nodes or to_id not in self.nodes:
            raise ValueError(f"unknown node in edge {from_id} -> {to_id}")
        if from_id == to_id:
            raise ValueError("self-edge is illegal in a DAG")
        self.edges.append((from_id, to_id, kind))
        target = self.nodes[to_id]
        target.deps.append(from_id)
        target.dep_kinds[from_id] = kind

    def topo_order(self) -> List[str]:
        """Kahn topological sort; raises on cycle (DAG invariant)."""
        in_deg: Dict[str, int] = {nid: 0 for nid in self.nodes}
        children: Dict[str, List[str]] = {nid: [] for nid in self.nodes}
        for src, dst, _ in self.edges:
            in_deg[dst] += 1
            children[src].append(dst)
        queue = [nid for nid, deg in in_deg.items() if deg == 0]
        order: List[str] = []
        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for child in children[nid]:
                in_deg[child] -= 1
                if in_deg[child] == 0:
                    queue.append(child)
        if len(order) != len(self.nodes):
            remaining = set(self.nodes) - set(order)
            raise ValueError(f"cycle detected among {sorted(remaining)}")
        return order

    def ready_nodes(self, completed: Set[str]) -> List[str]:
        """Nodes whose *required* deps are all in ``completed``.

        Optional deps are not gating for readiness — they only gate the
        fan-in's *result quality* (PARTIAL_SUCCESS vs ANALYSIS_COMPLETED),
        which :meth:`aggregate_partial_success` handles.
        """
        ready = []
        for nid, node in self.nodes.items():
            if nid in completed:
                continue
            if all(d in completed for d in node.required_deps()):
                ready.append(nid)
        return ready

    def fan_in(self, node_id: str) -> List[str]:
        """All immediate predecessors of ``node_id`` (any edge kind)."""
        return list(self.nodes[node_id].deps)

    def would_block_downstream(self, node: TaskNode, failed_required: Set[str]) -> bool:
        """True when ``node``'s terminal failure hard-blocks required downstream.

        § 23.3.3 rule 1: a *required* prerequisite failure blocks
        downstream tasks — they never silently run against missing inputs.
        A failure blocks when (a) this node is a recorded failed required
        prerequisite, and (b) some direct downstream node is still an
        uncompleted required consumer of it.
        """
        if node.id not in failed_required:
            return False
        for src, dst, kind in self.edges:
            if src == node.id and kind == EDGE_REQUIRED and dst not in failed_required:
                return True
        return False

    def aggregate_partial_success(
        self,
        node_results: Dict[str, str],
        required_deps: Set[str],
    ) -> InvestigationState:
        """Fan-in verdict per § 23.3.3.

        ``node_results`` maps node id -> terminal task state string
        (``"SUCCEEDED"`` / a failed terminal). ``required_deps`` is the
        set of node ids that are required prerequisites of the fan-in.

        * ``PARTIAL_SUCCESS`` when any *required* dep is missing or failed
          (i.e. not all required resolved to SUCCEEDED).
        * ``ANALYSIS_COMPLETED`` only when **every** required dep
          resolved to ``SUCCEEDED`` — never a blanket "engines failed but
          case completed OK".
        """
        all_required_succeeded = all(
            node_results.get(dep) == "SUCCEEDED" for dep in required_deps
        )
        if all_required_succeeded and required_deps:
            return InvestigationState.ANALYSIS_COMPLETED
        return InvestigationState.PARTIAL_SUCCESS


def build_default_investigation_dag() -> WorkflowDag:
    """Encode the § 23.3.2 refined investigation DAG.

    Required (blocking) vs optional (branch) edges are assigned to the
    engine branches that the § 23.3.2 refinement calls out:

    * the spine (Upload -> Hash -> WORM -> Register -> Triage) is all
      required; a spine failure terminal-fails the whole investigation.
    * parallel engine fan-out from TRIAGE: each engine branch is an
      *optional* prerequisite of the fan-in — a single engine's failure
      must not fail unrelated branches (§ 23.3.3 rule 2) — but each
      feeds a *required* input into CSTV / AGG where the TRD marks it so.
    * VISTA->AFB->AURAS, VISTA->VCI, VISTA->VDF, AIF->IMLF, {CMMS,AIF?,
      AURAS,VISTA,ONTF}->CSTV, all engines+CSTV->AGG->CDVF->Evidence
      Graph->Human Review->UFREDF per § 23.3.2.
    """
    dag = WorkflowDag()
    for nid in (
        "UPLOAD",
        "PREHASH",
        "WORMC",
        "EVIDREG",
        "TRIAGE",
        "CMMS",
        "AIF",
        "AURAS",
        "VISTA",
        "ONTF",
        "CCHDF",
        "AFB",
        "VCI",
        "VDF",
        "IMLF",
        "CSTV",
        "AGG",
        "CDVF",
        "EGRAPH",
        "REVIEW",
        "UFREDF",
    ):
        dag.add_node(nid, engine_id=nid, stage_id=nid, is_terminal_failure=(nid in {"UPLOAD", "PREHASH", "WORMC", "EVIDREG"}))

    def _req(src: str, dst: str) -> None:
        dag.add_edge(src, dst, EDGE_REQUIRED)

    def _opt(src: str, dst: str) -> None:
        dag.add_edge(src, dst, EDGE_OPTIONAL)

    # Spine — required, terminal-fails the investigation.
    _req("UPLOAD", "PREHASH")
    _req("PREHASH", "WORMC")
    _req("WORMC", "EVIDREG")
    _req("EVIDREG", "TRIAGE")

    # Fan-out to the parallel engines (each branch is an optional prereq of
    # the overall fan-in so one engine's failure does not fail the others).
    for eng in ("CMMS", "AIF", "AURAS", "VISTA", "ONTF", "CCHDF"):
        _req("TRIAGE", eng)

    # Branch refinements (§ 23.3.2 notes).
    _req("VISTA", "AFB")
    _opt("AFB", "AURAS")          # soft audio-stream dependency
    _req("VISTA", "VCI")
    _req("VISTA", "VDF")
    _req("AIF", "IMLF")

    # CSTV fan-in (§ 17): required from all four source engines.
    for eng in ("CMMS", "AURAS", "VISTA", "ONTF"):
        _req(eng, "CSTV")

    # AGG fan-in: required from the six top-level engines + CSTV.
    # CCHDF feeds AGG as a required input (per the § 23.3.2 AGG label
    # "required: CMMS + AIF + AURAS + VISTA + ONTF + CCHDFA + CSTV").
    for eng in ("CMMS", "AIF", "AURAS", "VISTA", "ONTF", "CSTV"):
        _req(eng, "AGG")
    _req("CCHDF", "AGG")

    # Terminal spine to the report.
    _req("AGG", "CDVF")
    _req("CDVF", "EGRAPH")
    _req("EGRAPH", "REVIEW")
    _req("REVIEW", "UFREDF")

    return dag
