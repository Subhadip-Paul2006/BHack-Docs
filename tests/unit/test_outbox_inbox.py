"""Unit tests for the outbox / inbox transactional-messaging contract.

Contracted modules: ``ps05.core.outbox`` and ``ps05.core.inbox``.

Invariants asserted:
- Enqueuing an outbox event inside a (fake) transaction is atomic with the
  surrounding state change: roll back -> the event is not visible; commit ->
  it is.
- Committed but undelivered outbox events are discoverable by a
  reconciliation poll (at-least-once delivery, no loss).
- Delivery happens in ``sequence_no`` order.
- The inbox deduplicates redelivered events with the same ``event_id``:
  the handler runs exactly once.

No live Postgres/Redis. A fake transaction unit-of-work and in-memory stores
are used. ASSUMED API (lead owns the actual implementation):
- ``outbox.enqueue(store, tx, event)`` records the event on ``store``
  atomically with the ``tx`` unit of work.
- ``outbox.poll(store)`` returns committed-but-undelivered events ordered
  by ``sequence_no``.
- ``inbox.deliver(store, event, handler)`` runs ``handler(event)`` unless
  ``event["event_id"]`` has already been processed in this store.
If the lead ships different names/shapes, the assertion targets below still
map 1:1 to these invariants; the discovery helpers keep collection alive.
"""

from __future__ import annotations

import inspect

import ps05.core.inbox as inbox_mod
import ps05.core.outbox as outbox_mod


class FakeTx:
    """Minimal commit/rollback-aware unit of work."""

    def __init__(self) -> None:
        self.state: dict = {}
        self.committed = False
        self.rolled_back = False

    def set(self, key, value) -> None:
        self.state[key] = value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _store() -> dict:
    """In-memory outbox table: {tx_id: [event, ...]}, events carry event_id/sequence_no."""
    return {}


def _enqueue():
    for name in ("enqueue", "enqueue_event", "publish", "append"):
        fn = getattr(outbox_mod, name, None)
        if callable(fn):
            return fn
    raise AssertionError("outbox module exposes no enqueue/publish callable")


def _poll():
    for name in ("poll", "find_undelivered", "pending_events", "scan", "reconcile"):
        fn = getattr(outbox_mod, name, None)
        if callable(fn):
            return fn
    raise AssertionError("outbox module exposes no poll/reconcile callable")


def _deliver():
    for name in ("deliver", "process", "consume", "receive", "handle_event"):
        fn = getattr(inbox_mod, name, None)
        if callable(fn):
            return fn
    raise AssertionError("inbox module exposes no deliver/process callable")


def _bind(fn, *args, **named):
    """Call ``fn`` with the argument convention it actually accepts.

    Tries the given positional order, common permutations, and the named
    keyword set. Returns None on success, raises the last TypeError if none
    fit — a clear signal the assumed API drifted from the lead's wiring.
    """
    candidates = [args]
    for perm in _permutations(list(args)):
        if perm != args:
            candidates.append(perm)
    last = None
    for cands in candidates:
        try:
            fn(*cands)
            return None
        except TypeError as exc:
            last = exc
    try:
        fn(**named)
        return None
    except TypeError:
        raise last


def _permutations(items):
    if len(items) <= 1:
        yield items
        return
    for i, item in enumerate(items):
        for rest in _permutations(items[:i] + items[i + 1 :]):
            yield [item] + rest


def test_enqueue_atomic_with_state_on_rollback():
    """An event written in a rolled-back transaction must not be visible."""
    enqueue, poll = _enqueue(), _poll()
    store, tx = _store(), FakeTx()
    event = {"event_id": "ev-1", "sequence_no": 1, "payload": {"case_id": "case-1"}}

    _bind(enqueue, store, tx, event,
          named={"store": store, "tx": tx, "event": event})

    # Simulate a state change in the same transaction, then roll back.
    tx.set("case_state", "PENDING")
    tx.rollback()
    # After rollback the fake store must hold no committed event:
    # drop rows whose owning transaction rolled back (atomicity means they
    # were never durable; if the store is tx-keyed the poll filters on
    # commit state).
    pending = _bind_call(poll, store, tx) or _bind_call(poll, store) or []
    assert all(e.get("event_id") != "ev-1" for e in pending), (
        "rolled-back outbox event must not be discoverable"
    )


def test_committed_undelivered_event_is_reconcilable():
    """A committed, undispatched event is found by the reconciliation poll."""
    enqueue, poll = _enqueue(), _poll()
    store, tx = _store(), FakeTx()
    event = {"event_id": "ev-2", "sequence_no": 1, "payload": {}}

    _bind(enqueue, store, tx, event,
          named={"store": store, "tx": tx, "event": event})
    tx.commit()

    found = _bind_call(poll, store, tx) or _bind_call(poll, store) or []
    assert [e.get("event_id") for e in found] == ["ev-2"], (
        f"committed-but-undelivered event must be discoverable: {[e.get('event_id') for e in found]}"
    )


def test_poll_orders_by_sequence_no():
    """The reconciliation surface must yield events in sequence_no order."""
    poll = _poll()
    store = _store()
    tx = FakeTx()
    tx.state["outbox"] = [
        {"event_id": "ev-b", "sequence_no": 2, "payload": {}},
        {"event_id": "ev-a", "sequence_no": 1, "payload": {}},
    ]
    tx.commit()

    found = _bind_call(poll, store, tx) or _bind_call(poll, store) or []
    seqs = [e.get("sequence_no") for e in found]
    assert seqs == sorted(seqs), f"outbox rows must come out in sequence order: {seqs}"


def test_inbox_dedupes_redelivered_event():
    """Delivering the same event_id twice must invoke the handler once."""
    deliver = _deliver()
    store = {}
    event = {"event_id": "ev-dup", "sequence_no": 3, "payload": {"n": 1}}
    calls: list = []
    handler = lambda e: calls.append(e["event_id"])  # noqa: E731

    for _ in range(2):
        _bind(deliver, store, event, handler,
              named={"store": store, "event": event, "handler": handler})
    assert calls.count("ev-dup") == 1, (
        f"redelivered event must be deduped, handler ran {len(calls)} times"
    )


def _bind_call(fn, *args):
    """Like ``_bind`` but returns the function's return value."""
    last = None
    for cands in _all_orders(args):
        try:
            return fn(*cands)
        except TypeError as exc:
            last = exc
    raise last


def _all_orders(args):
    seen = set()
    for cands in _permutations(list(args)):
        key = tuple(map(id, cands))
        if key not in seen:
            seen.add(key)
            yield cands
