"""The run lifecycle: its statuses, its triggers, and the legal moves between.

Pure module. It owns what a run's status may become and nothing else: no
locks, no journal, no I/O. :class:`~server.controller.RunController` owns the
current value and the side effects of changing it, so the rules here can be
enumerated in a unit test without composing a server.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final


class RunStatus(StrEnum):
    """Lifecycle status of one run, as frontends observe it.

    Domain view of the wire ``RunStatus`` enum (``common.proto``), which owns
    the closed set for the ``status`` field of ``RunSnapshot`` and of
    ``RunStatusChangedData``; ``server.wire.enums`` maps between the two by
    member name.

    ``PAUSING`` and ``PAUSED`` are distinct because a pause is only applied at
    an invocation boundary: ``/pause`` records the request, and the run keeps
    executing the call already in flight until it reaches that boundary.
    ``STOPPING`` and ``STOPPED`` split the same way for ``/stop``, whose
    boundary is where the run ends instead of where it parks.
    """

    STARTING = "starting"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def has_ended(self) -> bool:
        """Whether the run has settled into a status it never leaves."""
        match self:
            case RunStatus.COMPLETED | RunStatus.FAILED | RunStatus.STOPPED:
                return True
            case (
                RunStatus.STARTING
                | RunStatus.RUNNING
                | RunStatus.PAUSING
                | RunStatus.PAUSED
                | RunStatus.STOPPING
            ):
                return False


class RunTrigger(StrEnum):
    """What happened to a run, in the vocabulary the controller observes.

    Triggers are facts, not commands: ``INVOCATION_FINISHED`` fires at every
    controlled invocation boundary whether or not a pause is pending, and the
    transition table decides whether that boundary is where a pause lands.
    """

    ATTACHED = "attached"
    """Durable run storage was attached; the run may execute.

    A run can be attached more than once (a later attach re-bootstraps the
    journal), so this trigger is idempotent once the run is under way.
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


_TRANSITIONS: Final[MappingProxyType[tuple[RunStatus, RunTrigger], RunStatus]] = MappingProxyType(
    {
        (RunStatus.STARTING, RunTrigger.ATTACHED): RunStatus.RUNNING,
        (RunStatus.STARTING, RunTrigger.COMPLETED): RunStatus.COMPLETED,
        (RunStatus.STARTING, RunTrigger.FAILED): RunStatus.FAILED,
        (RunStatus.RUNNING, RunTrigger.ATTACHED): RunStatus.RUNNING,
        (RunStatus.RUNNING, RunTrigger.PAUSE_REQUESTED): RunStatus.PAUSING,
        (RunStatus.RUNNING, RunTrigger.INVOCATION_FINISHED): RunStatus.RUNNING,
        (RunStatus.RUNNING, RunTrigger.RESUMED): RunStatus.RUNNING,
        (RunStatus.RUNNING, RunTrigger.STOP_REQUESTED): RunStatus.STOPPING,
        (RunStatus.RUNNING, RunTrigger.COMPLETED): RunStatus.COMPLETED,
        (RunStatus.RUNNING, RunTrigger.FAILED): RunStatus.FAILED,
        (RunStatus.PAUSING, RunTrigger.ATTACHED): RunStatus.PAUSING,
        (RunStatus.PAUSING, RunTrigger.PAUSE_REQUESTED): RunStatus.PAUSING,
        (RunStatus.PAUSING, RunTrigger.INVOCATION_FINISHED): RunStatus.PAUSED,
        (RunStatus.PAUSING, RunTrigger.RESUMED): RunStatus.RUNNING,
        # A stop supersedes the pause pending at the same boundary.
        (RunStatus.PAUSING, RunTrigger.STOP_REQUESTED): RunStatus.STOPPING,
        (RunStatus.PAUSING, RunTrigger.COMPLETED): RunStatus.COMPLETED,
        (RunStatus.PAUSING, RunTrigger.FAILED): RunStatus.FAILED,
        (RunStatus.PAUSED, RunTrigger.ATTACHED): RunStatus.PAUSED,
        (RunStatus.PAUSED, RunTrigger.PAUSE_REQUESTED): RunStatus.PAUSED,
        (RunStatus.PAUSED, RunTrigger.INVOCATION_FINISHED): RunStatus.PAUSED,
        (RunStatus.PAUSED, RunTrigger.RESUMED): RunStatus.RUNNING,
        # Leaving PAUSED releases the thread parked at the pause wait, which
        # then lands the stop at the boundary it is already standing on.
        (RunStatus.PAUSED, RunTrigger.STOP_REQUESTED): RunStatus.STOPPING,
        (RunStatus.PAUSED, RunTrigger.COMPLETED): RunStatus.COMPLETED,
        (RunStatus.PAUSED, RunTrigger.FAILED): RunStatus.FAILED,
        (RunStatus.STOPPING, RunTrigger.ATTACHED): RunStatus.STOPPING,
        # A pause cannot downgrade a stop already pending at the boundary.
        (RunStatus.STOPPING, RunTrigger.PAUSE_REQUESTED): RunStatus.STOPPING,
        (RunStatus.STOPPING, RunTrigger.INVOCATION_FINISHED): RunStatus.STOPPED,
        # A resume that beats the boundary cancels the stop, like a pause.
        (RunStatus.STOPPING, RunTrigger.RESUMED): RunStatus.RUNNING,
        (RunStatus.STOPPING, RunTrigger.STOP_REQUESTED): RunStatus.STOPPING,
        (RunStatus.STOPPING, RunTrigger.COMPLETED): RunStatus.COMPLETED,
        (RunStatus.STOPPING, RunTrigger.FAILED): RunStatus.FAILED,
    }
)
"""Every legal move out of a status that has not ended, as data.

Absent pairs are illegal and raise. A status that has ended is absorbing and
is deliberately not listed: :func:`transition` answers for it first.
"""


def transition(current: RunStatus, trigger: RunTrigger) -> RunStatus:
    """Return the status ``trigger`` produces from ``current``.

    An ended status absorbs every trigger, which is what makes ``finish``
    idempotent and lets a late ``/resume`` or a boundary reached after the run
    stopped be a no-op rather than an error.

    Raises:
        IllegalRunTransitionError: The pair is not in the table, so the caller
            observed something the lifecycle says cannot happen.
    """
    if current.has_ended:
        return current
    result = _TRANSITIONS.get((current, trigger))
    if result is None:
        raise IllegalRunTransitionError(current, trigger)
    return result
