"""The run lifecycle: its triggers and the legal moves between statuses.

Pure module. It owns what a run's state may become and nothing else: no
locks, no journal, no I/O. The status vocabulary, ``RunStatus``, lives in the
light ``server.host_api`` with ``RunPhase``, the status a registry record
advertises, so the record can name it without loading the server.
:class:`~server.controller.RunController` owns the current value and the side effects of changing it, so the rules here can be
enumerated in a unit test without composing a server.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from server.host_api import RunStatus


class RunTrigger(StrEnum):
    """What happened to a run, in the vocabulary the controller observes.

    Triggers are facts, not commands: ``INVOCATION_FINISHED`` fires at every
    controlled invocation boundary whether or not a pause is pending, and the
    transition table decides whether that boundary is where a pause lands.
    """

    READY = "ready"
    """The run acquired its resources (image, container, project) and executes.

    Attaching the run's journal is not readiness: a run stays ``starting``
    through its image build and container start, whatever an operator
    requests meanwhile. Idempotent once the run is under way.
    """

    PAUSE_REQUESTED = "pause_requested"
    """An operator asked to pause at the next invocation boundary."""

    INVOCATION_FINISHED = "invocation_finished"
    """A controlled invocation reached its boundary."""

    RESUMED = "resumed"
    """An operator asked to resume, cancelling any pending pause or stop."""

    STOP_REQUESTED = "stop_requested"
    """An operator asked to end the run at the next invocation boundary."""

    COMPLETED = "completed"
    """The run finished its work."""

    FAILED = "failed"
    """The run stopped because of an error or an interruption."""


class IllegalRunTransitionError(Exception):
    """A trigger the run's current status cannot accept."""

    def __init__(self, current: RunStatus, trigger: RunTrigger) -> None:
        """Name the rejected pair so the caller's log identifies the bug."""
        super().__init__(
            f"a run in status {current.value!r} cannot accept trigger {trigger.value!r}"
        )
        self.current = current
        self.trigger = trigger


class RunRequest(StrEnum):
    """An operator request a starting run holds until it is ready."""

    PAUSE = "pause"
    STOP = "stop"


@dataclass(frozen=True)
class RunState:
    """A run's status, and the request it holds while it is still starting.

    Readiness and an operator's request are separate facts. A run stays
    ``starting`` until ``READY`` however often it is paused, resumed or
    stopped, so its startup stage and progress stay reported through the
    whole startup; ``requested`` remembers the last pause or stop asked for
    and ``READY`` lands the run in the status that request leads to.
    """

    status: RunStatus
    requested: RunRequest | None = None
    """The pending pause or stop of a starting run; ``None`` for every other status."""

    def __post_init__(self) -> None:
        """Reject a request on a run that is no longer starting."""
        if self.requested is not None and self.status is not RunStatus.STARTING:
            message = f"only a starting run holds a request, not a {self.status.value!r} one"
            raise ValueError(message)

    @property
    def pause_pending(self) -> bool:
        """A pause waits for its boundary: requested after readiness, or held while starting."""
        return self.status is RunStatus.PAUSING or self.requested is RunRequest.PAUSE

    @property
    def stop_pending(self) -> bool:
        """A stop waits for its boundary: requested after readiness, or held while starting."""
        return self.status is RunStatus.STOPPING or self.requested is RunRequest.STOP


_STARTING = RunState(RunStatus.STARTING)
_STARTING_PAUSE = RunState(RunStatus.STARTING, RunRequest.PAUSE)
_STARTING_STOP = RunState(RunStatus.STARTING, RunRequest.STOP)
_RUNNING = RunState(RunStatus.RUNNING)
_PAUSING = RunState(RunStatus.PAUSING)
_PAUSED = RunState(RunStatus.PAUSED)
_STOPPING = RunState(RunStatus.STOPPING)
_STOPPED = RunState(RunStatus.STOPPED)
_COMPLETED = RunState(RunStatus.COMPLETED)
_FAILED = RunState(RunStatus.FAILED)


def _starting_moves(
    held: RunState, *, ready: RunState
) -> dict[tuple[RunState, RunTrigger], RunState]:
    """The moves of a starting run holding ``held``'s request, which lands as ``ready``."""
    return {
        (held, RunTrigger.READY): ready,
        # A pause cannot downgrade a stop already pending.
        (held, RunTrigger.PAUSE_REQUESTED): (
            _STARTING_STOP if held.requested is RunRequest.STOP else _STARTING_PAUSE
        ),
        (held, RunTrigger.STOP_REQUESTED): _STARTING_STOP,
        # A resume cancels a pending pause; the run keeps starting. Nothing undoes a stop.
        (held, RunTrigger.RESUMED): (
            _STARTING_STOP if held.requested is RunRequest.STOP else _STARTING
        ),
        (held, RunTrigger.COMPLETED): _COMPLETED,
        (held, RunTrigger.FAILED): _FAILED,
    }


_TRANSITIONS: Final[MappingProxyType[tuple[RunState, RunTrigger], RunState]] = MappingProxyType(
    {
        # Requested while starting, a pause or stop is held, not entered: the
        # run reports ``starting`` until it is ready, and core lands the
        # request at the first boundary after that.
        **_starting_moves(_STARTING, ready=_RUNNING),
        **_starting_moves(_STARTING_PAUSE, ready=_PAUSING),
        **_starting_moves(_STARTING_STOP, ready=_STOPPING),
        # Core may land a held stop before the run is ready, which abandons the
        # startup: the run ends ``stopped`` without ever having run. A held
        # pause has no such boundary; it lands only after ``READY``.
        (_STARTING_STOP, RunTrigger.INVOCATION_FINISHED): _STOPPED,
        (_RUNNING, RunTrigger.READY): _RUNNING,
        (_RUNNING, RunTrigger.PAUSE_REQUESTED): _PAUSING,
        (_RUNNING, RunTrigger.INVOCATION_FINISHED): _RUNNING,
        (_RUNNING, RunTrigger.RESUMED): _RUNNING,
        (_RUNNING, RunTrigger.STOP_REQUESTED): _STOPPING,
        (_RUNNING, RunTrigger.COMPLETED): _COMPLETED,
        (_RUNNING, RunTrigger.FAILED): _FAILED,
        (_PAUSING, RunTrigger.READY): _PAUSING,
        (_PAUSING, RunTrigger.PAUSE_REQUESTED): _PAUSING,
        (_PAUSING, RunTrigger.INVOCATION_FINISHED): _PAUSED,
        (_PAUSING, RunTrigger.RESUMED): _RUNNING,
        # A stop supersedes the pause pending at the same boundary.
        (_PAUSING, RunTrigger.STOP_REQUESTED): _STOPPING,
        (_PAUSING, RunTrigger.COMPLETED): _COMPLETED,
        (_PAUSING, RunTrigger.FAILED): _FAILED,
        (_PAUSED, RunTrigger.READY): _PAUSED,
        (_PAUSED, RunTrigger.PAUSE_REQUESTED): _PAUSED,
        (_PAUSED, RunTrigger.INVOCATION_FINISHED): _PAUSED,
        (_PAUSED, RunTrigger.RESUMED): _RUNNING,
        # Leaving PAUSED releases the thread parked at the pause wait, which
        # then lands the stop at the boundary it is already standing on.
        (_PAUSED, RunTrigger.STOP_REQUESTED): _STOPPING,
        (_PAUSED, RunTrigger.COMPLETED): _COMPLETED,
        (_PAUSED, RunTrigger.FAILED): _FAILED,
        (_STOPPING, RunTrigger.READY): _STOPPING,
        # A pause cannot downgrade a stop already pending at the boundary.
        (_STOPPING, RunTrigger.PAUSE_REQUESTED): _STOPPING,
        (_STOPPING, RunTrigger.INVOCATION_FINISHED): _STOPPED,
        # A stop only escalates: a resume never cancels it (the run's control refuses one).
        (_STOPPING, RunTrigger.RESUMED): _STOPPING,
        (_STOPPING, RunTrigger.STOP_REQUESTED): _STOPPING,
        (_STOPPING, RunTrigger.COMPLETED): _COMPLETED,
        (_STOPPING, RunTrigger.FAILED): _FAILED,
    }
)
"""Every legal move out of a state that has not ended, as data.

Absent pairs are illegal and raise. A state that has ended is absorbing and
is deliberately not listed: :func:`transition` answers for it first.
"""


def transition(current: RunState, trigger: RunTrigger) -> RunState:
    """Return the state ``trigger`` produces from ``current``.

    An ended status absorbs every trigger, which is what makes ``finish``
    idempotent and lets a late ``/resume`` or a boundary reached after the run
    stopped be a no-op rather than an error.

    Raises:
        IllegalRunTransitionError: The pair is not in the table, so the caller
            observed something the lifecycle says cannot happen.
    """
    if current.status.has_ended:
        return current
    result = _TRANSITIONS.get((current, trigger))
    if result is None:
        raise IllegalRunTransitionError(current.status, trigger)
    return result
