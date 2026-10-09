"""A host run environment for tests that exercises an agent running outside Docker.

The host environment refuses to start without the platform's host sandbox. This
spec substitutes a check that passes, so a test can open an agent on the host on
any machine. Production code never builds it.
"""

from __future__ import annotations

from vibesys.api.request import RunEnvironmentSpec


def unconfined_host_spec() -> RunEnvironmentSpec:
    """Return a host environment spec whose confinement check always passes."""
    return RunEnvironmentSpec("host", {"build_sandbox": lambda *_args, **_kwargs: None})
