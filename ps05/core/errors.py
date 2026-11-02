"""Forensic error taxonomy and retry primitives (TRD § 23.10).

The ``ErrorClass`` values are the canonical ``error_class`` codes on
``platform_task_records``. Two hard rules from § 23.10 are encoded in
:attr:`RETRYABLE` / :attr:`PERMANENT`:

* ``INTEGRITY_FAILURE`` and ``AUTHORIZATION_FAILURE`` are **never**
  auto-retried — they permanently block (integrity) or are audit
  failures (authorization).
* ``CANCELLED`` is terminal by design.

``UNKNOWN_INTERNAL_FAILURE`` gets one *bounded* retry, then the DLQ.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ErrorClass(str, Enum):
    INPUT_INVALID = "INPUT_INVALID"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    AUTHORIZATION_FAILURE = "AUTHORIZATION_FAILURE"
    PERMANENT_PARSER_FAILURE = "PERMANENT_PARSER_FAILURE"
    TRANSIENT_STORAGE_FAILURE = "TRANSIENT_STORAGE_FAILURE"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    DEPENDENCY_TIMEOUT = "DEPENDENCY_TIMEOUT"
    RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"
    GPU_OUT_OF_MEMORY = "GPU_OUT_OF_MEMORY"
    WORKER_LOST = "WORKER_LOST"
    MODEL_CHECKPOINT_INVALID = "MODEL_CHECKPOINT_INVALID"
    SCHEMA_VALIDATION_FAILURE = "SCHEMA_VALIDATION_FAILURE"
    CANCELLED = "CANCELLED"
    UNKNOWN_INTERNAL_FAILURE = "UNKNOWN_INTERNAL_FAILURE"
    REQUIRES_EXAMINER_REVIEW = "REQUIRES_EXAMINER_REVIEW"


#: Error classes eligible for automatic retry under § 23.10 policies.
RETRYABLE: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.TRANSIENT_STORAGE_FAILURE,
        ErrorClass.DATABASE_UNAVAILABLE,
        ErrorClass.BROKER_UNAVAILABLE,
        ErrorClass.RESOURCE_EXHAUSTION,
        ErrorClass.GPU_OUT_OF_MEMORY,
        ErrorClass.WORKER_LOST,
        ErrorClass.UNKNOWN_INTERNAL_FAILURE,
    }
)

#: Error classes that are never auto-retried (quarantine / examiner gate).
PERMANENT: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.INPUT_INVALID,
        ErrorClass.INTEGRITY_FAILURE,
        ErrorClass.AUTHORIZATION_FAILURE,
        ErrorClass.PERMANENT_PARSER_FAILURE,
        ErrorClass.DEPENDENCY_TIMEOUT,
        ErrorClass.MODEL_CHECKPOINT_INVALID,
        ErrorClass.SCHEMA_VALIDATION_FAILURE,
        ErrorClass.CANCELLED,
        ErrorClass.REQUIRES_EXAMINER_REVIEW,
    }
)


def classify(error_class: ErrorClass) -> str:
    """``"retryable"`` or ``"permanent"`` (used by the DLQ quarantine gate)."""
    return "retryable" if error_class in RETRYABLE else "permanent"


class ForensicTaskError(Exception):
    """Domain error carrying the § 23.10 ``error_class`` + machine detail code."""

    def __init__(
        self,
        error_class: ErrorClass,
        error_code: str,
        message: str = "",
        *,
        attempt_number: int = 0,
        checkpoint_id: Optional[str] = None,
    ) -> None:
        self.error_class = error_class
        self.error_code = error_code
        self.attempt_number = attempt_number
        self.checkpoint_id = checkpoint_id
        detail = f"{error_class.value} ({error_code})"
        super().__init__(f"{detail}: {message}" if message else detail)


def backoff(
    attempt: int,
    base: float = 1.0,
    cap: float = 300.0,
    jitter: float = 0.5,
) -> float:
    """Exponential backoff with random jitter, capped.

    ``attempt`` is the 1-based failed attempt; delay =
    ``min(cap, base * 2 ** (attempt - 1)) * (1 + uniform(-jitter, +jitter))``.
    Jitter is bounded to ``[0, 1]``.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    delay = min(cap, base * (2 ** (attempt - 1)))
    if jitter > 1.0:
        jitter = 1.0
    return delay * (1.0 + random.uniform(-jitter, jitter))


@dataclass
class RetryPolicy:
    """Per-workload-class retry budget (REO-011).

    ``budget`` is a class-wide shared retry counter so one runaway task
    cannot exhaust another task's retries.
    """

    max_attempts: int = 3
    max_elapsed: float = 3600.0
    budget: int = 100

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.max_elapsed <= 0:
            raise ValueError("max_elapsed must be > 0")
        if self.budget < 1:
            raise ValueError("budget must be >= 1")

    def should_retry(
        self,
        attempt: int,
        error_class: ErrorClass,
        elapsed: float,
    ) -> bool:
        """True when the given failure is eligible for another attempt.

        ``attempt`` is the just-completed (failed) attempt number. A
        permanent error class is never retried, regardless of remaining
        attempts or elapsed time.
        """
        if error_class not in RETRYABLE:
            return False
        if attempt >= self.max_attempts:
            return False
        if elapsed >= self.max_elapsed:
            return False
        return True
