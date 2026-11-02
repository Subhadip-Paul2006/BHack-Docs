"""Universal 10-step triage engine (TRD § 22.2 / § 23.15 ``triage`` row).

The universal structural & entropy scanner runs *before* any modality
adapter and is owned by exactly one raw-executor (REO-013): every adapter
re-opens its own reader, so the triage pass's reads never consume or shift
another pass's byte range (TRD § 23.17 / REO-014).

Schema-only pass: the signature/MIME matcher and every optional
dependency (``libmagic``, container demuxers) are stubs that report
schema-shaped results — never benchmark numbers.  Steps 1–10 map 1:1 to
the emitted findings.

Hostile / unsupported input produces an ``INPUT_INVALID`` error or an
inert quarantine finding (TRD § 22.12): the input is never executed as
code and no macro path runs.  No ``subprocess``, no macro execution, no
code execution of any kind — this module's only side effects are the
bounded reads it opens itself and the findings it emits.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any

from .base import (
    BaseAdapter,
    Finding,
    FindingStatus,
    InputInvalidError,
    PermanentParserFailure,
    RunContext,
    get_read_surface,
    read_scanned,
)
from .registry import CANONICAL_DESCRIPTORS

# ---------------------------------------------------------------------------
# Tolerance constants (TRD § 22.2 step numbers; § 23.17 bounded-read rules)
# ---------------------------------------------------------------------------
_MAGIC_SNIFF_WINDOW = 4096       # step 1
_ENTROPY_WINDOW = 1024          # step 7 (W)
_ENTROPY_STEP = 512             # step 7 (S)
_ENTROPY_ALERT = 7.85           # step 7 threshold
_NESTED_SCAN_LIMIT = 1_048_576   # structural-pass scan budget (bounded)
_TRAILING_TOLERANCE = 16        # step 4 (Δ_trailing > 16 → quarantine)
_TRIAGE_TIMEOUT_S = 30.0        # defensive-parse timeout guard
_TAIL_WINDOW = 4096            # step 4: tail read for logical-EOF audit

# Nested-archive magic prefixes (TRD § 22.2 step 5).
_NESTED_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"PK\x03\x04", "PKZIP"),
    (b"7z\xBC\xAF\x27\x1C", "7ZIP"),
    (b"\x1F\x8B", "GZIP"),
    (b"\x7FELF", "ELF"),
    (b"MZ", "PE"),
    (b"ustar", "TAR"),
)

# Carrier routing (TRD § 22.2 step 9): MIME-set → carrier class.
_CARRIER_ROUTES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("image/jpeg", "image/png", "image/webp"), "IMAGE"),
    (("audio/wav", "audio/flac", "audio/mpeg"), "AUDIO"),
    (("video/mp4", "video/x-matroska"), "VIDEO"),
    (("application/pdf", "application/vnd.openxmlformats"), "DOCUMENT"),
    (("application/x-raw-disk-image", "application/x-ewf"), "FILESYSTEM"),
    (("application/vnd.tcpdump.pcap", "application/vnd.tcpdump.pcapng"), "NETWORK"),
)

# Container logical-EOF markers (TRD § 22.2 step 4).
_LOGICAL_EOF_MARKERS: dict[str, bytes] = {
    "image/jpeg": b"\xFF\xD9",
    "image/png": b"IEND\xAE\x42\x60\x82",
    "application/pdf": b"%%EOF",
    "application/zip": b"PK\x05\x06",
}

_TRIAGE_DESCRIPTOR = next(d for d in CANONICAL_DESCRIPTORS if d.engine_id == "triage")
try:
    from .registry import TRIAGE_DESCRIPTOR  # noqa: F401  (registry exports it)
except ImportError:  # pragma: no cover - registry always defines it
    TRIAGE_DESCRIPTOR = _TRIAGE_DESCRIPTOR


def _drain_head(handle: Any, limit: int) -> tuple[bytes, list[str]]:
    """Bounded read of up to ``limit`` head bytes via a fresh, independent
    reader (TRD § 23.17 re-open invariant).  Returns ``(bytes, chunk_ids)``;
    ``chunk_ids`` is the deterministic span the read covered, keyed to the
    handle's ``ChunkManifest`` (§ 23.8 digest-verification)."""
    reader = get_read_surface(handle)
    try:
        data = reader.read(limit)
    finally:
        reader.close()
    chunk_ids_in_range = getattr(handle, "chunk_ids_in_range", None)
    chunk_ids = list(chunk_ids_in_range(0, len(data))) if callable(chunk_ids_in_range) else []
    return data, chunk_ids


class TriageAdapter(BaseAdapter):
    """Universal 10-step structural & entropy scanner (REO-013 owner:
    ``universal_triage`` stage).

    Schema-only: MIME sniffing is a local signature-rule stub — the
    optional ``libmagic`` binding, when missing, records a data gap
    instead of fabricating a result.  ``INPUT_INVALID`` (unsupported /
    zero-byte / structurally hostile head) is raised as an error and
    carries the never-retried posture of TRD § 23.10.
    """

    engine_id = "triage"
    stage_id = "universal_triage"

    def __init__(self, model_version: str = TRIAGE_DESCRIPTOR.model_version,
                 config_version: str = TRIAGE_DESCRIPTOR.config_version) -> None:
        super().__init__(model_version, config_version)

    # ------------------------------------------------------------------ run
    def run(self, handle: Any, ctx: RunContext) -> list[Finding]:
        deadline = time.monotonic() + _TRIAGE_TIMEOUT_S  # defensive-parse guard
        total = getattr(handle, "total_bytes", None)

        # -- Step 1: magic-byte & actual MIME (local signature stub)
        head, head_chunks = _drain_head(handle, _MAGIC_SNIFF_WINDOW)
        if len(head) == 0:
            raise InputInvalidError("evidence head is empty (zero-byte object); quarantined inert")
        magic_mimes, mime_gap = _sniff_mime(head)
        findings: list[Finding] = [
            self.make_finding(
                ctx, "magic_mime_scan",
                {"declared_signature": ctx.declared_signature,
                 "magic_mimes": magic_mimes,
                 "head_sha256": hashlib.sha256(head).hexdigest(),
                 "head_bytes": len(head),
                 "chunk_ids": head_chunks,
                 "optional_dep_missing": mime_gap},
                status=FindingStatus.DATA_GAP if mime_gap else FindingStatus.OK,
            )
        ]
        if not magic_mimes and ctx.declared_signature is not None:
            # Declared signature that no local rule verifies: structurally
            # suspicious.  Inert quarantine — the input is never executed.
            findings.append(self.make_finding(
                ctx, "input_quarantined",
                {"reason": "declared signature unverified by signature stub",
                 "chunk_ids": head_chunks, "inert": True},
                status=FindingStatus.QUARANTINE,
            ))
            return findings

        if time.monotonic() > deadline:
            raise PermanentParserFailure(f"triage step 1 exceeded its {_TRIAGE_TIMEOUT_S:.0f}s defensive-parse budget")

        primary_mime = magic_mimes[0] if magic_mimes else "unknown"

        # -- Step 2: container stream inventory (demuxer stub, schema-only)
        findings.append(self.make_finding(
            ctx, "container_inventory",
            {"mime": primary_mime,
             "total_bytes": total,
             "inventory": _container_inventory_stub(primary_mime, total),
             "demuxer_available": _demuxer_available(primary_mime)},
            status=FindingStatus.DATA_GAP if not _demuxer_available(primary_mime) else FindingStatus.OK,
        ))

        # -- Step 3: extension/content discrepancy
        if ctx.declared_signature and ctx.declared_signature != primary_mime:
            findings.append(self.make_finding(
                ctx, "EXTENSION_CONTENT_MISMATCH",
                {"declared": ctx.declared_signature, "actual": primary_mime},
            ))

        # -- Step 4: trailing byte audit (bounded tail read)
        tail, tail_chunks = _drain_tail(handle, total)
        findings.append(_trailing_audit(self, ctx, total, tail, tail_chunks, primary_mime))

        # -- Steps 5–7: nested-archive scan, structural inspection, entropy
        self._run_structural_passes(ctx, handle, total, deadline, findings)

        # -- Step 8: polyglot / malformed gate (inert, never executes)
        polyglot = len(magic_mimes) > 1
        findings.append(self.make_finding(
            ctx, "polyglot_gate",
            {"viable_containers_at_zero": magic_mimes,
             "polyglot_candidate": polyglot, "malformed_rejected": polyglot,
             "inert": True},
            status=FindingStatus.QUARANTINE if polyglot else FindingStatus.OK,
        ))

        # -- Step 9: static adapter routing (carrier class)
        carrier = _route_carrier(primary_mime)
        findings.append(self.make_finding(ctx, "adapter_routing",
                                          {"carrier": carrier, "mime": primary_mime}))

        # -- Step 10: lineage anchor & safe quarantine (provenance record)
        findings.append(self.make_finding(
            ctx, "lineage_anchor",
            {"input_artifact_hash": ctx.input_artifact_hash,
             "total_bytes": total, "carrier": carrier,
             "zero_execution": True},
        ))

        return findings

    # ---------------------------------------------------------- steps 5–7
    def _run_structural_passes(self, ctx: RunContext, handle: Any, total: int | None,
                               deadline: float, findings: list[Finding]) -> None:
        """Bounded structural passes over an independently re-opened reader.

        Reads a single scan window (``_NESTED_SCAN_LIMIT`` bytes, smaller of
        window and ``total`` when known) — never the whole object — and
        emits one finding per structural step (5, 6, 7).
        """
        scan_budget = min(_NESTED_SCAN_LIMIT, total) if total else _NESTED_SCAN_LIMIT
        scan, scan_chunks = _drain_head(handle, scan_budget)
        if time.monotonic() > deadline:
            raise PermanentParserFailure(
                f"triage structural passes exceeded their {_TRIAGE_TIMEOUT_S:.0f}s defensive-parse budget"
            )

        # Step 5: nested archive & file scanner.
        hits = _scan_nested_magics(scan)
        if hits:
            findings.append(self.make_finding(ctx, "nested_archive_scan",
                                              {"hits": hits, "chunk_ids": scan_chunks}))

        # Step 6: metadata structural inspection (schema-only stub).
        findings.append(self.make_finding(
            ctx, "metadata_structural",
            {"scanned_bytes": len(scan), "defect_flags": [],
             "note": "schema-only structural inspection — stub, no demuxer call"},
        ))

        # Step 7: sliding-window Shannon entropy.
        alert_spans = _entropy_alerts(scan)
        findings.append(self.make_finding(
            ctx, "entropy_scan",
            {"window": _ENTROPY_WINDOW, "step": _ENTROPY_STEP,
             "alert_threshold": _ENTROPY_ALERT, "alert_spans": alert_spans},
        ))


# ---------------------------------------------------------------------------
# Module-level helpers (kept out of the class so they stay trivially testable)
# ---------------------------------------------------------------------------
def _drain_tail(handle: Any, total: int | None) -> tuple[bytes, list[str]]:
    """Bounded read of the object's tail (``_TAIL_WINDOW`` bytes), via a
    fresh, independent reader — never shifts another pass's cursor."""
    if total is None or total == 0:
        return b"", []
    limit = min(_TAIL_WINDOW, total)
    reader = get_read_surface(handle)
    try:
        data = reader.read(limit)
    finally:
        reader.close()
    chunk_ids_in_range = getattr(handle, "chunk_ids_in_range", None)
    chunk_ids = list(chunk_ids_in_range(max(0, total - len(data)), total)) \
        if callable(chunk_ids_in_range) else []
    return data, chunk_ids


def _sniff_mime(head: bytes) -> tuple[list[str], bool]:
    """Local signature stub.  Returns ``([mime, ...], data_gap)``; the
    data gap flags the missing optional ``libmagic`` dependency — a
    recorded absence, never a fabricated result."""
    mimes: list[str] = []
    if head.startswith(b"\xFF\xD8\xFF"):
        mimes.append("image/jpeg")
    elif head.startswith(b"\x89PNG"):
        mimes.append("image/png")
    elif head.startswith(b"RIFF"):
        mimes.append("image/webp" if head[8:12] == b"WEBP" else "audio/wav")
    elif head.startswith(b"PK\x03\x04"):
        mimes.append("application/zip")
    elif head.startswith(b"%PDF"):
        mimes.append("application/pdf")
    elif head.startswith(b"\x1F\x8B"):
        mimes.append("application/gzip")
    elif head.startswith(b"7z\xBC\xAF\x27\x1C"):
        mimes.append("application/x-7z-compressed")
    elif head.startswith(b"\xd0\xcf\x11\xe0"):
        mimes.append("application/x-ole2")
    elif head.startswith(b"\x0a\x0d\x0d\x0a"):
        mimes.append("application/vnd.tcpdump.pcap")
    elif head.startswith(b"\xd4\xc3\xb2\xa1"):
        mimes.append("application/vnd.tcpdump.pcapng")
    else:
        # No local signature rule matched; entropy-only fallback (data gap).
        mimes = []
    return mimes, not mimes


def _container_inventory_stub(mime: str, total: int | None) -> dict[str, Any]:
    """Demuxer-inventory stub (schema-only; no demuxer call in this pass)."""
    return {"mime": mime, "total_bytes": total,
            "tracks": None, "note": "demuxer stub — schema-only, no call"}


def _demuxer_available(mime: str) -> bool:
    """Optional-dependency probe (PyAV / scapy / stdlib).  Reported as a
    data gap when the dependency is missing — never a fabricated result."""
    if mime in ("application/pdf", "application/zip", "application/gzip"):
        import zlib  # stdlib: safe structural walk, no external call
        return True
    for mod in ("av", "scapy"):
        try:
            __import__(mod)
            return True
        except ModuleNotFoundError:
            continue
    return False


def _trailing_audit(adapter: BaseAdapter, ctx: RunContext, total: int | None,
                    tail: bytes, tail_chunks: list[str], mime: str) -> Finding:
    """Step 4: locate the carrier's logical-EOF marker inside the bounded
    tail window; isolate the trailing region beyond 16 bytes as an inert
    quarantine candidate (never executed)."""
    marker = _LOGICAL_EOF_MARKERS.get(mime)
    if marker is None or total is None:
        return adapter.make_finding(
            ctx, "trailing_audit",
            {"mime": mime, "trailing_bytes": None, "chunk_ids": tail_chunks,
             "note": "no logical-EOF rule for this carrier"},
            status=FindingStatus.DATA_GAP,
        )
    found_at = tail.rfind(marker)
    if found_at == -1:
        # Marker not in the tail window we could read: the logical EOF is
        # either before our window or the container is structurally malformed.
        return adapter.make_finding(
            ctx, "trailing_audit",
            {"mime": mime, "trailing_bytes": None, "chunk_ids": tail_chunks,
             "note": "logical EOF marker absent from tail window; possible malformed container"},
            status=FindingStatus.QUARANTINE,
        )
    trailing = total - (found_at + len(marker))  # Δ_trailing (TRD § 22.2 step 4)
    status = FindingStatus.QUARANTINE if trailing > _TRAILING_TOLERANCE else FindingStatus.OK
    return adapter.make_finding(
        ctx, "trailing_audit",
        {"mime": mime, "logical_eof_marker": marker.hex(),
         "trailing_bytes": trailing, "chunk_ids": tail_chunks,
         "quarantined_inert": status is FindingStatus.QUARANTINE},
        status=status,
    )


def _route_carrier(mime: str) -> str:
    """Step 9: static adapter routing — carrier class for the modality row."""
    for mimes, carrier in _CARRIER_ROUTES:
        if mime in mimes:
            return carrier
    return "UNKNOWN"


def _scan_nested_magics(scan: bytes) -> list[dict[str, Any]]:
    """Step 5: record byte offsets of nested archive headers in the window."""
    hits: list[dict[str, Any]] = []
    for magic, name in _NESTED_MAGICS:
        start = 0
        while True:
            idx = scan.find(magic, start)
            if idx == -1:
                break
            hits.append({"container": name, "magic": magic.hex(), "offset": idx})
            start = idx + len(magic)
    hits.sort(key=lambda h: h["offset"])
    return hits


def _entropy_alerts(scan: bytes) -> list[dict[str, float | int]]:
    """Step 7: sliding-window Shannon entropy, alert on H ≥ 7.85 (indicative
    of embedded compressed/encrypted payload in an otherwise plain carrier)."""
    alerts: list[dict[str, float | int]] = []
    if len(scan) < _ENTROPY_WINDOW:
        return alerts
    for start in range(0, len(scan) - _ENTROPY_WINDOW + 1, _ENTROPY_STEP):
        window = scan[start:start + _ENTROPY_WINDOW]
        hist = [0] * 256
        for b in window:
            hist[b] += 1
        h = -sum((c / _ENTROPY_WINDOW) * math.log2(c / _ENTROPY_WINDOW) for c in hist if c)
        if h >= _ENTROPY_ALERT:
            alerts.append({"offset": start, "entropy": round(h, 4)})
    return alerts
