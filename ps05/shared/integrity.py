"""CEIPL/EIV integrity surface (TRD 4 / 19 / 23.8 / 23.17).

Pre-decode dual hashing with ``hashlib.sha256`` + ``hashlib.sha3_512``
(the dual digest is the recorded value of the evidence *before* any
decode engine runs, so post-decode tampering is detectable).

Known-answer empty-input vectors:
- ``sha256(b'')``   = ``e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855``
- ``sha3_512(b'')`` =
  ``a69f73cca23a9ac5c8b567dc185a756e97c982164fe25859e0d1dcc1475c80a615b2123af1f5f94c11e3e9402c3ac558f500199d95b6d3e301758586281dcd26``
"""

from __future__ import annotations

import hashlib
import os
import typing

try:
    from ps05.core.errors import ErrorClass  # type: ignore
except ImportError:  # standalone-testability guard (ps05.core may be absent)
    ErrorClass = None  # type: ignore[assignment]


class IntegrityFailure(Exception):
    """Raised on any hash mismatch, short read, or lineage break.

    Maps to ``ErrorClass.INTEGRITY_FAILURE`` (TRD 23.10 taxonomy, §19):
    permanent, never auto-retried, hard-blocks downstream HARD
    dependents.
    """

    def __init__(self, message: str, *, error_class=None) -> None:
        super().__init__(message)
        if ErrorClass is not None and error_class is None:
            try:
                error_class = ErrorClass.INTEGRITY_FAILURE
            except AttributeError:
                error_class = None
        self.error_class = error_class


_STREAM_BUFFER = 1 << 20  # 1 MiB bounded read


def _stream_dual_hash(reader: typing.BinaryIO) -> tuple[str, str]:
    """Dual hash a file-like in bounded chunks. Never reads the whole
    object into RAM; each iteration materialises at most one buffer."""
    sha256 = hashlib.sha256()
    sha3_512 = hashlib.sha3_512()
    while True:
        block = reader.read(_STREAM_BUFFER)
        if not block:
            break
        sha256.update(block)
        sha3_512.update(block)
    return sha256.hexdigest(), sha3_512.hexdigest()


class CEIPLIntegrity:
    """Content-Evidence Integrity & Provenance Ledger (CEIPL) + Evidence
    Integrity Verification (EIV) operations.

    NOTE: a matching hash establishes the *bytes* are unaltered since
    the digest was recorded; it does NOT establish who acquired the
    evidence. Custody questions are answered by the append-only custody
    ledger (``record_custody`` / :class:`ps05.shared.ports.CustodyLedger`).
    """

    # -- pre-decode dual hashing ------------------------------------------

    @staticmethod
    def pre_decode_dual_hash(path_or_reader: str | typing.BinaryIO) -> tuple[str, str]:
        """Dual hash original evidence pre-decode.

        ``path_or_reader`` is a filesystem path (streamed in bounded
        reads) or an already-open binary file-like. Returns
        ``(sha256_hex, sha3_512_hex)``.
        """
        if isinstance(path_or_reader, (str, os.PathLike)):
            with open(os.fspath(path_or_reader), "rb") as fh:
                return _stream_dual_hash(fh)
        return _stream_dual_hash(path_or_reader)

    # -- Merkle over chunk digests ----------------------------------------

    @staticmethod
    def build_merkle_root(chunk_digests: list[str]) -> str:
        """Build a Merkle root over chunk SHA-256 hex digests.

        Leaves are the given digests (in list order — the caller supplies
        offset order, never arrival order); pairs are
        ``sha256(left || right)`` with hex concatenation, odd levels are
        promoted. Empty input yields the sha256 empty-digest constant
        (see module docstring known-answer vector)."""
        if not chunk_digests:
            return hashlib.sha256(b"").hexdigest()
        level = list(chunk_digests)
        while len(level) > 1:
            pairs = [
                hashlib.sha256(
                    (level[i] + level[i + 1]).encode("utf-8")
                ).hexdigest()
                for i in range(0, len(level) - 1, 2)
            ]
            if len(level) % 2:
                pairs.append(level[-1])
            level = pairs
        return level[0]

    # -- fixity ------------------------------------------------------------

    @staticmethod
    def verify_fixity(reader: typing.BinaryIO) -> bool:
        """Re-hash the readable object and compare against the digests
        recorded on its manifest. ``reader`` must be a
        :class:`ps05.shared.streaming.BoundedStreamReader` (or any
        file-like exposing ``.manifest`` with ``expected_sha256`` /
        ``expected_sha3_512``).

        Raises IntegrityFailure on mismatch or missing expectations;
        returns True when the fixity check passes.
        """
        manifest = getattr(reader, "manifest", None)
        if manifest is None:
            raise IntegrityFailure(
                "verify_fixity requires a reader with a .manifest "
                "attribute"
            )
        expected_sha256 = getattr(manifest, "expected_sha256", None)
        expected_sha3_512 = getattr(manifest, "expected_sha3_512", None)
        if expected_sha256 is None and expected_sha3_512 is None:
            raise IntegrityFailure(
                "manifest carries no expected whole-object digests to "
                "verify fixity against"
            )
        seekable = reader
        while hasattr(seekable, "_source") and not hasattr(seekable, "seek"):
            seekable = seekable._source
        if hasattr(seekable, "seek"):
            seekable.seek(0)
        actual_sha256, actual_sha3_512 = _stream_dual_hash(seekable)
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise IntegrityFailure(
                f"fixity failed: sha256 {actual_sha256} != "
                f"expected {expected_sha256}"
            )
        if expected_sha3_512 is not None and actual_sha3_512 != expected_sha3_512:
            raise IntegrityFailure(
                f"fixity failed: sha3_512 {actual_sha3_512} != "
                f"expected {expected_sha3_512}"
            )
        return True

    # -- custody (append-only) ----------------------------------------------

    @staticmethod
    def record_custody(
        ledger: object,
        actor: str,
        action: str,
        resource: str,
        context: dict,
        clock: object | None = None,
    ) -> None:
        """Append a custody event to an
        :class:`ps05.shared.ports.CustodyLedger`-compatible object.

        The ledger is append-only: corrections are *new* events, never
        rewrites. ``context`` is JSON-serialised for the event record.
        """
        from ps05.shared.ports import CustodyEvent

        at = clock.now_iso8601() if clock is not None else ""
        event = CustodyEvent(
            actor=actor,
            action=action,
            resource=resource,
            context=dict(context or {}),
            at=at,
        )
        ledger.append(event)
