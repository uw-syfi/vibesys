"""Every compute backend the enum names has a registered implementation.

A ``ComputeBackend`` member without a factory in the registry is accepted by
config parsing and the CLI, then fails only when a run asks to build it. This
completeness check fails CI instead, naming the backend that is missing.
"""

from __future__ import annotations

from vs_sandbox.api import ComputeBackend, registered_compute_backends


def test_every_compute_backend_has_a_registered_factory() -> None:
    missing = sorted(b.value for b in set(ComputeBackend) - registered_compute_backends())

    assert not missing, (
        f"ComputeBackend members with no registered factory: {missing}; register each "
        "in vs_sandbox.compute_backends._register_defaults"
    )
