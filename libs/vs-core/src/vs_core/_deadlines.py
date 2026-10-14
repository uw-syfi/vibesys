"""The one rule for how a ``Within`` and the run deadline become a record's ``Until``.

A wait states how long the work may take. The run may have less time left than that,
which is not malformed: core caps the stored time at the run deadline. A run deadline
that is not after the run clock has no time left; only then is new work refused, and
core's own cleanup requests still get their bounded time: each carries an explicit
``Within`` of at least ``limits.cleanup_bound`` (see ``cleanup``), never a limit of zero
and never a wait without a bound, so a stopped run can always close.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.records import Outbound, within
from .types.until import FromDispatch, Until, Within

if TYPE_CHECKING:
    from .types.kernel import RunState
    from .types.records import Request
    from .types.until import Watch


def resolve_until(run: RunState, within: Within) -> Until:
    """The absolute time and watches core stores for ``within`` accepted now.

    While the run has time left the stored time is capped at the run deadline. Once the
    run deadline has passed, nothing caps it: the request keeps the bounded ``limit`` it
    was given (core's cleanup budget, for its own requests), so cleanup and reconciliation
    can still run. A limit of zero or less is refused before this is reached; the time
    never precedes the clock.
    """
    at = run.now_at + max(within.limit, 0.0)
    if run.deadline_at > run.now_at:
        at = min(at, run.deadline_at)
    return Until(at=at, watches=within.watches)


def queued_until(run: RunState, unstarted: FromDispatch) -> Until:
    """What a request that has not been dispatched yet is held to: the run, not its limit.

    The limit starts when the shell dispatches the request (``started_until``), so a request
    waiting for a free slot spends none of it. Until then only the run deadline ends it.
    """
    if run.deadline_at > run.now_at:
        return Until(at=run.deadline_at, watches=unstarted.watches)
    return Until(at=run.now_at + max(unstarted.limit, 0.0), watches=unstarted.watches)


def from_dispatch(run: RunState, until: Until) -> FromDispatch:
    """``until`` as the duration a request accepted now may take once it is dispatched."""
    return FromDispatch(limit=max(until.at - run.now_at, 0.0), watches=until.watches)


def started_until(run: RunState, limit: float, watches: tuple[Watch, ...]) -> Until:
    """The ``Until`` of a request dispatched at the run clock: its limit counted from now."""
    return resolve_until(run, Within(limit=limit, watches=watches))


def limit_to(run: RunState, at: float) -> float:
    """The duration from the run clock to the absolute time ``at`` (never negative)."""
    return max(at - run.now_at, 0.0)


def bound_to_run(run: RunState, requested: float) -> float:
    """A stated time (an agent's wait deadline), no later than the run deadline."""
    return min(requested, run.deadline_at)


def time_remains(run: RunState, requested: float) -> bool:
    """Whether any time is left between the run clock and the bounded time."""
    return bound_to_run(run, requested) > run.now_at


def cleanup(run: RunState, request: Request) -> Outbound:
    """``request`` as core's own cleanup or inspection, with its own explicit bounded time.

    While the run has more time left than ``limits.cleanup_bound`` the limit is that time
    (``resolve_until`` caps it at the run deadline, so a host that restarts mid-cleanup
    still finds the request waiting). From the run deadline on, nothing is left to
    count, so the configured cleanup budget is the limit instead of zero.
    """
    return within(request, max(limit_to(run, run.deadline_at), run.limits.cleanup_bound))
