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
