"""CCHDF — CCHDF/PSEA orchestrator (TRD § 22 / § 23.15 ``cchdf_triage`` row,
§ 23.17 / REO-014 two-re-open contract).

The orchestrator is a *durable workflow task*, not an unbounded inline
orchestration function: it is produced by ``make_task(CCHDF_DESCRIPTOR,
orchestrator)`` (see :func:`build_cchdf_stage_task`) and registered by the
celery glue exactly as the other engines are —
``registry.register(CCHDF_DESCRIPTOR, CCHDFOrchestrator())`` (which
``ps05.engines.registry.build_default_registry`` already does).

Each stage RE-OPENS its own reader over the same immutable WORM object:

* stage 1 (universal triage scanner) gets ``reader_a = handle.open_reader()``;
* stage 2 (modality adapter) gets a *separate*
  ``reader_b = handle.open_reader()``.

Two distinct reader instances, no shared file-pointer state, no exhaustion /
partial-read hazard between the scanner and the modality adapter (REO-014).
Acceptance: the re-opening is observable — running ``run_investigation``
creates two distinct ``BoundedStreamReader`` instances; a test can assert
``orchestrator.scanner_reader is not orchestrator.modality_reader``.

One adapter failure isolates to that adapter's branch only: a modality-
adapter failure is recorded as an inert ``QUARANTINE`` branch finding and the
investigation completes with the scanner's findings intact; the scanner
branch itself still propagates its typed error so the § 23.10 posture holds.
``INPUT_INVALID`` / ``PERMANENT_PARSER_FAILURE`` are *never* retried — the
descriptor's ``retry_error_classes`` lists only
``TRANSIENT_STORAGE_FAILURE``, so the task factory routes every permanent
class to FAILED / DLQ, and a re-queued run re-records the identical
quarantined branch record.

Schema-only pass: emitted findings carry the full provenance record
(``model_version``, ``config_version``, ``input_artifact_hash``,
``analysis_run_id``); no benchmark numbers, no invented metrics.  Unsupported
/ hostile input produces an ``INPUT_INVALID`` posture (scanner raise) plus a
quarantine finding — the input is never executed as code, no macro path runs.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Mapping

from .base import (
    BaseAdapter,
    EngineExecutionError,
    EngineTask,
    Finding,
    FindingStatus,
    InputInvalidError,
    PermanentParserFailure,
    RunContext,
    make_task,
)
from .registry import CCHDF_DESCRIPTOR
from .triage import TriageAdapter

# Bounded-read budgets (§ 23.17 defensive-parse profile of the cchdf_triage
# matrix row; ``timeout_s=900`` of the canonical descriptor drives the guard).
_CCHDF_HEAD_WINDOW = 4096      # bounded head probe per stage (never whole-file)
_CCHDF_TIMEOUT_S = 900.0

# Carrier class (triage step 9 routing) -> modality adapter.  Only NETWORK
# has a raw-executor in this pass (ONTF); the others record a schema-only
# data gap, never a fabricated model result.
_MODALITY_ROUTE_CARRIERS: Mapping[str, str] = {"NETWORK": "ontf"}


class CCHDFOrchestrator(BaseAdapter):
    """Two-stage CCHDF/PSEA orchestrator: universal triage scanner, then a
    modality adapter routed by the triage profile (REO-013 owner: the
    ``cchdf_triage`` stage; each stage runs over its own re-opened reader,
    § 23.17 / REO-014)."""

    engine_id = "cchdf"
    stage_id = "cchdf_triage"

    def __init__(
        self,
        model_version: str = CCHDF_DESCRIPTOR.model_version,
        config_version: str = CCHDF_DESCRIPTOR.config_version,
        triage_adapter: BaseAdapter | None = None,
    ) -> None:
        super().__init__(model_version, config_version)
        self._triage = triage_adapter or TriageAdapter()
        self._reader_a: Any = None  # scanner stage's own re-opened reader
        self._reader_b: Any = None  # modality stage's own re-opened reader

    # -- two-re-open observability (REO-014 acceptance) --------------------
    @property
    def scanner_reader(self) -> Any:
        """The distinct reader instance stage 1 (scanner) ran over."""
        return self._reader_a

    @property
    def modality_reader(self) -> Any:
        """The distinct reader instance stage 2 (modality) ran over."""
        return self._reader_b

    # ------------------------------------------------------------------ run
    def run(self, handle: Any, ctx: RunContext) -> list[Finding]:
        """BaseAdapter entry point — the durable task factory calls this."""
        return self.run_investigation(handle, ctx)

    def run_investigation(self, handle: Any, ctx: RunContext) -> list[Finding]:
        """Run the two-stage CCHDF investigation over independently re-opened
        readers.

        * (a) ``reader_a = handle.open_reader()`` — the universal triage
          scanner's own cursor.
        * (b) ``reader_b = handle.open_reader()`` — a *separate* cursor for
          the modality adapter (``reader_a is not reader_b``; each closes its
          own reader and touches no other stage's pointer state).
        """
        deadline = time.monotonic() + _CCHDF_TIMEOUT_S

        # -- Stage 1: universal triage scanner, over its own reader_a ------
        reader_a = handle.open_reader()
        self._reader_a = reader_a
        scanner_findings: list[Finding] = []
        try:
            profile, scanner_findings = self._run_scanner(reader_a, handle, ctx, deadline)
        finally:
            reader_a.close()
        # The scanner's typed error (InputInvalidError / PermanentParser-
        # Failure) already escaped above: INPUT_INVALID / PERMANENT_PARSER_
        # FAILURE are never retried, and there is no modality stage to run
        # without a triage profile.

        if profile["quarantined"]:
            # The scanner recorded an inert quarantine (unsupported /
            # hostile head; § 22.12).  The input is never executed — no
            # modality branch runs; the investigation completes with the
            # scanner's quarantined record (INPUT_INVALID posture, noted).
            scanner_findings.append(self.make_finding(
                ctx, "cchdf_modality_skipped",
                {"carrier": profile["carrier"],
                 "reason": "scanner input quarantined inert",
                 "error_class": "INPUT_INVALID",
                 "inert": True},
                status=FindingStatus.QUARANTINE,
            ))
            return scanner_findings

        # -- Stage 2: modality adapter, over its separate reader_b ---------
        reader_b = handle.open_reader()
        self._reader_b = reader_b
        try:
            modality_findings: list[Finding]
            try:
                modality_findings = self._run_modality(reader_b, handle, ctx, profile)
            except EngineExecutionError as exc:
                # One adapter failure isolates to that adapter's branch:
                # the scanner's findings are preserved and this branch
                # records an inert quarantined failure; permanent classes
                # (INPUT_INVALID / PERMANENT_PARSER_FAILURE) are never
                # retried — a re-queued run re-records the same record.
                modality_findings = [self.make_finding(
                    ctx, "modality_adapter_quarantined",
                    {"branch": "modality", "carrier": profile["carrier"],
                     "error_class": exc.error_class.value,
                     "message": str(exc), "inert": True},
                    status=FindingStatus.QUARANTINE,
                )]
            finally:
                reader_b.close()
            scanner_findings.extend(modality_findings)
            return scanner_findings
        except Exception:
            reader_b.close()
            raise

    # ---------------------------------------------------------- stage 1
    def _run_scanner(
        self,
        reader_a: Any,
        handle: Any,
        ctx: RunContext,
        deadline: float,
    ) -> tuple[dict[str, Any], list[Finding]]:
        """Scanner stage: a bounded head probe on ``reader_a`` plus the
        universal 10-step TriageAdapter pass (which re-opens its own
        independent readers too — REO-014; this stage consumes its
        findings, never its cursor).  Returns ``(profile, findings)``."""
        head = reader_a.read(_CCHDF_HEAD_WINDOW)
        head_chunks = _chunk_ids_for(handle, 0, len(head))
        scanner_findings = [
            self.make_finding(
                ctx, "cchdf_scanner_head_probe",
                {"head_sha256": hashlib.sha256(head).hexdigest(),
                 "head_bytes": len(head),
                 "chunk_ids": head_chunks,
                 "declared_signature": ctx.declared_signature},
                status=FindingStatus.OK if head else FindingStatus.QUARANTINE,
            )
        ]
        scanner_findings.extend(self._triage.run(handle, ctx))
        if time.monotonic() > deadline:
            raise PermanentParserFailure(
                f"CCHDF: scanner stage exceeded its {_CCHDF_TIMEOUT_S:.0f}s defensive-parse budget"
            )
        profile = _extract_profile(scanner_findings)
        return profile, scanner_findings

    # ---------------------------------------------------------- stage 2
    def _run_modality(
        self,
        reader_b: Any,
        handle: Any,
        ctx: RunContext,
        profile: dict[str, Any],
    ) -> list[Finding]:
        """Modality stage: a bounded head probe on ``reader_b`` (its own
        re-opened cursor), then route to the carrier's modality adapter by
        the triage profile.  Only ``NETWORK`` owns a raw-executor in this
        pass (ONTF); every other carrier records a schema-only data gap."""
        head = reader_b.read(_CCHDF_HEAD_WINDOW)
        findings = [
            self.make_finding(
                ctx, "cchdf_modality_probe",
                {"carrier": profile["carrier"],
                 "head_sha256": hashlib.sha256(head).hexdigest(),
                 "head_bytes": len(head),
                 "chunk_ids": _chunk_ids_for(handle, 0, len(head))},
            )
        ]
        engine_id = _MODALITY_ROUTE_CARRIERS.get(profile["carrier"])
        if engine_id is None:
            findings.append(self.make_finding(
                ctx, "modality_model_unimplemented",
                {"carrier": profile["carrier"],
                 "note": "schema-only: modality model stub — no metric emitted"},
                status=FindingStatus.DATA_GAP,
            ))
            return findings
        adapter = _build_modality_adapter(engine_id)
        # The routed adapter re-opens its own readers too (independent;
        # REO-014): we consume its emitted findings, never its cursor.
        findings.extend(adapter.run(handle, ctx))
        return findings


def _extract_profile(scanner_findings: list[Finding]) -> dict[str, Any]:
    """The routing profile the scanner produced: carrier class (triage step
    9 ``adapter_routing`` finding) and whether the scanner quarantined the
    input inert (``input_quarantined`` / quarantined gate findings)."""
    quarantined = any(f.status is FindingStatus.QUARANTINE for f in scanner_findings)
    carrier = "UNKNOWN"
    for f in scanner_findings:
        if f.category == "adapter_routing":
            carrier = str(f.payload.get("carrier", "UNKNOWN"))
    return {"carrier": carrier, "quarantined": quarantined}


def _build_modality_adapter(engine_id: str) -> BaseAdapter:
    """The routed modality adapter for a triage carrier (schema-only in
    this pass: NETWORK → ONTF; future carriers are registered the same
    way and never re-register an owned raw-executor — REO-013)."""
    from . import ontf  # local import: registry-level wiring stays acyclic

    if engine_id == "ontf":
        return ontf.ONTFAdapter()
    raise InputInvalidError(f"CCHDF: no modality adapter registered for {engine_id!r}")


def _chunk_ids_for(handle: Any, lo: int, hi: int) -> list[str]:
    """Deterministic chunk-ID span the bounded read covered, keyed to the
    handle's ``ChunkManifest`` (§ 23.8 digest-verification) — best effort:
    handles without the accessor record an empty span, never invent IDs."""
    chunk_ids_in_range = getattr(handle, "chunk_ids_in_range", None)
    if callable(chunk_ids_in_range):
        return list(chunk_ids_in_range(lo, hi))
    return []


# ---------------------------------------------------------------------------
# Durable CCHDF stage task (TRD § 23.3 / REO-015)
# ---------------------------------------------------------------------------
def build_cchdf_stage_task(
    orchestrator: CCHDFOrchestrator | None = None,
    *,
    handle_resolver: Any = None,
    finding_sink: Any = None,
    checkpoint_sink: Any = None,
) -> EngineTask:
    """The durable Celery task for the CCHDF stage, produced via
    ``make_task(desc, orchestrator)`` over the canonical ``CCHDF_DESCRIPTOR``
    (queue ``DEFENSIVE_PARSER_SANDBOX``, workload class ``cchdf_triage``,
    profile ``CCHDF_v1``; the registry is the single owner of that § 23.15
    row).  The app's celery glue registers it as ``run_cchdf_stage`` on the
    routed queue and pairs it with ``registry.register(CCHDF_DESCRIPTOR,
    CCHDFOrchestrator())`` — the same form the other engines use."""
    return make_task(
        CCHDF_DESCRIPTOR,
        orchestrator or CCHDFOrchestrator(),
        task_name="run_cchdf_stage",
        handle_resolver=handle_resolver,
        finding_sink=finding_sink,
        checkpoint_sink=checkpoint_sink,
    )


#: The out-of-box durable task instance (the glue may build its own with
#: app-owned resolvers / sinks via :func:`build_cchdf_stage_task`).
run_cchdf_stage: EngineTask = build_cchdf_stage_task()
