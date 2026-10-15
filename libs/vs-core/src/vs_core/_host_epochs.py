"""Host liveness as recorded fact: the epoch log, and the downtime it proves.

A host is the process that runs a run's core loop while holding the store lease; each lease
acquisition is an epoch. Core records when each epoch started and when its host last proved
itself alive (a lease renewal or a commit). The time no host was alive is the gaps between
consecutive epochs in that log, and ``downtime`` is the only place that computes it.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING

from ._registry import ContractError
from .types.kernel import AreaChange
from .types.records import HOST_LOG_LENGTH, HostEpoch, HostRenewed

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .types.kernel import RecordsContext
    from .types.records import RecordsEvent, RecordsState


def downtime(hosts: Sequence[HostEpoch]) -> float:
    """The time no host was alive between the first epoch in ``hosts`` and the last one's start.

    Each gap runs from when a host last proved itself alive to when the next one started. A
    host that died later than its last proof makes the gap longer than the real downtime by
    at most the time since that proof (a live host renews its lease, so at most one renewal
    interval). Never negative.
    """
    return sum(max(later.started_at - earlier.alive_at, 0.0) for earlier, later in pairwise(hosts))


def opened(
    hosts: tuple[HostEpoch, ...], epoch: int, now_at: float, last_commit_at: float
) -> tuple[HostEpoch, ...]:
    """The log once epoch ``epoch`` starts at ``now_at``.

    ``last_commit_at`` is the previous host's last commit, which proves it alive as a renewal
    does. Keeps the newest ``HOST_LOG_LENGTH`` epochs: the last downtime needs no more.
    """
    closed = hosts
    if hosts:
        last = hosts[-1]
        if epoch <= last.epoch:
            raise ContractError(("epoch",), "a new host epoch must follow the recorded ones")
        closed = (
            *hosts[:-1],
            last.model_copy(update={"alive_at": max(last.alive_at, last_commit_at)}),
        )
    started = HostEpoch(epoch=epoch, started_at=now_at, alive_at=now_at)
    return (*closed, started)[-HOST_LOG_LENGTH:]


def renewed(
    state: RecordsState, _context: RecordsContext, event: RecordsEvent
) -> AreaChange[RecordsState]:
    """Record that the current epoch's host was alive at ``event.now_at``."""
    if not isinstance(event, HostRenewed):
        raise ContractError(("event",), "event is not a lease renewal")
    if not state.hosts or state.hosts[-1].epoch != event.epoch:
        raise ContractError(("epoch",), "a lease renewal names an epoch that is not current")
    current = state.hosts[-1]
    if event.now_at <= current.alive_at:
        return AreaChange(state=state)
    current = current.model_copy(update={"alive_at": event.now_at})
    return AreaChange(state=state.model_copy(update={"hosts": (*state.hosts[:-1], current)}))
