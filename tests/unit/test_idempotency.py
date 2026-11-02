"""Unit tests for ``ps05.core.idempotency``.

Contracted names: build_idempotency_key, is_duplicate.

Invariants asserted:
- Deterministic key: the same logical inputs always produce the same key.
- A different ``model_checkpoint_version`` or ``run_generation`` yields a
  different key (distinct reproducible runs are not conflated).
- ``is_duplicate`` reports True when the same key is observed a second time.

``is_duplicate`` is expected to be stateful (it records which keys have been
claimed by a completed run); the exact registry is an implementation choice
of the lead. The tests below only rely on observable behaviour: a key seen
once is not a duplicate on first sight, and the same key seen again is.
"""

from __future__ import annotations

from ps05.core.idempotency import build_idempotency_key, is_duplicate


def _key(**overrides) -> str:
    """Build a key from a fixed logical input vector plus per-test overrides.

    The exact kwarg names are an implementation choice;
    ``model_checkpoint_version`` and ``run_generation`` are the two axis names
    named in the brief. A stable base payload makes determinism meaningful.
    """
    payload = {
        "case_id": "case-1",
        "task_name": "pre_decode_dual_hash",
        "workload_class": "EVIDENCE_INTAKE",
        "input_manifest_sha256": "ab" * 32,
        "model_checkpoint_version": "cdvf-siglip-v1",
        "run_generation": 1,
    }
    payload.update(overrides)
    return build_idempotency_key(**payload)


def test_key_is_deterministic():
    assert _key() == _key()


def test_key_changes_with_model_checkpoint_version():
    assert _key(model_checkpoint_version="v1") != _key(model_checkpoint_version="v2")


def test_key_changes_with_run_generation():
    assert _key(run_generation=1) != _key(run_generation=2)


def test_is_duplicate_true_on_reobserved_key():
    k = _key(case_id="dup-case")
    # First observation of a fresh key: not a duplicate.
    assert is_duplicate(k) is False
    # Re-observation of the same key: now a duplicate.
    assert is_duplicate(k) is True


def test_is_duplicate_false_for_a_different_run():
    # Distinct reproducible runs are separate keys and must not be
    # cross-reported as duplicates.
    assert is_duplicate(_key(case_id="run-a", run_generation=1)) is False
    assert is_duplicate(_key(case_id="run-b", run_generation=1)) is False
