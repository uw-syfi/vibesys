"""How a run on the step loop starts from its log, as the product starts it and a simulation does.

``start_run`` takes the run's log and what a process knows of it and returns the opened log and
the search as the log left it: the search restored from its advance records, the log opened over
the cache (``state.pb``) when it is given one, and whether the log held a start of the run, so
the process is a resume. ``resume_paused`` is the operator's Resume that a relaunch is: a run
that stood paused, draining or parked when its last process ended does not move until it is
resumed, and the loop returns at once for a run at rest, so the Resume is in the log before the
loop starts.

These are the steps ``vibesys.run.core_run.drive_core_run`` and ``vs_dst``'s step host share, so
a startup defect is one a simulation can reach: neither composes its own copy of them.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vs_core.api import (
    ControlId,
    ControlInput,
    ControlRefusedError,
    RunControlEvent,
    RunStatus,
)
from vs_core.api.recovery import RunRest, at_rest, starts_run
from vs_proto.api import Timestamp
from vs_runtime._core_inputs import SearchRun
from vs_runtime._fold_keeper import CACHE_EVERY
from vs_runtime._run_log_step_log import RunLogStepLog

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_core.api import CoreState, OperationRegistry, SearchDriver
    from vs_proto.api import Record
    from vs_runlog.api import ConfirmedLog, FoldCache
    from vs_runtime._run_log_step_log import RestAnnouncer
    from vs_sim.api import Clock


@dataclass(frozen=True)
class RunOpening[C]:
    """What a process brings to open a run besides its log: the search, the state a new run begins from."""

    driver: SearchDriver[Any, C]
    config: C
    registry: OperationRegistry
    seed: CoreState | None
    """The state a log with no start of the run gets as its first record (`RunLogStepLog.open`)."""
    clock: Clock
    stamp: Callable[[], Timestamp] = Timestamp
    cache: FoldCache | None = None
    """``state.pb``: the start folds only the records after it, and the log keeps it."""
    cache_every: int = CACHE_EVERY
    """Core records a process appends between two writes of the cache while the run goes on."""
    announcer: RestAnnouncer | None = None


@dataclass(frozen=True)
class StartedRun[C]:
    """A run's log opened, and the search it restored."""

    log: RunLogStepLog
    search: SearchRun[Any, C]
    resumed: bool
    """Whether the log already held a start of the run: this process resumes it."""


def start_run[C](
    log: ConfirmedLog, records: Sequence[Record], opening: RunOpening[C], *, epoch: int
) -> StartedRun[C]:
    """Open the run ``log`` holds, in ``epoch``, over ``records`` the caller read of it.

    ``records`` is the log's content before the open (the caller needed it to start the run's
    clock), so the log is not read for it again: the search is restored from it, and the start
    is a resume if it holds a start of the run.
    """
    search: SearchRun[Any, C] = SearchRun.restored(
        records, opening.driver, opening.config, opening.registry
    )
    opened = RunLogStepLog.open(
        log,
        epoch=epoch,
        seed=opening.seed,
        registry=opening.registry,
        clock=opening.clock,
        search=search,
        stamp=opening.stamp,
        cache=opening.cache,
        cache_every=opening.cache_every,
        announcer=opening.announcer,
    )
    return StartedRun(opened, search, resumed=any(starts_run(record) for record in records))


def resume_paused(log: RunLogStepLog, *, now_at: float, epoch: int, resumed: bool) -> None:
    """Append the operator's Resume when this process resumes a run that stood paused or parked.

    A process that opens a run that already started is the operator's resume of it. A run that
    is running, or whose Resume core refuses (it is finishing), takes none: the loop then goes
    on as it would have, and a refusal is not an error of the start.
    """
    state = log.state
    paused = at_rest(state) is RunRest.PARKED or state.run.status in (
        RunStatus.PAUSED,
        RunStatus.DRAINING,
    )
    if not (resumed and paused):
        return
    resume = ControlInput(
        control_id=ControlId(root=f"operator-resume:{epoch}"), action="resume", when="now"
    )
    with contextlib.suppress(ControlRefusedError):
        log.input(RunControlEvent(control=resume, now_at=now_at))
