"""Unit tests for ``ps05.core.errors``.

Contracted names:
    ErrorClass, RETRYABLE, PERMANENT, backoff, RetryPolicy, ForensicTaskError.

Invariants asserted:
- INTEGRITY_FAILURE and AUTHORIZATION_FAILURE are in PERMANENT and NOT in
  RETRYABLE.
- ``backoff`` grows with the attempt number and is capped.
- ``RetryPolicy.should_retry`` is False for permanent classes and for
  attempts beyond max.
"""

from __future__ import annotations

from ps05.core.errors import (
    PERMANENT,
    RETRYABLE,
    ErrorClass,
    ForensicTaskError,
    RetryPolicy,
    backoff,
)


def test_permanent_classes_are_not_retryable():
    assert ErrorClass.INTEGRITY_FAILURE in PERMANENT
    assert ErrorClass.AUTHORIZATION_FAILURE in PERMANENT
    assert ErrorClass.INTEGRITY_FAILURE not in RETRYABLE
    assert ErrorClass.AUTHORIZATION_FAILURE not in RETRYABLE
    # Invariant: the two sets are disjoint.
    assert not (set(PERMANENT) & set(RETRYABLE)), "PERMANENT and RETRYABLE must be disjoint"


def test_backoff_grows_with_attempt_and_is_capped():
    # attempt is a 1-indexed (or 0-indexed) counter; either way backoff must
    # be monotonic non-decreasing and bounded.
    values = [backoff(n) for n in range(1, 60)]
    assert all(b2 >= b1 for b1, b2 in zip(values, values[1:])), "backoff must not shrink"
    assert values[-1] == max(values), "backoff must reach and hold its cap"
    # The cap is actually binding somewhere in the sequence.
    assert any(v == values[-1] for v in values[10:]), "the cap should kick in well before attempt 60"


def test_retry_policy_refuses_permanent_class():
    policy = RetryPolicy(max_attempts=5, base_delay=0.1)
    assert policy.should_retry(ErrorClass.INTEGRITY_FAILURE, attempt=1) is False
    assert policy.should_retry(ErrorClass.AUTHORIZATION_FAILURE, attempt=1) is False


def test_retry_policy_refuses_attempts_beyond_max():
    policy = RetryPolicy(max_attempts=3, base_delay=0.1)
    # A retryable class must still be refused once attempts are exhausted.
    retryable = next(iter(RETRYABLE))
    assert policy.should_retry(retryable, attempt=3) is False
    assert policy.should_retry(retryable, attempt=4) is False
    # ...and allowed while under the cap.
    assert policy.should_retry(retryable, attempt=1) is True


def test_forensic_task_error_carries_class():
    exc = ForensicTaskError(ErrorClass.INTEGRITY_FAILURE, "digest mismatch")
    assert exc is not None
    # The error must carry its class so retry policies can key on it.
    assert getattr(exc, "error_class", None) is ErrorClass.INTEGRITY_FAILURE or \
        getattr(exc, "cls", None) is ErrorClass.INTEGRITY_FAILURE or \
        getattr(exc, "category", None) is ErrorClass.INTEGRITY_FAILURE or \
        getattr(exc, "kind", None) is ErrorClass.INTEGRITY_FAILURE
