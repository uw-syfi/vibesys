"""Independent canonical fact fingerprints for public contract fixtures."""

import hashlib
import json

import vs_core.api as core


def value_digest(value: core.Value) -> str:
    """Build the persisted fingerprint from canonical ingress, before corruption."""
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def canonical_source(
    request: core.Request,
    lifecycle: core.LifecycleClass = core.LifecycleClass.QUERY,
    observation: core.Observation | None = None,
) -> core.RequestRecord:
    """The recorded outbox row that certifies a release owner's request."""
    assert request.request_id is not None
    return core.RequestRecord(
        request_id=request.request_id,
        request=request,
        payload_digest=value_digest(request),
        lifecycle=lifecycle,
        phase=core.RequestPhase.COMPLETED,
        observation=observation,
        until=core.Until(at=100.0),
        addressed_by=(),
    )
