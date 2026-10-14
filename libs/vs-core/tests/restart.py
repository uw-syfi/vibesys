"""When a test re-creates the host between steps.

A restart after every step equals the live run: vs-core proves that once, generically, over
generated runs (``test_flows_properties``, ``test_recovery_converges``, ``test_flows_restart``).
A rule's own test therefore steps live at pull-request depth and reloads between steps only at
nightly depth, where the whole suite is a restart sweep.
"""

from vs_core.api import (
    BlockRequest,
    CancelOwnedResource,
    InspectRequest,
    RequestPhase,
    RequestRecord,
    RunState,
    initial_state,
)
from vs_sim.api.testing import Depth, depth


def restarts() -> bool:
    """Whether tests reload the state between steps (nightly depth)."""
    return depth() is Depth.NIGHTLY


def restarted(
    record: RequestRecord,
    now_at: float,
    persisted: RunState | None = None,
    *,
    last_alive: float | None = None,
) -> RequestRecord:
    """``record`` as a host restarting at ``now_at`` finds it, after the last commit of ``persisted``.

    An open record's limit moves later by the downtime (from the later of the last commit and
    ``last_alive``, when the earlier host last proved itself alive), no further than the run deadline;
    recovery's own commands (inspect, cancel, block) keep theirs. Without ``last_alive``
    nothing moves. ``persisted`` defaults to the run of ``initial_state()``.
    """
    run = persisted or initial_state().run
    if (
        last_alive is None
        or isinstance(record.request, InspectRequest | CancelOwnedResource | BlockRequest)
        or record.phase
        not in (RequestPhase.PREPARED, RequestPhase.DISPATCHED, RequestPhase.RECONCILING)
    ):
        return record
    at = record.until.at + max(now_at - max(run.now_at, last_alive), 0.0)
    if record.until.at <= run.deadline_at:
        at = min(at, run.deadline_at)
    return record.model_copy(update={"until": record.until.model_copy(update={"at": at})})
