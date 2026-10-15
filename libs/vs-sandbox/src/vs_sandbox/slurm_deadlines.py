"""The Slurm executor's two deadlines: one for waiting in the queue, one for running.

Queue wait is the scheduler's business and can last hours on a busy cluster;
it is reported as data and bounded only when the operator sets
``queue_timeout_seconds``. Run time is bounded by ``job_timeout_seconds`` from
the moment the job is first seen running, so a job that starts late still gets
its whole run budget. A requeued job starts a new attempt: it queues and runs
against fresh deadlines, as Slurm's own ``--time`` does.

This module is pure: the executor feeds it each scheduler reading and persists
what it returns before acting on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeadlinePhase(StrEnum):
    """Which deadline applies to the job now."""

    QUEUE = "queue"
    RUN = "run"


@dataclass(frozen=True, slots=True)
class DeadlineLimits:
    """The operator's bounds, in seconds: queue wait (None: unbounded) and run time."""

    queue_seconds: float | None
    run_seconds: float


@dataclass(frozen=True, slots=True)
class WaitDeadlines:
    """The deadlines of the job's current scheduler attempt, as epoch seconds.

    ``queue_epoch_s`` is None when queue wait is unbounded; ``run_epoch_s`` is
    None until the attempt is first seen running.
    """

    attempt: int
    queue_epoch_s: float | None
    run_epoch_s: float | None

    @property
    def phase(self) -> DeadlinePhase:
        """The run deadline applies once the attempt has started."""
        return DeadlinePhase.QUEUE if self.run_epoch_s is None else DeadlinePhase.RUN

    @property
    def active_epoch_s(self) -> float | None:
        """The deadline that applies now, or None when nothing bounds the wait."""
        return self.queue_epoch_s if self.run_epoch_s is None else self.run_epoch_s


def first_deadlines(*, now: float, queued_seconds: float, limits: DeadlineLimits) -> WaitDeadlines:
    """The deadlines of a job just submitted, which has queued ``queued_seconds`` already."""
    queue = None if limits.queue_seconds is None else now + limits.queue_seconds - queued_seconds
    return WaitDeadlines(attempt=0, queue_epoch_s=queue, run_epoch_s=None)


def observe(
    deadlines: WaitDeadlines, *, attempt: int, running: bool, now: float, limits: DeadlineLimits
) -> WaitDeadlines:
    """Fold one scheduler reading into the deadlines.

    A newer attempt (a requeue) queues afresh. The first reading of an attempt
    running anchors its run deadline at ``now``; later readings never move it.
    """
    if attempt > deadlines.attempt:
        queue = None if limits.queue_seconds is None else now + limits.queue_seconds
        deadlines = WaitDeadlines(attempt=attempt, queue_epoch_s=queue, run_epoch_s=None)
    if running and deadlines.run_epoch_s is None:
        deadlines = WaitDeadlines(
            attempt=deadlines.attempt,
            queue_epoch_s=deadlines.queue_epoch_s,
            run_epoch_s=now + limits.run_seconds,
        )
    return deadlines


__all__ = ["DeadlineLimits", "DeadlinePhase", "WaitDeadlines", "first_deadlines", "observe"]
