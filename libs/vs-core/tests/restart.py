"""When a test re-creates the host between steps.

A restart after every step equals the live run: vs-core proves that once, generically, over
generated runs (``test_flows_properties``, ``test_recovery_converges``, ``test_flows_restart``).
A rule's own test therefore steps live at pull-request depth and reloads between steps only at
nightly depth, where the whole suite is a restart sweep.
"""

from vs_sim.api.testing import Depth, depth


def restarts() -> bool:
    """Whether tests reload the state between steps (nightly depth)."""
    return depth() is Depth.NIGHTLY
