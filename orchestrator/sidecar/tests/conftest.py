"""Test configuration: Hypothesis runs a fixed example set, and a failure prints how to replay it."""

from __future__ import annotations

from hypothesis import HealthCheck, settings

settings.register_profile(
    "sidecar",
    max_examples=200,
    deadline=None,
    derandomize=False,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("sidecar")
