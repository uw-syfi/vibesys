"""When a test re-creates the host between steps.

A restart after every step equals the live run: vs-core proves that once, generically, over
generated runs (``test_flows_properties``, ``test_recovery_converges``, ``test_flows_restart``).
A rule's own test therefore steps live at pull-request depth and reloads between steps only at
nightly depth, where the whole suite is a restart sweep.
"""

from vs_core.api import RequestRecord
from vs_sim.api.testing import Depth, depth


def restarts() -> bool:
    """Whether tests reload the state between steps (nightly depth)."""
    return depth() is Depth.NIGHTLY


def unaddressed(record: RequestRecord) -> RequestRecord:
    """*record* apart from the reconciliation commands sent about it (``addressed_by``).

    Recovery sends inspections and cancels about the records it reconciles; a test of what it
    does to a record's own facts compares the rest. The addressing is tested on its own.
    """
    return record.model_copy(update={"addressed_by": ()})
