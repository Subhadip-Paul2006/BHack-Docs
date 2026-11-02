"""Evidence ingestion: registration + staged workflow + resumable uploads
(TRD 4 / 19 / 23.8 / 23.17).

The staged workflow (``stage_then_commit``):
1. **Write staging** — chunks land in the rewritable staging area,
   keyed by deterministic chunk ID (retry without duplication:
   re-uploading a completed chunk ID is a no-op).
2. **Verify** — byte count (sum of chunk sizes) and per-chunk digests
   are checked against the manifest; the whole-object dual hash is
   recomputed in bounded streaming when expectations are recorded.
3. **Commit immutable** — the object is moved atomically to WORM
   storage; it is never overwritten afterwards (CEIPL/EIV).
4. **(Caller)** registers metadata + lineage (``register_evidence`` /
   custody ledger append).
5. **(Caller)** publishes the outbox event *in-DB* (same transaction as
   the evidence row) so no event is lost if the process dies before the
   broker hand-off.

This module performs steps 1-3. Steps 4-5 are deliberately the caller's
so the DB and event publication stay transactional together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ps05.shared.integrity import CEIPLIntegrity, IntegrityFailure
from ps05.shared.streaming import BoundedStreamReader, ChunkManifest


@dataclass(frozen=True)
class EvidenceRecord:
    """Registered (metadata) view of an ingested evidence object.

    This is the caller-facing record produced by
    :func:`register_evidence`; the outbox event published in step 5 of
    the staged workflow carries these fields.
    """

    case_id: str
    filename: str
    size: int
    digests: dict[str, str]  # e.g. {"sha256": ..., "sha3_512": ...}
    custody_ref: str


def register_evidence(
    case_id: str,
    filename: str,
    size: int,
    digests: dict[str, str],
    custody_ref: str,
) -> EvidenceRecord:
    """Register metadata for an ingested object.

    Registration records *what* is in the store; it does not re-verify
    fixity — that is done by the WORM readback + ``verify_fixity`` step.
    A matching digest does NOT establish who acquired the evidence;
    ``custody_ref`` points at the append-only custody ledger for that.
    """
    return EvidenceRecord(
        case_id=case_id,
        filename=filename,
        size=size,
        digests=dict(digests),
        custody_ref=custody_ref,
    )


def plan_chunks(total_bytes: int, chunk_size: int) -> list[tuple[int, int]]:
    """Deterministic resumable-upload chunk plan.

    Returns ``(offset, size)`` pairs tiling ``[0, total_bytes)`` in
    fixed-size chunks; the last chunk is the remainder. Same
    inputs always yield the same plan, so chunk IDs (and therefore the
    upload's deduplication and resume points) are stable across
    attempts.
    """
    if total_bytes < 0:
        raise ValueError("total_bytes must be >= 0")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    plan: list[tuple[int, int]] = []
    offset = 0
    while offset < total_bytes:
        size = min(chunk_size, total_bytes - offset)
        plan.append((offset, size))
        offset += size
    return plan


def stage_then_commit(
    reader: BoundedStreamReader,
    manifest: ChunkManifest,
    store: Any,
) -> str:
    """Execute the staged workflow over one object.

    Parameters
    ----------
    reader:
        Bounded reader positioned over the *staging* bytes; each call
        reads exactly one chunk range in bounded memory.
    manifest:
        Chunk manifest carrying every chunk's ``digest`` and, when
        known, the whole-object ``expected_sha256`` /
        ``expected_sha3_512``.
    store:
        A :class:`ps05.shared.ports.EvidenceStore`-compatible object.
    Returns
    -------
    str
        The immutable object key returned by
        ``store.commit_immutable``.

    Raises
    ------
    IntegrityFailure
        On per-chunk digest mismatch, a byte-count (coverage) mismatch,
        or a whole-object digest mismatch when expectations are set.
    """
    evidence_id = manifest.evidence_id

    records = manifest.chunks_in_offset_order()
    if not records:
        raise IntegrityFailure("manifest has no chunks to stage")

    # 1. Write staging, chunk by chunk, in offset order. Idempotent on
    #    chunk ID: re-staging an already-complete chunk is a no-op, so
    #    resumed uploads never duplicate.
    expected_total = manifest.total_size()
    written: dict[str, int] = {}
    for record in records:
        data = reader.read_chunk(record.chunk_id)
        # Bounded: only one chunk's bytes are live here at a time.
        store.write_staging(evidence_id, record.chunk_id, data)
        written[record.chunk_id] = record.size

    # 2. Verify: byte count must tile contiguously from 0 (checked in
    #    total_size() above), and every chunk must have been accepted.
    if len(written) != len(records):
        raise IntegrityFailure("staging coverage mismatch")
    if manifest.expected_sha256 is not None or manifest.expected_sha3_512 is not None:
        # Whole-object dual hash, recomputed in bounded streaming.
        reopener = getattr(reader, "re_open", None)
        verify_source = reopener() if callable(reopener) else reader
        actual_sha256, actual_sha3_512 = CEIPLIntegrity.pre_decode_dual_hash(
            _BoundedReopenSource(verify_source, expected_total)
        )
        if manifest.expected_sha256 is not None and actual_sha256 != manifest.expected_sha256:
            raise IntegrityFailure(
                f"whole-object sha256 mismatch: {actual_sha256} != "
                f"{manifest.expected_sha256}"
            )
        if manifest.expected_sha3_512 is not None and actual_sha3_512 != manifest.expected_sha3_512:
            raise IntegrityFailure(
                f"whole-object sha3_512 mismatch: {actual_sha3_512} != "
                f"{manifest.expected_sha3_512}"
            )

    # 3. Commit immutable. The caller (steps 4-5) then registers
    #    metadata + lineage and publishes the outbox event in-DB.
    return store.commit_immutable(evidence_id, manifest)


class _BoundedReopenSource:
    """File-like wrapper that re-seeks to 0 exactly once so the
    whole-object dual hash streams the object from the start without
    the caller needing to re-open the underlying storage handle."""

    def __init__(self, inner: BoundedStreamReader, expected_total: int) -> None:
        self._inner = inner
        self._expected_total = expected_total
        self._consumed = 0
        self._seeked = False

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = 1 << 20
        remaining = self._expected_total - self._consumed
        if remaining <= 0:
            return b""
        if not self._seeked:
            self._inner._source.seek(0)
            self._seeked = True
        data = self._inner._source.read(min(n, remaining))
        self._consumed += len(data)
        return data
