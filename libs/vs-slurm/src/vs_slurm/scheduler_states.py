"""The one Slurm lifecycle translator: raw scheduler status to a typed job life.

``squeue`` and ``sacct`` name a job's state with the strings in
:class:`SlurmRawState`. This module is the only place those strings, the
``sacct`` exit-code text, and the "not listed in squeue" absence are
interpreted; every other module reads a :data:`JobLife` and its projections
(:class:`SlurmJobStatus`, :class:`SlurmPhase`).

The contract of :func:`observe`:

* Forward only. A life orders by ``(attempt, rank)`` with Queued < Running <
  Completing < Ended. A reading that would move a job backwards, or change how
  it ended, is ignored; a higher attempt (a scheduler requeue) restarts the
  order, except after Ended, which is final.
* No guessing. A state outside the documented set is ``Unknown(raw)``, never
  the nearest known state. A job missing from ``squeue`` is not an outcome: it
  says nothing, and the caller asks ``sacct`` (:func:`needs_accounting`).
* Typed. How a job ended is an :class:`EndOutcome`, one per documented terminal
  state; collapsing outcomes into a public status is a projection of the life.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field


class SlurmJobStatus(StrEnum):
    """Scheduler state visible through the public job lifecycle API.

    A projection of :data:`JobLife` (:func:`status_of`); never read from raw text.
    """

    UNKNOWN = "unknown"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """Whether the scheduler has finished with the job."""
        return self in {SlurmJobStatus.COMPLETED, SlurmJobStatus.FAILED, SlurmJobStatus.CANCELLED}

    @property
    def active(self) -> bool:
        """Whether the job is queued or computing, so it can still be cancelled."""
        return self in {SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING}


class SlurmPhase(StrEnum):
    """Where a job's life sits on its path, in order within one attempt.

    The public status folds COMPLETING into RUNNING; this finer order keeps it, so
    a reader can tell a job that is tearing down from one that is computing.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETING = "completing"
    ENDED = "ended"
    UNKNOWN = "unknown"


class SlurmRawState(StrEnum):
    """Every job state the Slurm manuals document for ``squeue`` and ``sacct``."""

    BOOT_FAIL = "BOOT_FAIL"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    COMPLETING = "COMPLETING"
    CONFIGURING = "CONFIGURING"
    DEADLINE = "DEADLINE"
    FAILED = "FAILED"
    NODE_FAIL = "NODE_FAIL"
    OUT_OF_MEMORY = "OUT_OF_MEMORY"
    PENDING = "PENDING"
    PREEMPTED = "PREEMPTED"
    REQUEUE_FED = "REQUEUE_FED"
    REQUEUE_HOLD = "REQUEUE_HOLD"
    REQUEUED = "REQUEUED"
    RESIZING = "RESIZING"
    RESV_DEL_HOLD = "RESV_DEL_HOLD"
    REVOKED = "REVOKED"
    RUNNING = "RUNNING"
    SIGNALING = "SIGNALING"
    SPECIAL_EXIT = "SPECIAL_EXIT"
    STAGE_OUT = "STAGE_OUT"
    STOPPED = "STOPPED"
    SUSPENDED = "SUSPENDED"
    TIMEOUT = "TIMEOUT"


class EndOutcome(StrEnum):
    """How a job ended: one value per documented terminal state, none merged."""

    OK = "ok"
    NONZERO_EXIT = "nonzero_exit"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    DEADLINE_MISSED = "deadline_missed"
    OUT_OF_MEMORY = "out_of_memory"
    NODE_LOST = "node_lost"
    BOOT_FAILED = "boot_failed"
    PREEMPTED = "preempted"
    REVOKED = "revoked"
    SPECIAL_EXIT = "special_exit"


class _Life(BaseModel):
    """Base of the lives; strict and frozen so a persisted or compared life is exact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Queued(_Life):
    """Accepted and waiting for resources."""

    kind: Literal["queued"] = "queued"
    attempt: int = 0


class Running(_Life):
    """Computing (a suspended or staging-out job still holds its allocation)."""

    kind: Literal["running"] = "running"
    attempt: int = 0


class Completing(_Life):
    """The job script has exited; the node is tearing down."""

    kind: Literal["completing"] = "completing"
    attempt: int = 0


class Ended(_Life):
    """Final. ``outcome`` says how."""

    kind: Literal["ended"] = "ended"
    outcome: EndOutcome
    attempt: int = 0


class Unknown(_Life):
    """Nothing usable is known. ``raw`` is the unrecognised text, None before any reading."""

    kind: Literal["unknown"] = "unknown"
    raw: str | None = None

    @property
    def attempt(self) -> int:
        """Unknown belongs to no attempt."""
        return 0


JobLife = Annotated[Queued | Running | Completing | Ended | Unknown, Field(discriminator="kind")]

INITIAL_LIFE: JobLife = Unknown()


@dataclass(frozen=True)
class QueueRow:
    """The ``squeue -h -o '%T|%r|%S'`` line of a job: state, reason, estimated start."""

    state: str
    reason: str | None = None
    estimated_start: str | None = None
    attempt: int = 0


@dataclass(frozen=True)
class AccountingRow:
    """The ``sacct -P --format=State,ExitCode`` row of a job."""

    state: str
    exit_code: str
    attempt: int = 0


@dataclass(frozen=True)
class NotInQueue:
    """``squeue`` does not list the job. It says nothing about the outcome: ask ``sacct``."""


@dataclass(frozen=True)
class NoAccountingRow:
    """``sacct`` returned no usable row (accounting lags or is disabled)."""


SlurmStatus = QueueRow | AccountingRow | NotInQueue | NoAccountingRow


class Notice(StrEnum):
    """Why a reading was not applied."""

    REGRESSION = "regression"
    STALE_ATTEMPT = "stale_attempt"
    AFTER_END = "after_end"


@dataclass(frozen=True)
class Step:
    """The life after one reading and what the reading did to the order."""

    life: JobLife
    notice: Notice | None = None
    requeued: bool = False


def parse_accounting_row(output: str) -> AccountingRow | NoAccountingRow:
    """The first ``State|ExitCode`` row of ``sacct -P`` output.

    ``-P`` prints whole values: without it sacct cuts State to 10 columns and marks
    the cut with ``+`` (``OUT_OF_ME+``). A cancelled job's State carries its
    canceller (``CANCELLED by 1000``); the state is the first word.
    """
    for line in output.splitlines():
        state, _, exit_code = line.strip().partition("|")
        words = state.split()
        if words and exit_code.strip():
            return AccountingRow(words[0], exit_code.strip())
    return NoAccountingRow()


_EMPTY_FIELDS = frozenset({"", "N/A", "Unknown"})
_REASON_FIELD = 1
_START_FIELD = 2


def parse_queue_row(output: str) -> QueueRow | NotInQueue:
    """The first ``State|Reason|Start`` line of ``squeue -h -o '%T|%r|%S'`` output."""
    lines = output.strip().splitlines()
    if not lines or not lines[0].strip():
        return NotInQueue()
    fields = [field.strip() for field in lines[0].split("|")]
    reason = fields[_REASON_FIELD] if len(fields) > _REASON_FIELD else ""
    start = fields[_START_FIELD] if len(fields) > _START_FIELD else ""
    return QueueRow(
        fields[0],
        reason=reason or None,
        estimated_start=None if start in _EMPTY_FIELDS else start,
    )


def _parse_state(raw_state: str) -> SlurmRawState | None:
    try:
        return SlurmRawState(raw_state)
    except ValueError:
        return None


def _exit_is_clean(exit_code: str | None) -> bool | None:
    """Whether ``ExitCode`` text (``code:signal``) is a clean exit; None when unparseable.

    No exit code at all (a queue row) is clean: ``squeue`` shows COMPLETED only for
    a zero exit, and any other ending has its own state.
    """
    if exit_code is None:
        return True
    match = re.fullmatch(r"([0-9]+):([0-9]+)", exit_code)
    if match is None:
        return None
    return match.group(1) == "0" and match.group(2) == "0"


_QUEUED_STATES = frozenset(
    {
        SlurmRawState.PENDING,
        SlurmRawState.CONFIGURING,
        SlurmRawState.REQUEUED,
        SlurmRawState.REQUEUE_HOLD,
        SlurmRawState.REQUEUE_FED,
        SlurmRawState.RESV_DEL_HOLD,
    }
)
_RUNNING_STATES = frozenset(
    {
        SlurmRawState.RUNNING,
        SlurmRawState.SUSPENDED,
        SlurmRawState.STOPPED,
        SlurmRawState.SIGNALING,
        SlurmRawState.STAGE_OUT,
        SlurmRawState.RESIZING,
    }
)
# SPECIAL_EXIT is a requeued job held until an operator releases it; nothing here
# does, so its attempt has ended. COMPLETED is not here: its exit code decides.
_END_OF_STATE = {
    SlurmRawState.FAILED: EndOutcome.FAILED,
    SlurmRawState.CANCELLED: EndOutcome.CANCELLED,
    SlurmRawState.TIMEOUT: EndOutcome.TIMED_OUT,
    SlurmRawState.DEADLINE: EndOutcome.DEADLINE_MISSED,
    SlurmRawState.OUT_OF_MEMORY: EndOutcome.OUT_OF_MEMORY,
    SlurmRawState.NODE_FAIL: EndOutcome.NODE_LOST,
    SlurmRawState.BOOT_FAIL: EndOutcome.BOOT_FAILED,
    SlurmRawState.PREEMPTED: EndOutcome.PREEMPTED,
    SlurmRawState.REVOKED: EndOutcome.REVOKED,
    SlurmRawState.SPECIAL_EXIT: EndOutcome.SPECIAL_EXIT,
}


def _life_of_state(state: SlurmRawState, exit_code: str | None, attempt: int, raw: str) -> JobLife:
    if state in _QUEUED_STATES:
        return Queued(attempt=attempt)
    if state in _RUNNING_STATES:
        return Running(attempt=attempt)
    if state is SlurmRawState.COMPLETING:
        return Completing(attempt=attempt)
    if state is SlurmRawState.COMPLETED:
        clean = _exit_is_clean(exit_code)
        if clean is None:
            return Unknown(raw=f"{raw} {exit_code}")
        return Ended(outcome=EndOutcome.OK if clean else EndOutcome.NONZERO_EXIT, attempt=attempt)
    return Ended(outcome=_END_OF_STATE[state], attempt=attempt)


def _reading_life(raw: SlurmStatus) -> JobLife | None:
    """What one raw reading says on its own; None when it says nothing."""
    match raw:
        case QueueRow(state=text, attempt=attempt):
            exit_code = None
        case AccountingRow(state=text, exit_code=exit_code, attempt=attempt):
            pass
        case NotInQueue() | NoAccountingRow():
            return None
        case _:
            assert_never(raw)
    state = _parse_state(text)
    if state is None:
        return Unknown(raw=text)
    return _life_of_state(state, exit_code, attempt, text)


_RANK = {"unknown": -1, "queued": 0, "running": 1, "completing": 2, "ended": 3}


def _rank(life: JobLife) -> int:
    return _RANK[life.kind]


def advance_with_notice(prev: JobLife, seen: JobLife) -> Step:
    """Join a reading's life into the held life, forward only, and say what was ignored."""
    if isinstance(seen, Unknown):
        # Unrecognised text carries no order; it only replaces another unknown.
        return Step(seen if isinstance(prev, Unknown) else prev)
    if isinstance(prev, Unknown):
        return Step(seen)
    if isinstance(prev, Ended):
        return Step(prev, None if seen == prev else Notice.AFTER_END)
    return _join_live(prev, seen)


def _join_live(prev: JobLife, seen: JobLife) -> Step:
    """Join two known lives where *prev* has not ended: attempts first, then phases."""
    if seen.attempt != prev.attempt:
        if seen.attempt < prev.attempt:
            return Step(prev, Notice.STALE_ATTEMPT)
        return Step(seen, requeued=True)
    if _rank(seen) < _rank(prev):
        return Step(prev, Notice.REGRESSION)
    return Step(seen)


def advance(prev: JobLife, seen: JobLife) -> JobLife:
    """The life after joining *seen* into *prev*; see :func:`advance_with_notice`."""
    return advance_with_notice(prev, seen).life


def observe_with_notice(prev: JobLife, raw: SlurmStatus) -> Step:
    """Fold one raw scheduler reading into *prev*, reporting what was ignored."""
    seen = _reading_life(raw)
    return Step(prev) if seen is None else advance_with_notice(prev, seen)


def observe(prev: JobLife, raw: SlurmStatus) -> JobLife:
    """Fold one raw scheduler reading into *prev*. Total, pure, and forward only."""
    return observe_with_notice(prev, raw).life


def needs_accounting(life: JobLife) -> bool:
    """Whether ``squeue`` alone left the job's state open, so ``sacct`` must be asked."""
    return isinstance(life, (Completing, Unknown))


def phase_of(life: JobLife) -> SlurmPhase:
    """The phase a life sits in."""
    match life:
        case Queued():
            return SlurmPhase.PENDING
        case Running():
            return SlurmPhase.RUNNING
        case Completing():
            return SlurmPhase.COMPLETING
        case Ended():
            return SlurmPhase.ENDED
        case Unknown():
            return SlurmPhase.UNKNOWN
        case _:
            assert_never(life)


_STATUS_OF_OUTCOME = {
    EndOutcome.OK: SlurmJobStatus.COMPLETED,
    EndOutcome.CANCELLED: SlurmJobStatus.CANCELLED,
}


def status_of(life: JobLife) -> SlurmJobStatus:
    """The public status a life reads as. Only a clean exit completes; only a cancel cancels.

    Every other way of ending (a nonzero exit, a timeout, a lost node, a
    preemption) is a failure: the scheduler, not the caller, ended the job.
    """
    match life:
        case Queued():
            return SlurmJobStatus.PENDING
        case Running() | Completing():
            return SlurmJobStatus.RUNNING
        case Ended(outcome=outcome):
            return _STATUS_OF_OUTCOME.get(outcome, SlurmJobStatus.FAILED)
        case Unknown():
            return SlurmJobStatus.UNKNOWN
        case _:
            assert_never(life)


def life_for_status(status: SlurmJobStatus, attempt: int = 0) -> JobLife:
    """The canonical life of a public status, for a Fake scripted in public statuses."""
    match status:
        case SlurmJobStatus.PENDING:
            return Queued(attempt=attempt)
        case SlurmJobStatus.RUNNING:
            return Running(attempt=attempt)
        case SlurmJobStatus.COMPLETED:
            return Ended(outcome=EndOutcome.OK, attempt=attempt)
        case SlurmJobStatus.FAILED:
            return Ended(outcome=EndOutcome.FAILED, attempt=attempt)
        case SlurmJobStatus.CANCELLED:
            return Ended(outcome=EndOutcome.CANCELLED, attempt=attempt)
        case SlurmJobStatus.UNKNOWN:
            return Unknown()
        case _:
            assert_never(status)
