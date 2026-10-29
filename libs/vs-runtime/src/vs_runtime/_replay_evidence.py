"""Explicit evidence projections at the legacy lifecycle-owner boundary.

Native acceptance is the generated fact contract. The legacy measurement owner
still uses a larger observation envelope; encoding projects its positive source
correspondence without duplicating resource lifecycle state. Adoption uses refs
only to look up commit-message metrics: its projection never fabricates a legacy
observation or grants scientific acceptance authority. The complete native proof
stays in the immutable replay request.
"""

from typing import cast

from pydantic import TypeAdapter

from vs_core.api import EvidenceRef, values
from vs_decode.api import Unreadable, decode_json
from vs_proto.api import core_agent_facts, core_common, core_vocabulary, validate
from vs_runtime.api.core_values import from_proto, to_proto

_MAX_EVIDENCE_BYTES = 1024 * 1024
_NATIVE_EVIDENCE = TypeAdapter(values.EvidenceRef)


def encode_evidence(value: EvidenceRef) -> core_agent_facts.EvidenceRef | Unreadable:
    projected = value.model_copy(update={"acceptance_receipt": None})
    native = decode_json(
        projected.model_dump_json(),
        _NATIVE_EVIDENCE,
        source="legacy replay evidence",
        max_bytes=_MAX_EVIDENCE_BYTES,
    )
    if isinstance(native, Unreadable):
        return native
    message = cast("core_agent_facts.EvidenceRef", to_proto(native))
    if value.acceptance_receipt is not None:
        source = value.acceptance_receipt.observation
        message.acceptance_receipt = core_agent_facts.EvidenceAcceptanceReceipt(
            source_request=cast("core_common.RequestId", to_proto(source.request_id)),
            scope=cast("core_common.Scope", to_proto(source.scope)),
            observation_sequence=str(source.sequence),
            status=core_vocabulary.ObservationStatus[source.status.name],
        )
    validate(message)
    return message


def adoption_evidence(message: core_agent_facts.EvidenceRef) -> EvidenceRef | Unreadable:
    """The legacy workspace owner's commit-message ref, carrying no fabricated receipt."""
    native = cast("values.EvidenceRef", from_proto(message))
    projected = native.model_copy(update={"acceptance_receipt": None})
    return decode_json(
        projected.model_dump_json(),
        EvidenceRef,
        source="native replay evidence",
        max_bytes=_MAX_EVIDENCE_BYTES,
    )
