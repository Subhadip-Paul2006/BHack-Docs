"""Integrity-surface regression tests: known-answer vectors, dual hashing, fixity, custody ledger."""

from __future__ import annotations

import hashlib
import io

import pytest

from ps05.core.errors import ErrorClass
from ps05.shared.integrity import CEIPLIntegrity, IntegrityFailure
from ps05.shared.ports import CustodyEvent
from ps05.shared.streaming import BoundedStreamReader, ChunkManifest

#: Documented empty-input vectors (see the ps05.shared.integrity module docstring).
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_EMPTY_SHA3_512 = (
    "a69f73cca23a9ac5c8b567dc185a756e97c982164fe25859e0d1dcc1475c80a615"
    "b2123af1f5f94c11e3e9402c3ac558f500199d95b6d3e301758586281dcd26"
)


def test_known_answer_vectors_agree_with_hashlib():
    # The module's second algorithm is hashlib.sha3_512; its empty-input
    # vector matches hashlib's own (computed at write time, not guessed).
    assert hashlib.sha256(b"").hexdigest() == _EMPTY_SHA256
    assert hashlib.sha3_512(b"").hexdigest() == _EMPTY_SHA3_512
    # The empty chunk-list Merkle root degenerates to the sha256 empty digest.
    assert CEIPLIntegrity.build_merkle_root([]) == _EMPTY_SHA256


def test_pre_decode_dual_hash_matches_hashlib_directly():
    payload = b"evidence-bytes" * 512
    sha256_hex, sha3_hex = CEIPLIntegrity.pre_decode_dual_hash(io.BytesIO(payload))
    assert sha256_hex == hashlib.sha256(payload).hexdigest()
    assert sha3_hex == hashlib.sha3_512(payload).hexdigest()

    empty_256, empty_512 = CEIPLIntegrity.pre_decode_dual_hash(io.BytesIO(b""))
    assert (empty_256, empty_512) == (_EMPTY_SHA256, _EMPTY_SHA3_512)


def _reader_with_expected(payload: bytes, expected_sha256: str, expected_sha3_512: str) -> BoundedStreamReader:
    manifest = ChunkManifest(expected_sha256=expected_sha256, expected_sha3_512=expected_sha3_512)
    return BoundedStreamReader(io.BytesIO(payload), manifest)


def test_fixity_passes_on_intact_bytes():
    payload = b"intact-evidence"
    reader = _reader_with_expected(
        payload,
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha3_512(payload).hexdigest(),
    )
    assert CEIPLIntegrity.verify_fixity(reader) is True


def test_fixity_fails_after_one_byte_tamper():
    intact = b"intact-evidence-bytes"
    tampered = bytearray(intact)
    tampered[4] ^= 0xFF  # flip exactly one byte
    reader = _reader_with_expected(
        bytes(tampered),
        hashlib.sha256(intact).hexdigest(),
        hashlib.sha3_512(intact).hexdigest(),
    )
    with pytest.raises(IntegrityFailure) as excinfo:
        CEIPLIntegrity.verify_fixity(reader)
    # ps05.core.errors imports cleanly, so the real exception carries the
    # taxonomy code from TRD 23.10.
    assert excinfo.value.error_class is ErrorClass.INTEGRITY_FAILURE


class _RecordingLedger:
    """Minimal CustodyLedger fake: records every appended event verbatim."""

    def __init__(self) -> None:
        self.events: list[CustodyEvent] = []

    def append(self, event: CustodyEvent) -> None:
        self.events.append(event)


def test_custody_correction_appends_without_rewriting():
    ledger = _RecordingLedger()
    CEIPLIntegrity.record_custody(
        ledger,
        actor="officer-a",
        action="COLLECT",
        resource="ev-1",
        context={"note": "original"},
    )
    original = ledger.events[0]
    CEIPLIntegrity.record_custody(
        ledger,
        actor="officer-b",
        action="CORRECTION",
        resource="ev-1",
        context={"note": "corrected"},
    )
    assert len(ledger.events) == 2
    # The correction is a new event; the earlier one keeps its original values.
    assert original.action == "COLLECT"
    assert original.context == {"note": "original"}
    assert ledger.events[1].action == "CORRECTION"
    assert ledger.events[1].context == {"note": "corrected"}
