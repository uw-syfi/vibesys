"""Slurm job states: the one total mapping from raw scheduler text to lifecycle.

``squeue`` and ``sacct`` name a job's state with the strings in
:class:`SlurmRawState`. :func:`classify` maps any such string to the phase the
job is in and the public status it reads as; a string outside the closed set (a
newer Slurm) is UNKNOWN.
"""

from __future__ import annotations

from enum import StrEnum
from typing import assert_never


class SlurmJobStatus(StrEnum):
    """Scheduler state visible through the public job lifecycle API."""

    UNKNOWN = "unknown"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SlurmPhase(StrEnum):
    """Where a scheduler state sits on a job's path, in order within one attempt.

    The public status folds COMPLETING into RUNNING; this finer order keeps it, so
    a reader can tell a job that is tearing down from one that is computing.
    Within one attempt the phases only advance. A requeue starts a new attempt,
    which restarts the order at PENDING.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETING = "completing"
    ENDED = "ended"
    UNKNOWN = "unknown"


class SlurmRawState(StrEnum):
    """Every job state the Slurm manuals document for ``squeue`` and ``sacct``.

    ``phase_of`` is a total function over this closed set; a state outside it
    (a newer Slurm) reads as ``SlurmPhase.UNKNOWN``.
    """

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


def parse_accounting_row(output: str) -> tuple[str, str] | None:
    """The first ``State|ExitCode`` row of ``sacct -P`` output.

    ``-P`` prints whole values: without it sacct cuts State to 10 columns and marks
    the cut with ``+`` (``OUT_OF_ME+``). A cancelled job's State carries its
    canceller (``CANCELLED by 1000``); the state is the first word.
    """
    for line in output.splitlines():
        state, _, exit_code = line.strip().partition("|")
        words = state.split()
        if words and exit_code.strip():
            return words[0], exit_code.strip()
    return None


def _parse_state(raw_state: str) -> SlurmRawState | None:
    try:
        return SlurmRawState(raw_state)
    except ValueError:
        return None


def _phase_of_state(state: SlurmRawState) -> SlurmPhase:
    match state:
        case (
            SlurmRawState.PENDING
            | SlurmRawState.CONFIGURING
            | SlurmRawState.REQUEUED
            | SlurmRawState.REQUEUE_HOLD
            | SlurmRawState.REQUEUE_FED
            | SlurmRawState.RESV_DEL_HOLD
        ):
            return SlurmPhase.PENDING
        case (
            SlurmRawState.RUNNING
            | SlurmRawState.SUSPENDED
            | SlurmRawState.STOPPED
            | SlurmRawState.SIGNALING
            | SlurmRawState.STAGE_OUT
            | SlurmRawState.RESIZING
        ):
            return SlurmPhase.RUNNING
        case SlurmRawState.COMPLETING:
            return SlurmPhase.COMPLETING
        # SPECIAL_EXIT is a requeued job held until an operator releases it; nothing
        # here does, so for this API its attempt has ended in failure.
        case (
            SlurmRawState.COMPLETED
            | SlurmRawState.FAILED
            | SlurmRawState.CANCELLED
            | SlurmRawState.TIMEOUT
            | SlurmRawState.OUT_OF_MEMORY
            | SlurmRawState.NODE_FAIL
            | SlurmRawState.PREEMPTED
            | SlurmRawState.DEADLINE
            | SlurmRawState.BOOT_FAIL
            | SlurmRawState.REVOKED
            | SlurmRawState.SPECIAL_EXIT
        ):
            return SlurmPhase.ENDED
        case _:
            assert_never(state)


def phase_of(raw_state: str) -> SlurmPhase:
    """Map one raw Slurm job state to its phase. Total: undocumented states are UNKNOWN."""
    state = _parse_state(raw_state)
    return SlurmPhase.UNKNOWN if state is None else _phase_of_state(state)


def _ended_status(state: SlurmRawState, exit_code: str | None) -> SlurmJobStatus:
    if state is SlurmRawState.COMPLETED:
        ok = exit_code is None or exit_code.startswith("0:")
        return SlurmJobStatus.COMPLETED if ok else SlurmJobStatus.FAILED
    if state in {SlurmRawState.CANCELLED, SlurmRawState.PREEMPTED, SlurmRawState.REVOKED}:
        return SlurmJobStatus.CANCELLED
    return SlurmJobStatus.FAILED


def classify(raw_state: str, exit_code: str | None) -> tuple[SlurmPhase, SlurmJobStatus]:
    """The phase and public status one raw state reads as; ``exit_code`` splits COMPLETED."""
    state = _parse_state(raw_state)
    if state is None:
        return SlurmPhase.UNKNOWN, SlurmJobStatus.UNKNOWN
    phase = _phase_of_state(state)
    match phase:
        case SlurmPhase.PENDING:
            return phase, SlurmJobStatus.PENDING
        case SlurmPhase.RUNNING | SlurmPhase.COMPLETING:
            return phase, SlurmJobStatus.RUNNING
        case SlurmPhase.ENDED:
            return phase, _ended_status(state, exit_code)
        case SlurmPhase.UNKNOWN:
            return phase, SlurmJobStatus.UNKNOWN
        case _:
            assert_never(phase)
