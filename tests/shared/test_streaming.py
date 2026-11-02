"""Shared-streaming tests for ``ps05.shared.streaming``: chunking, digest order,
bounded reads and the CCHDF re-open contract (TRD 23.17).

Verifies: deterministic/distinct ``ChunkManifest.chunk_id``; idempotent
``add`` under retry; an arrival-order-invariant final digest (6 chunks, 3
orders); ``verify_parent_child`` pass/tamper; a ``BoundedStreamReader`` that
never reads outside its chunk ranges; and ``EvidenceStreamHandle.open_reader``
yielding independent cursors.
"""

from __future__ import annotations

import hashlib
import io
import tempfile

import pytest

from ps05.shared.integrity import IntegrityFailure
from ps05.shared.streaming import BoundedStreamReader, ChunkManifest, EvidenceStreamHandle


#: Six contiguous 32-byte chunks tiling [0, 192).
OFFSETS = [(i * 32, 32) for i in range(6)]


def _chunks(manifest: ChunkManifest) -> list[str]:
    """Register the six tiling chunks in offset order; returns chunk ids."""
    ids = []
    for i, (offset, size) in enumerate(OFFSETS):
        digest = hashlib.sha256(f"payload-{offset}".encode("utf-8")).hexdigest()
        ids.append(manifest.add(offset, size, digest, seq=i))
    return ids


def test_chunk_id_deterministic_and_distinct():
    manifest = ChunkManifest(parent_digest="parent-x")
    a = manifest.chunk_id(0, 32)
    b = manifest.chunk_id(0, 32)
    c = manifest.chunk_id(32, 32)
    assert a == b, "same offset/size must yield the same chunk id"
    assert a != c, "different offsets must yield distinct chunk ids"
    assert len(a) == 32


def test_add_is_idempotent_under_retry():
    manifest = ChunkManifest()
    digest = hashlib.sha256(b"chunk-bytes").hexdigest()
    first = manifest.add(0, 32, digest, seq=1)
    # A redelivery of the SAME chunk (identical content and arrival seq) is
    # a no-op: chunk_id is deterministic, the record is byte-identical.
    second = manifest.add(0, 32, digest, seq=1)
    assert first == second
    assert len(manifest.chunks_in_offset_order()) == 1
    # A conflicting digest for the same chunk id is rejected.
    with pytest.raises(IntegrityFailure):
        manifest.add(0, 32, hashlib.sha256(b"other").hexdigest(), seq=1)


def test_final_digest_is_order_invariant():
    orders = [[0, 1, 2, 3, 4, 5], [3, 1, 4, 0, 5, 2], [5, 4, 3, 2, 1, 0]]
    digests = []
    for order in orders:
        manifest = ChunkManifest()
        for seq, i in enumerate(order):
            offset, size = OFFSETS[i]
            digest = hashlib.sha256(f"payload-{offset}".encode("utf-8")).hexdigest()
            manifest.add(offset, size, digest, seq=seq)
        digests.append(manifest.order_independent_final_digest())
    # 6 chunks, 3 random completion orders -> one digest.
    assert digests[0] == digests[1] == digests[2]


def test_verify_parent_child_passes_and_detects_tamper():
    manifest = ChunkManifest(parent_digest="parent")
    _chunks(manifest)
    assert manifest.verify_parent_child() is True
    # Tamper the parent digest used to compute the chunk ids -> chain breaks.
    manifest.parent_digest = "tampered-parent"
    with pytest.raises(IntegrityFailure):
        manifest.verify_parent_child()


class TrackingBuffer(io.BytesIO):
    """``BytesIO`` proxy recording every ``(start, end)`` byte-range read."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.reads: list[tuple[int, int]] = []

    def read(self, n: int) -> bytes:
        start = self.tell()
        data = super().read(n)
        self.reads.append((start, start + len(data)))
        return data


def test_bounded_reader_never_touches_bytes_outside_chunk_range(tmp_path):
    # Exactly 6 x 32-byte chunks tiling [0, 192) so no chunk short-reads.
    payload = bytes(range(192))
    with tempfile.NamedTemporaryFile(dir=tmp_path, delete=False) as fh:
        fh.write(payload)
        path = fh.name
    with open(path, "rb") as source:
        reader = BoundedStreamReader(source, _manifest_for(payload))
        for i, (offset, size) in enumerate(OFFSETS):
            data = reader.read_chunk(_chunk_id(payload, i))
            assert len(data) == size

    with open(path, "rb") as source:
        proxy = TrackingBuffer(source.read())
        reader = BoundedStreamReader(proxy, _manifest_for(payload))
        for i in range(6):
            reader.read_chunk(_chunk_id(payload, i))
        total = len(payload)
        for start, end in proxy.reads:
            assert start >= 0 and end <= total, "reader touched bytes outside the object"
            # Bounded: never a read past the object boundary.
        assert all(end - start <= 32 for start, end in proxy.reads), "reads must stay bounded"


def _manifest_for(payload: bytes) -> ChunkManifest:
    manifest = ChunkManifest(evidence_id="ev-1")
    for i, (offset, size) in enumerate(OFFSETS):
        digest = hashlib.sha256(payload[offset : offset + size]).hexdigest()
        manifest.add(offset, size, digest, seq=i)
    return manifest


def _chunk_id(payload: bytes, i: int) -> str:
    offset, size = OFFSETS[i]
    return ChunkManifest(evidence_id="ev-1").chunk_id(offset, size)


def test_evidence_stream_readers_are_independent():
    payload = b"evidence-bytes" * 8
    manifest = ChunkManifest()
    manifest.add(0, len(payload), hashlib.sha256(payload).hexdigest(), seq=0)

    handle = EvidenceStreamHandle(lambda: io.BytesIO(payload), manifest)
    reader_a = handle.open_reader()
    reader_b = handle.open_reader()

    head = reader_a.read(len(payload) // 2)
    assert reader_b.read(4) == payload[:4], "reader A's position must not move reader B's cursor"
    assert head == payload[: len(payload) // 2]
    # Re-opening yields yet another fresh cursor.
    reader_c = reader_a.re_open()
    assert reader_c.read(3) == payload[:3]
