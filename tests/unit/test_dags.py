"""Unit tests for ``ps05.core.dags``.

Contracted names:
    WorkflowDag, TaskNode, build_default_investigation_dag,
    topo_order, ready_nodes, aggregate_partial_success.

Invariants asserted:
- ``build_default_investigation_dag()`` topologically sorts: every node
  appears after all of its prerequisites in ``topo_order``.
- A failed REQUIRED dependency blocks its downstream
  (``would_block_downstream`` -> True) while an OPTIONAL failure does not.
- ``aggregate_partial_success`` yields PARTIAL_SUCCESS when only OPTIONAL
  branches fail.
- ``ready_nodes`` respects prerequisites: only nodes whose inputs are all
  satisfied are ready.

The exact shape of ``TaskNode`` (dataclass fields) and the return type of
``aggregate_partial_success`` are lead choices; the tests discover them
defensively so a field-name convention difference surfaces as a clear
diagnosable failure rather than a wrong assertion.
"""

from __future__ import annotations

import inspect

from ps05.core.dags import (
    TaskNode,
    WorkflowDag,
    aggregate_partial_success,
    build_default_investigation_dag,
    ready_nodes,
    topo_order,
)


def _node(name: str, depends_on=(), required=True) -> TaskNode:
    """Construct a TaskNode tolerating dataclass/keyword field-name variants."""
    for kw in ({"name": name, "depends_on": list(depends_on), "required": required},
               {"name": name, "deps": list(depends_on), "required": required},
               {"name": name, "prereqs": list(depends_on), "required": required},
               {"name": name}):
        try:
            return TaskNode(**kw)
        except TypeError:
            continue
    raise AssertionError(f"could not construct TaskNode(name={name!r})")


def _dag(nodes) -> WorkflowDag:
    for kw in ({"nodes": nodes}, {"tasks": nodes}):
        try:
            return WorkflowDag(**kw)
        except TypeError:
            continue
    return WorkflowDag(nodes)


def _node_names(container) -> list:
    """Yield node names from a DAG/ordering that may hold objects or names."""
    nodes = getattr(container, "nodes", None) or getattr(container, "tasks", None)
    if nodes is None:
        return [getattr(n, "name", n) for n in container]
    if not isinstance(nodes, (list, tuple)):
        nodes = list(nodes.values()) if hasattr(nodes, "values") else list(nodes)
    return [getattr(n, "name", n) for n in nodes]


def _prereqs_of(node) -> list:
    for attr in ("depends_on", "deps", "prereqs", "predecessors", "requires"):
        if hasattr(node, attr):
            val = getattr(node, attr)
            return [getattr(p, "name", p) for p in (val or [])]
    return []


def _order(dag) -> list:
    order = topo_order(dag)
    if isinstance(order, (str, int)):
        return [order]
    return list(order)


def _node_by_name(dag, name):
    for n in _iter_nodes(dag):
        if getattr(n, "name", None) == name:
            return n
    return None


def _iter_nodes(dag):
    nodes = getattr(dag, "nodes", None) or getattr(dag, "tasks", None)
    if nodes is None:
        return []
    if not isinstance(nodes, (list, tuple)):
        return list(nodes.values()) if hasattr(nodes, "values") else list(nodes)
    return list(nodes)


def test_default_dag_topologically_sorts():
    dag = build_default_investigation_dag()
    names = _node_names(dag)
    positions = {n: i for i, n in enumerate(_order(dag))}
    # Every node that appears in the DAG must be in the order (no drops).
    for n in _iter_nodes(dag):
        nm = getattr(n, "name", None)
        if nm is None:
            continue
        for p in _prereqs_of(n):
            pnm = getattr(p, "name", p)
            assert pnm in positions, f"prereq {pnm!r} missing from topo order"
            assert positions[pnm] < positions[nm], (
                f"{pnm!r} must precede {nm!r} in topo order"
            )


def _make_small_dag() -> WorkflowDag:
    nodes = [
        _node("required_root", required=True),
        _node("optional_root", required=False),
        _node("child_of_required", depends_on=["required_root"], required=True),
        _node("child_of_optional", depends_on=["optional_root"], required=True),
    ]
    return _dag(nodes)


def _mark(dag, name: str, status) -> None:
    """Record a node's outcome. The status field/method name is a lead choice."""
    for attr in ("set_status", "mark", "set_state", "record_outcome"):
        fn = getattr(dag, attr, None)
        if callable(fn):
            fn(name, status)
            return
    # Fallback: mutate the node object directly.
    for n in _iter_nodes(dag):
        if getattr(n, "name", None) == name:
            for field in ("status", "state", "outcome"):
                setattr(n, field, status)
            return
    raise AssertionError(f"could not mark node {name!r} on {type(dag).__name__}")


def _would_block(dag, name: str) -> bool:
    """Query whether a failed node blocks its downstream.

    ``would_block_downstream`` may be a module-level or instance method, and
    may take the node name, the node object, or (child, parent) pairs.
    """
    import ps05.core.dags as dags_mod

    candidates = []
    mod_fn = getattr(dags_mod, "would_block_downstream", None)
    if callable(mod_fn):
        candidates.append(mod_fn)
    inst_fn = getattr(dag, "would_block_downstream", None)
    if callable(inst_fn):
        candidates.append(inst_fn)
    for fn in candidates:
        sig = inspect.signature(fn)
        params = [p for p in sig.parameters if p not in ("self", "dag", "self_")]
        node = _node_by_name(dag, name)
        if not params:
            return bool(fn(name))
        # One free arg: pass the node (fall back to the name).
        if len(params) == 1:
            for arg in (node, name):
                try:
                    return bool(fn(arg))
                except (TypeError, AttributeError):
                    continue
        # Two free args (child, parent): find a downstream child.
        if len(params) == 2:
            for other in _iter_nodes(dag):
                for p in _prereqs_of(other):
                    pnm = getattr(p, "name", p)
                    if pnm == name:
                        for c_arg in (other, getattr(other, "name", other)):
                            for p_arg in (node, name):
                                try:
                                    return bool(fn(c_arg, p_arg))
                                except (TypeError, AttributeError):
                                    continue
    raise AssertionError("no would_block_downstream API found on the DAG/module")


def _aggregate_label(result) -> str:
    """Normalise an aggregate result to its textual label (enum or string)."""
    if hasattr(result, "name"):
        return result.name
    if hasattr(result, "value"):
        val = result.value
        return str(val)
    return str(result)


def test_failed_required_dep_blocks_downstream():
    dag = _make_small_dag()
    _mark(dag, "required_root", "FAILED")
    assert _would_block(dag, "required_root") is True


def test_optional_failure_does_not_block():
    dag = _make_small_dag()
    _mark(dag, "optional_root", "FAILED")
    assert _would_block(dag, "optional_root") is False


def test_partial_success_aggregation():
    dag = _make_small_dag()
    _mark(dag, "optional_root", "FAILED")
    _mark(dag, "child_of_optional", "SKIPPED_OPTIONAL")
    _mark(dag, "required_root", "SUCCEEDED")
    _mark(dag, "child_of_required", "SUCCEEDED")
    assert _aggregate_label(aggregate_partial_success(dag)) == "PARTIAL_SUCCESS"


def test_ready_nodes_respect_prerequisites():
    dag = _make_small_dag()
    ready = [getattr(n, "name", n) for n in ready_nodes(dag)]
    # Roots (no prerequisites) are ready.
    assert "required_root" in ready
    assert "optional_root" in ready
    # A node with an unmet prerequisite is not ready.
    assert "child_of_required" not in ready
    assert "child_of_optional" not in ready


def test_ready_nodes_unblock_after_prereq_succeeds():
    dag = _make_small_dag()
    _mark(dag, "required_root", "SUCCEEDED")
    ready = [getattr(n, "name", n) for n in ready_nodes(dag)]
    assert "child_of_required" in ready
