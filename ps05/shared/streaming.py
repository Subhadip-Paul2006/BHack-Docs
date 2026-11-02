"""Bounded-memory streaming primitives (TRD 4 / 23.8 / 23.17).

The universal streaming policy: large evidence is never loaded whole
into RAM. It is addressed by deterministic chunk IDs with explicit
offset ranges, chunk completion order never affects final evidence
ordering, and digest verification happens per chunk in bounded
buffers.

Known-answer empty-input vectors (documented for regression):
- ``sha256(b'')``   = ``e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855``
- ``sha3_512(b'')`` =
  ``a69f73cca23a9ac5c8b567dc185a756e97c982164fe25859e0d1dcc1475c80a615b2123af1f5f94c11e3e9402c3ac558f500199d95b6d3e301758586281dcd26``
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

from ps05.shared.integrity import IntegrityFailure

_DEFAULT_PARENT_DIGEST = ""  # top-level manifests with no object-level lineage


@dataclass(frozen=True)
class ChunkRecord:
    """One chunk of an immutable evidence object.

    ``seq`` is the *arrival* sequence number. Ordering of the evidence
    is derived from ``offset`` only, never from ``seq``.
    """

    chunk_id: str
    offset: int
    size: int
    digest: str  # SHA-256 hex digest of the chunk bytes
    seq: int


class ChunkManifest:
    """Deterministic chunk manifest for one immutable evidence object.

    ``chunk_id(offset, size)`` is a stable prefix of
    ``sha256(f"{offset}:{size}:{parent_digest}")``: two callers that
    describe the same chunk compute the same ID, so re-uploads and
    retries deduplicate on it. ``parent_digest`` is the object-level
    lineage value (empty for a top-level manifest), giving the
    cryptographic parent->child lineage verified by
    :meth:`verify_parent_child`.
    """

    def __init__(
        self,
        parent_digest: str = _DEFAULT_PARENT_DIGEST,
        *,
        expected_sha256: str | None = None,
        expected_sha3_512: str | None = None,
        evidence_id: str = "evidence",
    ) -> None:
        self.parent_digest = parent_digest
        #: Optional whole-object dual hash, checked during staging.
        self.expected_sha256 = expected_sha256
        self.expected_sha3_512 = expected_sha3_512
        #: Stable identifier used by the store's staging area.
        self.evidence_id = evidence_id
        self._chunks: dict[str, ChunkRecord] = {}

    # -- identity ---------------------------------------------------------

    def chunk_id(self, offset: int, size: int) -> str:
        """Stable chunk ID: SHA-256 of (offset, size, parent_digest),
        32-hex-char prefix."""
        payload = f"{offset}:{size}:{self.parent_digest}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:32]

    # -- mutation (idempotent) -------------------------------------------

    def add(self, offset: int, size: int, digest: str, seq: int) -> str:
        """Register a chunk. Re-adding the same chunk ID with identical
        content is a no-op (retry without duplication); a conflicting
        digest for an existing chunk ID raises IntegrityFailure."""
        cid = self.chunk_id(offset, size)
        record = ChunkRecord(cid, offset, size, digest, seq)
        existing = self._chunks.get(cid)
        if existing is not None:
            if existing != record:
                raise IntegrityFailure(
                    f"chunk {cid} re-added with conflicting content "
                    f"(retry/duplication guard)"
                )
            return cid
        self._chunks[cid] = record
        return cid

    # -- lookup ------------------------------------------------------------

    def get(self, chunk_id: str) -> ChunkRecord:
        try:
            return self._chunks[chunk_id]
        except KeyError:
            raise KeyError(f"unknown chunk_id {chunk_id!r}") from None

    def chunks_in_offset_order(self) -> list[ChunkRecord]:
        """Chunks sorted by offset. Evidence ordering is offset order,
        not arrival (``seq``) order."""
        return sorted(self._chunks.values(), key=lambda r: r.offset)

    def total_size(self) -> int:
        """Total bytes covered, if chunks tile ``[0, total)``
        contiguously."""
        total = 0
        for record in self.chunks_in_offset_order():
            if record.offset != total:
                raise IntegrityFailure(
                    f"chunk at offset {record.offset} does not continue "
                    f"contiguous tile at {total}"
                )
            total += record.size
        return total

    # -- verification ------------------------------------------------------

    def verify_parent_child(self) -> bool:
        """Recompute every chunk ID from (offset, size, parent_digest)
        and confirm the cryptographic lineage chain is intact.

        Raises IntegrityFailure on any mismatch; returns True otherwise.
        """
        for record in self._chunks.values():
            recomputed = self.chunk_id(record.offset, record.size)
            if recomputed != record.chunk_id:
                raise IntegrityFailure(
                    f"chunk {record.chunk_id} failed parent->child lineage "
                    f"check (recomputed {recomputed})"
                )
        return True

    def order_independent_final_digest(self) -> str:
        """SHA-256 over the chunk identities accumulated in *offset*
        order. Arrival order (``seq``) has no effect on the result, so
        chunks completing out of order never change the final digest."""
        hasher = hashlib.sha256()
        for record in self.chunks_in_offset_order():
            hasher.update(record.chunk_id.encode("utf-8"))
            hasher.update(record.digest.encode("utf-8"))
        return hasher.hexdigest()


class BoundedStreamReader:
    """Streams one immutable object in bounded chunks.

    ``source`` is a file-like object (``seek`` + ``read``) positioned
    over the immutable bytes — e.g. a download handle from
    :class:`ps05.shared.ports.ObjectStorage` or a WORM readback
    stream. Each call to :meth:`read_chunk` seeks to exactly that
    chunk's range and reads exactly ``size`` bytes; the reader never
    materialises the whole object and never reads past a chunk's range.
    """

    #: Bounded buffer per read; a chunk is one read.
    def __init__(self, source: Any, manifest: ChunkManifest, position: int = 0) -> None:
        self._source = source
        self.manifest = manifest
        self._position = position

    def read_chunk(self, chunk_id: str) -> bytes:
        """Read exactly one chunk's byte range and verify its SHA-256
        digest. Raises IntegrityFailure on short reads or digest
        mismatch."""
        record = self.manifest.get(chunk_id)
        self._source.seek(record.offset)
        data = self._source.read(record.size)
        if len(data) != record.size:
            raise IntegrityFailure(
                f"chunk {chunk_id}: short read "
                f"({len(data)}/{record.size} bytes)"
            )
        if hashlib.sha256(data).hexdigest() != record.digest:
            raise IntegrityFailure(
                f"chunk {chunk_id}: digest mismatch "
                f"(expected {record.digest})"
            )
        self._position = record.offset + record.size
        return data

    def read(self, n: int) -> bytes:
        """Read at most ``n`` bytes from the cursor, without exceeding
        the object boundary implied by the manifest."""
        if n <= 0:
            return b""
        limit = self.manifest.total_size()
        if self._position >= limit:
            return b""
        self._source.seek(self._position)
        to_read = min(n, limit - self._position)
        data = self._source.read(to_read)
        self._position += len(data)
        return data

    def close(self) -> None:
        close = getattr(self._source, "close", None)
        if close is not None:
            close()

    def re_open(self) -> "BoundedStreamReader":
        """A fresh cursor over the same immutable object. The original
        reader's position is not shared or mutated."""
        return BoundedStreamReader(self._source, self.manifest, position=0)


class EvidenceStreamHandle:
    """TRD 23.17: a re-openable, independently readable primitive.

    Each consumer calls :meth:`open_reader` to get its own cursor over
    the *same* immutable object — there is no shared mutable
    file-pointer state, so no exhaustion or partial-read hazard.
    ``source_factory`` must return a fresh, independently positioned
    file-like over the object bytes on every call.
    """

    def __init__(
        self,
        source_factory: Callable[[], Any],
        manifest: ChunkManifest,
    ) -> None:
        if not callable(source_factory):
            raise TypeError("source_factory must be a zero-argument callable")
        self._source_factory = source_factory
        self.manifest = manifest

    def open_reader(self) -> BoundedStreamReader:
        """Open a new bounded, chunk-ID-keyed, digest-verifiable reader.
        Every call returns an independent cursor."""
        return BoundedStreamReader(self._source_factory(), self.manifest)
