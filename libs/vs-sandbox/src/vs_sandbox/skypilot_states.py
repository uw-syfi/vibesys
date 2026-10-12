"""The one SkyPilot lifecycle translator: raw CLI status to typed lives.

SkyPilot reports three kinds of raw status: a cluster's ``status`` string, a
queued job's ``status`` string, and the exit code of ``sky logs``. This module
is the only place those are interpreted. Every other module reads a
:data:`ClusterLife`, a :data:`JobLife`, or a :data:`LogsVerdict`.

The contract:

* No guessing. A status outside the documented set, or a missing one, is
  ``JobUnknown(raw)``; it is never matched case-insensitively, never defaulted to a
  running state, and never read as the nearest known state.
* Forward only (jobs). A job life orders JobQueued < JobRunning < JobEnded. A reading
  that would move a job backwards is ignored, and JobEnded is final, so a late
  queue listing cannot un-finish a job. An unrecognised reading never replaces a
  known life; it is reported by :func:`job_reading`, which the caller must treat
  as an error before folding the reading in.
* Clusters are snapshots, not histories. A stopped cluster is relaunched under
  the same name, so a cluster life has no forward order and no held state; each
  reading stands alone.
* Typed. How a job ended is a :class:`JobEnd`, one value per documented terminal
  state; how ``sky logs`` finished is a :class:`JobStatus`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field


class SkyClusterState(StrEnum):
    """Every cluster status string this module recognises."""

    INIT = "INIT"
    UP = "UP"
    STOPPED = "STOPPED"
    DOWN = "DOWN"


class SkyJobState(StrEnum):
    """Every job queue status string this module recognises."""

    INIT = "INIT"
    PENDING = "PENDING"
    SETTING_UP = "SETTING_UP"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    FAILED_SETUP = "FAILED_SETUP"
    FAILED_DRIVER = "FAILED_DRIVER"
    CANCELLED = "CANCELLED"


class JobStatus(StrEnum):
    """How a submitted application command finished, as read from ``sky logs``."""

    COMPLETED = "COMPLETED"
    APPLICATION_FAILED = "APPLICATION_FAILED"
    CANCELLED = "CANCELLED"


class ClusterEnd(StrEnum):
    """Why a cluster no longer serves work: one value per documented state."""

    STOPPED = "STOPPED"
    DOWN = "DOWN"


class JobEnd(StrEnum):
    """How a queued job ended: one value per documented terminal state, none merged."""

    OK = "ok"
    FAILED = "failed"
    SETUP_FAILED = "setup_failed"
    DRIVER_FAILED = "driver_failed"
    CANCELLED = "cancelled"


class _Life(BaseModel):
    """Base of the lives; strict and frozen so a compared or persisted life is exact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ClusterInitializing(_Life):
    """The cluster exists and is still coming up."""

    kind: Literal["initializing"] = "initializing"


class ClusterUp(_Life):
    """The cluster accepts work."""

    kind: Literal["up"] = "up"


class ClusterEnded(_Life):
    """The cluster no longer serves work. ``outcome`` says why."""

    kind: Literal["ended"] = "ended"
    outcome: ClusterEnd


class ClusterUnknown(_Life):
    """The status is not one this module recognises. ``raw`` is the text, None if absent."""

    kind: Literal["unknown"] = "unknown"
    raw: str | None = None


ClusterLife = Annotated[
    ClusterInitializing | ClusterUp | ClusterEnded | ClusterUnknown, Field(discriminator="kind")
]


class JobQueued(_Life):
    """Accepted and waiting, or being initialised."""

    kind: Literal["queued"] = "queued"


class JobRunning(_Life):
    """Setting up or computing."""

    kind: Literal["running"] = "running"


class JobEnded(_Life):
    """Final. ``outcome`` says how."""

    kind: Literal["ended"] = "ended"
    outcome: JobEnd


class JobUnknown(_Life):
    """Nothing usable is known. ``raw`` is the unrecognised text, None before any reading."""

    kind: Literal["unknown"] = "unknown"
    raw: str | None = None


JobLife = Annotated[JobQueued | JobRunning | JobEnded | JobUnknown, Field(discriminator="kind")]

INITIAL_JOB_LIFE: JobLife = JobUnknown()


@dataclass(frozen=True)
class ClusterInfo:
    """Identity and observed life of one cluster."""

    name: str
    life: ClusterLife


@dataclass(frozen=True)
class StatusText:
    """One raw ``status`` field: the text SkyPilot printed, None when the field is absent."""

    text: str | None = None


def status_text(value: object) -> StatusText:
    """The raw status field of a JSON record, whatever type it arrived as."""
    return StatusText(None if value is None else str(value))


_ENDED_CLUSTER = {
    SkyClusterState.STOPPED: ClusterEnd.STOPPED,
    SkyClusterState.DOWN: ClusterEnd.DOWN,
}


def cluster_life(raw: StatusText) -> ClusterLife:
    """What one raw cluster status says. Total; a snapshot, not a history."""
    state = _parse(SkyClusterState, raw.text)
    match state:
        case None:
            return ClusterUnknown(raw=raw.text)
        case SkyClusterState.INIT:
            return ClusterInitializing()
        case SkyClusterState.UP:
            return ClusterUp()
        case SkyClusterState.STOPPED | SkyClusterState.DOWN:
            return ClusterEnded(outcome=_ENDED_CLUSTER[state])
        case _:
            assert_never(state)


def allocation_gone(cluster: ClusterInfo | None) -> bool:
    """Whether the allocation is provably not serving: absent, stopped, or down.

    An initializing, up, or unrecognised cluster is not provably gone.
    """
    return cluster is None or isinstance(cluster.life, ClusterEnded)


def describe_cluster(life: ClusterLife) -> str:
    """The status of a cluster life as text, for an error message."""
    match life:
        case ClusterInitializing():
            return SkyClusterState.INIT.value
        case ClusterUp():
            return SkyClusterState.UP.value
        case ClusterEnded(outcome=outcome):
            return outcome.value
        case ClusterUnknown(raw=raw):
            return f"an unrecognised status {raw!r}"
        case _:
            assert_never(life)


_QUEUED_JOB_STATES = frozenset({SkyJobState.INIT, SkyJobState.PENDING})
_RUNNING_JOB_STATES = frozenset({SkyJobState.SETTING_UP, SkyJobState.RUNNING})
_END_OF_JOB_STATE = {
    SkyJobState.SUCCEEDED: JobEnd.OK,
    SkyJobState.FAILED: JobEnd.FAILED,
    SkyJobState.FAILED_SETUP: JobEnd.SETUP_FAILED,
    SkyJobState.FAILED_DRIVER: JobEnd.DRIVER_FAILED,
    SkyJobState.CANCELLED: JobEnd.CANCELLED,
}


def job_reading(raw: StatusText) -> JobLife:
    """What one raw job status says on its own. Total; ``JobUnknown(raw)`` outside the set."""
    state = _parse(SkyJobState, raw.text)
    if state is None:
        return JobUnknown(raw=raw.text)
    if state in _QUEUED_JOB_STATES:
        return JobQueued()
    if state in _RUNNING_JOB_STATES:
        return JobRunning()
    return JobEnded(outcome=_END_OF_JOB_STATE[state])


_RANK = {"unknown": -1, "queued": 0, "running": 1, "ended": 2}


def advance(prev: JobLife, seen: JobLife) -> JobLife:
    """Join a reading's life into the held life, forward only.

    An unknown reading carries no order: it only replaces another unknown. A
    reading below the held life, or any reading after JobEnded, is ignored.
    """
    if isinstance(seen, JobUnknown):
        return seen if isinstance(prev, JobUnknown) else prev
    if isinstance(prev, JobUnknown):
        return seen
    if isinstance(prev, JobEnded):
        return prev
    return seen if _RANK[seen.kind] >= _RANK[prev.kind] else prev


def observe(prev: JobLife, raw: StatusText) -> JobLife:
    """Fold one raw job status into *prev*. Total, pure, and forward only."""
    return advance(prev, job_reading(raw))


def is_driver_failure(life: JobLife) -> bool:
    """Whether the job ended because SkyPilot's own driver died, so it may be resubmitted."""
    return isinstance(life, JobEnded) and life.outcome is JobEnd.DRIVER_FAILED


@dataclass(frozen=True)
class RemoteJobInfo:
    """Recoverable identity and held life for one remote job."""

    job_id: int
    job_name: str
    life: JobLife


# Exit codes of ``sky logs`` for a finished job.
_SKY_JOB_FAILED = 100
_SKY_JOB_NOT_FINISHED = 101
_SKY_JOB_NOT_FOUND = 102
_SKY_JOB_CANCELLED = 103


@dataclass(frozen=True)
class Finished:
    """``sky logs`` followed the job to its end; ``status`` says how it finished."""

    status: JobStatus


@dataclass(frozen=True)
class Indeterminate:
    """``sky logs`` says the job has not finished or does not exist: no outcome is known."""

    returncode: int


@dataclass(frozen=True)
class LogsFailed:
    """``sky logs`` itself failed; it says nothing about the job."""

    returncode: int


LogsVerdict = Finished | Indeterminate | LogsFailed


def logs_verdict(returncode: int) -> LogsVerdict:
    """What the exit code of ``sky logs`` says about the job. Total; no code is guessed."""
    if returncode == 0:
        return Finished(JobStatus.COMPLETED)
    if returncode == _SKY_JOB_FAILED:
        return Finished(JobStatus.APPLICATION_FAILED)
    if returncode == _SKY_JOB_CANCELLED:
        return Finished(JobStatus.CANCELLED)
    if returncode in {_SKY_JOB_NOT_FINISHED, _SKY_JOB_NOT_FOUND}:
        return Indeterminate(returncode)
    return LogsFailed(returncode)


def _parse[StateT: StrEnum](states: type[StateT], text: str | None) -> StateT | None:
    """The documented state spelled exactly as *text*, or None. No case folding."""
    if text is None:
        return None
    try:
        return states(text)
    except ValueError:
        return None
