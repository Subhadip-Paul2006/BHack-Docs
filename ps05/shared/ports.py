"""Ports (Protocol contracts) consumed by PS-05 engines and infra.

These are structural Protocols: any object with the matching method
signatures satisfies them. They define the *shape* of dependencies so
engine code can be tested against fakes without importing adapters.

Universal streaming policy (TRD 4 / 23.8): callers stream from
``EvidenceStore`` / ``ObjectStorage`` in bounded chunks; they never load
whole large payloads into RAM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class EvidenceStore(Protocol):
    """Commit-to-WORM evidence store.

    Staging is writable/rewritable; the committed immutable object is
    never overwritten after WORM commit (CEIPL/EIV, TRD 19 / 23.8).
    """

    def write_staging(self, evidence_id: str, chunk_id: str, data: bytes) -> None:
        """Write one chunk to the (rewritable) staging area."""

    def commit_immutable(self, evidence_id: str, manifest: object) -> str:
        """Atomically move staging to the immutable object store.

        Returns the immutable object key / URI.
        """

    def read(self, object_key: str, offset: int, length: int) -> bytes:
        """Read exactly ``length`` bytes at ``offset``. Bounded; never the
        whole object."""

    def verify_fixity(self, object_key: str, chunk_id: str, expected_digest: str) -> bool:
        """Re-hash one chunk of the immutable object and compare digests."""


@runtime_checkable
class ObjectStorage(Protocol):
    """Raw immutable object storage keyed by a string key.

    Distinct from :class:`EvidenceStore`: no evidence semantics, just
    byte ranges. Used where engines need to stream a raw object.
    """

    def get_range(self, key: str, offset: int, length: int) -> bytes:
        """Return ``length`` bytes starting at ``offset``."""

    def open_reader(self, key: str) -> object:
        """Return a re-openable bounded reader (see
        :class:`ps05.shared.streaming.BoundedStreamReader`)."""


@runtime_checkable
class Hasher(Protocol):
    """Dual-hashing surface (TRD 23.8: pre-decode dual hash).

    The two digests are computed over the *original* evidence bytes,
    pre-decode. A matching pair establishes the bytes are unaltered; it
    does NOT establish who acquired the evidence.
    """

    def sha256(self, data: bytes) -> str:
        """SHA-256 hex digest of the input."""

    def sha3_512(self, data: bytes) -> str:
        """SHA3-512 (NIST) hex digest of the input."""

    def stream_hash(self, reader: object, chunk_bytes: int = 1 << 20) -> tuple[str, str]:
        """Stream a reader in bounded chunks; returns
        ``(sha256, sha3_512)`` hex digests without materialising the whole
        payload."""


@runtime_checkable
class Clock(Protocol):
    """Deterministic time source; tests inject a fixed value."""

    def now_iso8601(self) -> str:
        """Current time as an ISO-8601 UTC string."""


@dataclass(frozen=True)
class CustodyEvent:
    """One append-only custody-ledger event.

    Corrections are new events, never rewrites of prior ones.
    """

    actor: str
    action: str
    resource: str
    context: dict
    at: str


@runtime_checkable
class CustodyLedger(Protocol):
    """Append-only custody ledger (TRD 19 / 23.8).

    NOTE: a matching hash / custody record does NOT establish who
    acquired the evidence; it only records that a custody action was
    observed by the system.
    """

    def append(self, event: CustodyEvent) -> None:
        """Append an event. The ledger is append-only; entries are never
        rewritten or removed."""
