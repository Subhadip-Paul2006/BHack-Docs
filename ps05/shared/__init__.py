"""PS-05 shared contracts: streaming policy, integrity, ingestion.

Consumed by every engine, infra, and api module. This package is the
universal streaming policy + CEIPL/EIV integrity surface (TRD 4, 19,
23.8, 23.17) and must stay importable standalone (no hard dependency on
``ps05.core``).
"""

from ps05.shared.ports import (
    Clock,
    CustodyLedger,
    EvidenceStore,
    Hasher,
    ObjectStorage,
)
from ps05.shared.integrity import CEIPLIntegrity, IntegrityFailure
from ps05.shared.ingestion import EvidenceRecord, plan_chunks, register_evidence, stage_then_commit
from ps05.shared.streaming import BoundedStreamReader, ChunkManifest, EvidenceStreamHandle

__all__ = [
    "BoundedStreamReader",
    "CEIPLIntegrity",
    "ChunkManifest",
    "Clock",
    "CustodyLedger",
    "EvidenceRecord",
    "EvidenceStreamHandle",
    "EvidenceStore",
    "Hasher",
    "IntegrityFailure",
    "ObjectStorage",
    "plan_chunks",
    "register_evidence",
    "stage_then_commit",
]
