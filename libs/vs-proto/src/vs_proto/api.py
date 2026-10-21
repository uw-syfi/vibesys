"""Run store and host API contracts generated from the root proto tree.

Messages encode with ``to_binary`` and decode with ``from_binary``. Call
``validate`` at input boundaries; it raises ``ValidationError`` when schema
rules fail. Decoding preserves unknown fields for additive store evolution.
The two schema hashes identify the canonical descriptors, not generated code.
"""

from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp
from protovalidate import ValidationError, validate

from vs_proto._generated.schema_hashes import API_SCHEMA_HASH, STORE_SCHEMA_HASH
from vs_proto._generated.vibesys.api.v1.host_pb import (
    HelloRequest,
    HelloResponse,
    TailRequest,
    TailResponse,
)
from vs_proto._generated.vibesys.store.v1.record_pb import EpochStarted, LegacyEvent, Record

__all__ = [
    "API_SCHEMA_HASH",
    "STORE_SCHEMA_HASH",
    "EpochStarted",
    "HelloRequest",
    "HelloResponse",
    "LegacyEvent",
    "Oneof",
    "Record",
    "TailRequest",
    "TailResponse",
    "Timestamp",
    "ValidationError",
    "validate",
]
