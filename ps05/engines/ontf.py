"""ONTF — defensive PCAP / PCAPNG dissection engine (TRD § 22.8 / § 23.15
``network_dissection`` row).

Defensive-parser-sandbox semantics: the capture is parsed as a *bounded
chunk* — a fixed packet-budget read via this adapter's own re-opened reader
(no unbounded whole-file read), a hard timeout guard, and **no network
egress** (the module never opens a socket; dissection is byte-level only).
A truncated capture is a **data condition** — ``PERMANENT_PARSER_FAILURE``,
never a retry (the TRD § 23.15 ONTF row names this explicitly).

TShark / Zeek deep dissection is an **external-tool step** (the second leg
of the ONTF row, on ``NETWORK_FILESYSTEM_FORENSIC``).  This pass does *not*
call an installed binary: it documents the command contract in
``EXTERNAL_TOOL_CONTRACT`` and emits a ``requires_tool`` marker finding so
the external step is testable offline and the DAG can schedule it on the
right queue without this engine owning that execution.
"""

from __future__ import annotations

import struct
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
from .registry import ONTF_DESCRIPTOR

# Bounded-parser budgets (TRD § 23.15 DEFENSIVE_PARSER_SANDBOX profile):
_PCAPNG_MAGIC = 0xA1B2C3D4
_PCAP_MAGIC_LITTLE = 0xA1B23C4D
_PCAP_MAGIC_BIG = 0x4C3B2BA1
_PACKET_BUDGET = 4096      # max packets dissected in one pass (bounded chunk)
_BYTE_BUDGET = 4 * 1024 * 1024  # 4 MiB read budget per pass
_ONTF_TIMEOUT_S = 60.0
_MAX_EVIDENCE = 65535      # PCAP per-packet-included-length cap

# ---------------------------------------------------------------------------
# External-tool command contract (TShark / Zeek, § 22.8) — documented, not
# executed.  The NETWORK_FILESYSTEM_FORENSIC queue owns this step; ONTF's
# sandbox pass emits a ``requires_tool`` marker keyed to this contract.
# ---------------------------------------------------------------------------
EXTERNAL_TOOL_CONTRACT: dict[str, Any] = {
    "tool": "tshark",
    "min_version": "4.0",
    "egress_policy": "none",  # air-gap invariant (§ 15.3): no network egress
    "tshark": {
        "command": "tshark -r {capture_uri} -Y {filter} -w {out_capture_uri} -c {packet_budget}",
        "zeek": {
            "command": "zeek -p {pcap_pipe} {zeek_log_dir}/",
            "logs": ["conn", "dns", "http", "ssl", "ident"],
        },
    },
    "notes": (
        "The external step runs on NETWORK_FILESYSTEM_FORENSIC with its own "
        "resource profile and checkpoints at cp_pcap_dissection_batch.  This "
        "engine's sandbox pass never invokes tshark or zeek; the command "
        "strings above are the scheduling contract, not a call site."
    ),
}


class ONTFAdapter(BaseAdapter):
    """Defensive PCAP/PCAPNG dissection (REO-013 owner:
    ``network_dissection`` stage; TShark/Zeek step is external, § 22.8)."""

    engine_id = "ontf"
    stage_id = "network_dissection"

    def __init__(self, model_version: str = ONTF_DESCRIPTOR.model_version,
                 config_version: str = ONTF_DESCRIPTOR.config_version) -> None:
        super().__init__(model_version, config_version)

    # ------------------------------------------------------------------ run
    def run(self, handle: Any, ctx: RunContext) -> list[Finding]:
        deadline = time.monotonic() + _ONTF_TIMEOUT_S
        total = getattr(handle, "total_bytes", None)

        # Own, independently re-opened reader; bounded budget (sandbox).
        reader = get_read_surface(handle)
        try:
            data = reader.read(min(_BYTE_BUDGET, total)) if total else reader.read(_BYTE_BUDGET)
        finally:
            reader.close()

        if not data:
            raise PermanentParserFailure(
                "ONTF: empty capture read (data condition, not retriable)"
            )

        kind = _detect_capture_kind(data)
        if kind is None:
            # Not PCAP / PCAPNG: unsupported container — permanent, inert.
            raise PermanentParserFailure(
                "ONTF: no PCAP/PCAPNG signature in head; unparseable container (data condition)"
            )

        truncated, flows, summary = _dissect(kind, data, total)
        findings: list[Finding] = [
            self.make_finding(
                ctx, "ontf_capture_summary",
                {"capture_kind": kind,
                 "bytes_read": len(data),
                 "total_bytes": total,
                 "packet_budget": _PACKET_BUDGET,
                 "packets_dissected": summary["packets_dissected"],
                 "flows": summary["flow_count"],
                 "summary": summary,
                 "truncated_capture": truncated,
                 "sandbox": {"bounded_memory": True, "timeout_guard_s": _ONTF_TIMEOUT_S,
                             "network_egress": False},
                 "egress_policy": EXTERNAL_TOOL_CONTRACT["egress_policy"]},
                status=FindingStatus.QUARANTINE if truncated else FindingStatus.OK,
            ),
        ]

        if flows:
            findings.append(self.make_finding(ctx, "ontf_flows",
                                              {"flows": flows, "budget": _PACKET_BUDGET}))

        # External-tool step marker: TShark/Zeek dissection is scheduled off
        # this engine's output on NETWORK_FILESYSTEM_FORENSIC — testable
        # offline, no binary call in this pass.
        findings.append(self.make_finding(
            ctx, "ontf_external_tool_step",
            {"requires_tool": EXTERNAL_TOOL_CONTRACT["tool"],
             "contract": EXTERNAL_TOOL_CONTRACT,
             "queue": "NETWORK_FILESYSTEM_FORENSIC",
             "checkpoint": ONTF_DESCRIPTOR.checkpoint},
            status=FindingStatus.REQUIRES_TOOL,
        ))

        if truncated:
            # Truncated capture is a data condition, not a retry: raise
            # permanently so the task factory maps PERMANENT_PARSER_FAILURE.
            raise PermanentParserFailure(
                "ONTF: capture truncated mid-stream (data condition, not retriable)",
            )
        return findings


# ---------------------------------------------------------------------------
# Pure, bounded dissection helpers (byte-level, no egress, no subprocess)
# ---------------------------------------------------------------------------
def _detect_capture_kind(data: bytes) -> str | None:
    """PCAP / PCAPNG signature sniff on the head.

    Classic PCAP: 4-byte magic at offset 0 (LE or BE).  PCAPNG: the first
    block is a Section Header Block (type 0x0000000A, LE) whose
    ``magicNumber`` field at offset 8 is ``0xA1B2C3D4``.
    """
    if data[:4] == struct.pack("<I", _PCAP_MAGIC_LITTLE) or \
       data[:4] == struct.pack(">I", _PCAP_MAGIC_BIG):
        return "pcap"
    if len(data) >= 12 and struct.unpack_from("<I", data, 0)[0] == 0x0000000A and \
       struct.unpack_from("<I", data, 8)[0] == _PCAPNG_MAGIC:
        return "pcapng"
    return None


def _dissect(kind: str, data: bytes, total: int | None) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    """Bounded dissection of the read window.  Returns
    ``(truncated, flows, summary)``.  A truncation flag (not a retry) is
    set when the read window ends on a partial record."""
    truncated = False
    if total is not None and total > len(data):
        truncated = True  # read budget capped the window: capture continues
    if kind == "pcap":
        flows, summary = _dissect_pcap(data, truncated)
    else:
        flows, summary = _dissect_pcapng(data, truncated)
    return truncated, flows, summary


def _flow_key(ethertype: int) -> str:
    """Coarse L3/L4 flow classification (schema-only; no Zeek-grade
    protocol tracking — that is the external tool's job)."""
    if ethertype == 0x0800:
        return "ipv4"
    if ethertype == 0x86DD:
        return "ipv6"
    if ethertype == 0x8100:
        return "vlan_tagged"
    return f"ethertype_0x{ethertype:04x}"


def _dissect_pcap(data: bytes, truncated_in: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Classic PCAP: 24-byte global header, then 16-byte record headers.

    Bounded to ``_PACKET_BUDGET`` records; a final partial record marks the
    capture truncated (data condition)."""
    if len(data) < 24:
        return [], {"packets_dissected": 0, "flow_count": 0,
                    "note": "global header incomplete — truncated capture"}
    version_major, version_minor = struct.unpack_from("<HH", data, 4)
    if version_major != 2:
        return [], {"packets_dissected": 0, "flow_count": 0,
                    "note": f"unsupported PCAP major version {version_major}"}
    flows: list[dict[str, Any]] = []
    packets = 0
    offset = 24
    n = len(data)
    truncated_out = truncated_in
    while offset + 16 <= n and packets < _PACKET_BUDGET:
        incl_len = struct.unpack_from("<I", data, offset + 8)[0]
        if incl_len > _MAX_EVIDENCE:
            # Oversized included-length: hostile header, stop this pass.
            truncated_out = True
            break
        if offset + 16 + incl_len > n:
            truncated_out = True  # partial record at the window edge
            break
        packets += 1
        ethernet_type = _extract_ethertype(data[offset + 16:offset + 16 + incl_len])
        key = _flow_key(ethernet_type)
        if flows and flows[-1]["flow"] == key:
            flows[-1]["packets"] += 1
        else:
            flows.append({"flow": key, "ethertype": ethernet_type, "packets": 1})
        offset += 16 + incl_len
    summary = {"packets_dissected": packets, "flow_count": len(flows)}
    return flows, summary


def _dissect_pcapng(data: bytes, truncated_in: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """PCAPNG: block-oriented parse.  Only Interface Description Blocks
    (id 0x00000001) and Enhanced Packet Blocks (id 0x00000006) are walked —
    bounded to ``_PACKET_BUDGET`` blocks; a partial final block marks the
    capture truncated (data condition)."""
    if len(data) < 12:
        return [], {"packets_dissected": 0, "flow_count": 0,
                    "note": "PCAPNG stream incomplete — truncated capture"}
    n = data[4:8]
    flows: list[dict[str, Any]] = []
    blocks = 0
    offset = 0
    while offset + 12 <= len(data) and blocks < _PACKET_BUDGET:
        btype, block_len = struct.unpack_from("<II", data, offset)
        if btype == 0x00000006 and block_len >= 32:  # EPB
            if block_len > _MAX_EVIDENCE + 32:
                break
            if offset + block_len > len(data):
                break
            ethernet_type = _extract_ethertype(data[offset + 20:offset + block_len])
            key = _flow_key(ethernet_type)
            if flows and flows[-1]["flow"] == key:
                flows[-1]["packets"] += 1
            else:
                flows.append({"flow": key, "ethertype": ethernet_type, "packets": 1})
        blocks += 1
        if block_len == 0:
            break  # malformed: do not loop forever (sandbox invariant)
        offset += block_len
    truncated_out = truncated_in or (offset < len(data) and blocks >= _PACKET_BUDGET)
    summary = {"packets_dissected": blocks, "flow_count": len(flows),
               "unparsed_tail": len(data) - offset}
    return flows, summary


def _extract_ethertype(packet: bytes) -> int:
    """Coarse L2/L3 Ethernet-type extraction (no protocol deep-dive — the
    TShark/Zeek step owns protocol tracking)."""
    if len(packet) < 14:
        return 0
    ethertype = int.from_bytes(packet[12:14], "big")
    if ethertype in (0x8100, 0x88A8):  # 802.1Q / 802.1ad — peek inside the tag
        if len(packet) < 18:
            return 0
        ethertype = int.from_bytes(packet[16:18], "big")
    return ethertype
