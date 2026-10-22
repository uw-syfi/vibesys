"""Explicit evidence projections at the legacy lifecycle-owner boundary.

Native acceptance is the generated fact contract. The legacy measurement owner
still uses a larger observation envelope; encoding projects its positive source
correspondence without duplicating resource lifecycle state. Adoption uses refs
only to look up commit-message metrics: its projection never fabricates a legacy
observation or grants scientific acceptance authority. The complete native proof
stays in the immutable replay request.
"""

from typing import cast

from vs_core.api import EvidenceRef, values
from vs_proto.api import core_agent_facts, core_common, core_vocabulary, validate
from vs_runtime.api.core_values import from_proto, to_proto


def encode_evidence(value: EvidenceRef) -> core_agent_facts.EvidenceRef:
    projected = value.model_copy(update={"acceptance_receipt": None})
    native = values.EvidenceRef.model_validate_json(projected.model_dump_json())
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


def adoption_evidence(message: core_agent_facts.EvidenceRef) -> EvidenceRef:
    """The legacy workspace owner's commit-message ref, carrying no fabricated receipt."""
    native = cast("values.EvidenceRef", from_proto(message))
    projected = native.model_copy(update={"acceptance_receipt": None})
    return EvidenceRef.model_validate_json(projected.model_dump_json())
