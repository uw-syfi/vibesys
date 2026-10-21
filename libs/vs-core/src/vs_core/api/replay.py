"""Pure keyed ordered replay. No storage or dispatch is performed by this API.

Identity is validated before matching. Shells retain protobuf records and drive
accepted observations in log order; flow continuations use only this scheduler.
"""

from vs_core.flows._replay_identity import (
    EMPTY_CAUSALITY,
    ENCODING_VERSION,
    ClassifiedContext,
    EncodedIdentity,
    ReplayIdentityError,
    SemanticKey,
    chain_dependency,
    classify_context,
    encode_identity,
)

__all__ = [
    "EMPTY_CAUSALITY",
    "ENCODING_VERSION",
    "ClassifiedContext",
    "EncodedIdentity",
    "ReplayIdentityError",
    "SemanticKey",
    "chain_dependency",
    "classify_context",
    "encode_identity",
]
