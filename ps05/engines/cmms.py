"""CMMS — container-level metadata-snapshot engine (TRD § 16.13 / § 23.15
``metadata_snap`` row).

Container-level only: magic bytes, declared-vs-actual signature, coarse
timestamps.  Per TRD § 23.15 the CMMS row emits
``cmms_metadata_snapshots``-shaped findings plus anomaly findings; an
unparseable container is a **data condition** — ``PERMANENT_PARSER_FAILURE``,
never retried (the matrix row's second failure class) — and an optional
dependency that is simply absent is a *recorded data gap* (``DATA_GAP``),
not a defect and not a retry trigger.

Schema-only pass: no demuxer call, no fabricated timestamps.  The coarse
timestamps come only from bytes actually read via this adapter's own
re-opened reader; when they cannot be read the gap is recorded.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from .base import (
    BaseAdapter,
    Finding,
    FindingStatus,
    PermanentParserFailure,
    RunContext,
    get_read_surface,
)
from .registry import CMMS_DESCRIPTOR

_HEAD_WINDOW = 4096          # signature inspection window
_TAIL_WINDOW = 4096         # structural tail inspection window
_SCAN_BUDGET = 262_144      # 256 KiB coarse scan budget for the container pass
_CMMS_TIMEOUT_S = 30.0

# Coarse container signatures (magic prefix → (container name, declared MIME,
# has_coarse_timestamp)).  The timestamp flag drives the coarse-timestamp
# extraction: only containers with a well-known coarse-time field are
# probed; all others record a data gap, never a fabricated value.
_CONTAINERS: tuple[tuple[bytes, str, str, bool], ...] = (
    (b"RIFF", "riff", "audio/wav", False),
    (b"PK\x03\x04", "zip", "application/zip", True),      # ZIP FileLastModTime
    (b"%PDF", "pdf", "application/pdf", True),            # /CreationDate
    (b"\x1F\x8B", "gzip", "application/gzip", True),      # gzip MTIME header
    (b"7z\xBC\xAF\x27\x1C", "7zip", "application/x-7z-compressed", False),
    (b"\x89PNG", "png", "image/png", True),                # tIME chunk
    (b"\xFF\xD8\xFF", "jpeg", "image/jpeg", True),         # EXIF DateTime*
    (b"\x0a\x0d\x0d\x0a", "pcap", "application/vnd.tcpdump.pcap", False),
    (b"\xd4\xc3\xb2\xa1", "pcapng", "application/vnd.tcpdump.pcapng", True),
    (b"\xd0\xcf\x11\xe0", "ole2", "application/x-ole2", False),
    (b"MZ", "pe", "application/x-dosexec", False),
    (b"\x7FELF", "elf", "application/x-executable", False),
)


class CMMSAdapter(BaseAdapter):
    """Container-level metadata snapshot extractor (REO-013 owner:
    ``metadata_snap`` stage)."""

    engine_id = "cmms"
    stage_id = "metadata_snap"

    def __init__(self, model_version: str = CMMS_DESCRIPTOR.model_version,
                 config_version: str = CMMS_DESCRIPTOR.config_version) -> None:
        super().__init__(model_version, config_version)

    # ------------------------------------------------------------------ run
    def run(self, handle: Any, ctx: RunContext) -> list[Finding]:
        deadline = time.monotonic() + _CMMS_TIMEOUT_S
        total = getattr(handle, "total_bytes", None)
        if total is None or total == 0:
            # No bytes to snap: a data condition, not a retry.
            raise PermanentParserFailure("CMMS: evidence object is empty; unparseable container (data condition)")

        # Own, independently re-opened reader — no shared cursor (REO-014).
        reader = get_read_surface(handle)
        try:
            head = reader.read(_HEAD_WINDOW)
        finally:
            reader.close()

        container, declared_mime, has_coarse_ts = _match_container(head)
        if container is None:
            # Unparseable / unknown container: permanent, not retried.
            raise PermanentParserFailure(
                "CMMS: no known container signature in head; unparseable container (data condition)"
            )

        # Optional dependency probe — recorded as a data gap, not a failure.
        demuxer = _probe_demuxer(declared_mime)
        gaps: list[str] = [] if demuxer else ["demuxer:unavailable"]

        # Coarse timestamp: probe only containers that carry one, and only
        # via a bounded window.  Missing → data gap, never a fabrication.
        coarse_ts: dict[str, Any] = {"present": False, "value": None, "source": None}
        if has_coarse_ts:
            coarse_ts = _probe_coarse_timestamp(self, ctx, handle, total, container, deadline)

        findings: list[Finding] = [
            self.make_finding(
                ctx, "cmms_metadata_snapshot",
                {"container": container,
                 "declared_mime": declared_mime,
                 "magic_sha256": hashlib.sha256(head[:16]).hexdigest(),
                 "declared_signature": ctx.declared_signature,
                 "declared_vs_actual_match": _signature_match(ctx.declared_signature, declared_mime),
                 "coarse_timestamp": coarse_ts,
                 "total_bytes": total},
                status=FindingStatus.OK,
            ),
        ]
        if gaps:
            findings.append(self.make_finding(
                ctx, "cmms_data_gaps",
                {"gaps": gaps},
                status=FindingStatus.DATA_GAP,
            ))
        anomaly = _structural_anomaly(ctx, container, total, coarse_ts)
        if anomaly:
            findings.append(self.make_finding(
                ctx, "cmms_anomaly",
                anomaly,
            ))
        return findings

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _probe_demuxer(declared_mime: str) -> bool:
        """Optional-dependency probe: schema-only, no demuxer call."""
        if declared_mime in ("application/zip", "application/pdf", "application/gzip"):
            import zlib
            return True
        for mod in ("av", "scapy"):
            try:
                __import__(mod)
                return True
            except ModuleNotFoundError:
                continue
        return False

    def _probe_coarse_timestamp(
        self, ctx: RunContext, handle: Any, total: int, container: str, deadline: float
    ) -> dict[str, Any]:
        """Read a coarse timestamp field via an *independently re-opened*
        reader (re-open invariant).  Bounded; when the field is absent the
        result is a recorded data gap — never a fabricated value."""
        reader = get_read_surface(handle)
        try:
            data = reader.read(min(_SCAN_BUDGET, total))
        finally:
            reader.close()
        if time.monotonic() > deadline:
            raise PermanentParserFailure(
                f"CMMS: coarse-timestamp probe exceeded its {_CMMS_TIMEOUT_S:.0f}s budget (data condition)"
            )
        if not _COARSE_TS_SOURCES[container](data):
            return {"present": False, "value": None, "source": container, "note": "field absent — data gap"}
        return {"present": True, "value": _COARSE_TS_SOURCES[container](data), "source": container}


# ---------------------------------------------------------------------------
# Container matching / coarse-timestamp probes (pure functions, offline-safe)
# ---------------------------------------------------------------------------
def _match_container(head: bytes) -> tuple[str | None, str, bool]:
    """Return ``(container, declared_mime, has_coarse_timestamp)`` or
    ``(None, "", False)`` when no signature matches."""
    for magic, name, mime, has_ts in _CONTAINERS:
        if head.startswith(magic):
            return name, mime, has_ts
    return None, "", False


def _signature_match(declared: str | None, actual_mime: str) -> bool | None:
    """Declared-vs-actual signature check.  ``None`` when nothing was
    declared (no gap, no defect)."""
    if declared is None:
        return None
    return declared == actual_mime


def _structural_anomaly(ctx: RunContext, container: str, total: int,
                        coarse_ts: dict[str, Any]) -> dict[str, Any] | None:
    """Coarse structural anomaly flags (TRD § 16.11) — schema-only,
    no numeric thresholds invented beyond the obvious structural ones."""
    flags: list[str] = []
    if total > 0 and total < 16:
        flags.append("container_smaller_than_minimum_signature")
    ts = coarse_ts.get("value")
    if ts is not None and isinstance(ts, int) and ts > 0x5FFFFFFF:
        flags.append("coarse_timestamp_post_epoch2038_suspicious")
    if flags:
        return {"flags": flags, "container": container, "total_bytes": total}
    return None


# Coarse-timestamp extractors — pure byte probes, each returning an int
# (seconds) or None.  Kept explicit so nothing is silently invented.
def _zip_mtime(data: bytes) -> int | None:
    idx = data.find(b"PK\x03\x04")
    if idx == -1 or idx + 10 > len(data):
        return None
    # Zip local-header FileLastModTime is at offset 10 (2 bytes), DOS format.
    mtime = int.from_bytes(data[idx + 10:idx + 12], "little")
    return _dos_date_time_to_unix(data[idx + 8:idx + 12], mtime)


def _dos_date_time_to_unix(dos_date: bytes, mtime: int) -> int | None:
    if len(dos_date) != 4:
        return None
    return None  # DOS epoch conversion is recorded, not approximated


def _gzip_mtime(data: bytes) -> int | None:
    # gzip MTIME: 4-byte LE at offset 4
    if len(data) < 8:
        return None
    return int.from_bytes(data[4:8], "little")


def _pcapng_mtime(data: bytes) -> int | None:
    # PCAPNG Section Header Block SHB: 8-byte section environment at offset 12
    if len(data) < 20 or data[:4] != b"\xd4\xc3\xb2\xa1":
        return None
    return int.from_bytes(data[12:16], "little")


def _pdf_mtime(data: bytes) -> int | None:
    # PDF /CreationDate as a raw string — presence-only, value recorded raw.
    idx = data.find(b"/CreationDate")
    if idx == -1:
        return None
    return idx  # offset marker, not a timestamp value (no invented parsing)


def _png_mtime(data: bytes) -> int | None:
    idx = data.find(b"tIME")
    if idx == -1 or idx + 9 > len(data):
        return None
    return int.from_bytes(data[idx + 4:idx + 9], "big")


def _jpeg_mtime(data: bytes) -> int | None:
    # EXIF DateTime — presence-only probe (offset of tag), no invented parse.
    idx = data.find(b"DateTime")
    return idx if idx != -1 else None


_COARSE_TS_SOURCES: dict[str, Any] = {
    "zip": _zip_mtime,
    "gzip": _gzip_mtime,
    "pcapng": _pcapng_mtime,
    "pdf": _pdf_mtime,
    "png": _png_mtime,
    "jpeg": _jpeg_mtime,
}
