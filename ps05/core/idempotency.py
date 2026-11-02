"""Idempotency keys (TRD § 23.5).

The platform-wide ``idempotency_key`` is a single deterministic SHA-256
over the canonical component list. A re-run that changes **any one** of
``model_checkpoint_version``, ``config_version`` or ``run_generation``
produces a **distinct** key (a new, reproducible run — REO-004) and never
overwrites or merges into the prior generation's results.

An identical key for an already-completed run is a **no-op**: duplicate
delivery collapses to the original row's terminal state.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Mapping, Optional, Union

# Placeholders per § 23.5.
CASE_LEVEL = "CASE-LEVEL"
NA = "N/A"

# Accepts a pre-computed hex digest string, a {artifact_id: sha256} mapping
# (the § 23.5 "deterministic order: sorted by artifact_id" rule), or a
# plain iterable of artifact ids.
DigestLike = Union[str, Iterable[str], Mapping[str, str], None]


def _canonical(artifacts: DigestLike) -> str:
    """Deterministic digest of the input-artifact set."""
    if artifacts is None:
        return ""
    if isinstance(artifacts, str):
        return artifacts
    if isinstance(artifacts, Mapping):
        payload = "|".join(f"{aid}:{artifacts[aid]}" for aid in sorted(artifacts))
    else:  # iterable of artifact ids: sort for determinism
        payload = "|".join(sorted(artifacts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_idempotency_key(
    case_id: str,
    evidence_id: Optional[str],
    engine_id: str,
    stage_id: str,
    input_artifact_digest: DigestLike,
    config_version: str,
    model_checkpoint_version: Optional[str] = None,
    run_generation: int = 0,
) -> str:
    """Build the canonical § 23.5 idempotency key.

    ``run_generation`` is the explicit analysis-generation identifier
    (``GEN-0`` first run, ``GEN-1`` ...). ``evidence_id=None`` renders as
    the ``CASE-LEVEL`` placeholder; a missing ``model_checkpoint_version``
    renders as ``N/A``.
    """
    components = [
        str(case_id),
        str(evidence_id) if evidence_id else CASE_LEVEL,
        str(engine_id),
        str(stage_id),
        _canonical(input_artifact_digest),
        str(config_version),
        str(model_checkpoint_version) if model_checkpoint_version else NA,
        f"GEN-{int(run_generation)}",
    ]
    return hashlib.sha256("|".join(components).encode("utf-8")).hexdigest()


def is_duplicate(existing: set, key: str) -> bool:
    """True when ``key`` was already seen — i.e. this delivery is a no-op."""
    return key in existing
