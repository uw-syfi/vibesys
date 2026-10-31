"""How a step ends, what an interrupted attempt of it means, and what the log says of its attempts.

A step is one request core asks the shell to perform. It ends with one result: ``Finished``
with the observation the work produced, ``Failed`` with a typed reason, or ``Cancelled`` with
the reason core asked for it. An attempt that stops without a result (a pause, a shutdown, a
crash) is not a result: the step stays open and runs again, unless its asker declared that an
interruption cancels it (``InterruptPolicy``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .attempts import AttemptsEvent
from .common import (
    ContractValidationError,
    Count,
    InterruptPolicy,
    Observation,
    RequestId,
    Value,
    unset,
)
from .evaluation import EvaluationEvent
from .records import RequestObserved
from .sessions import SessionsEvent
from .settlement import RunResultProposal, SettlementEvent
from .until import Failed

type OwnerEvent = Annotated[
    AttemptsEvent | SessionsEvent | EvaluationEvent | SettlementEvent, Field(discriminator="kind")
]
"""What an executor tells the area that owns the resource of its request, beside the observation.

The ledger derives an owner event of its own from a report (``_request_forward.owner_signals``),
but not every fact: a plain turn's reply, a mid-turn input the provider refused and a job's failure
classification reach core only as the executor's own events."""


class CancelReason(StrEnum):
    """Why a step was cancelled. Closed."""

    DEADLINE = "deadline"
    """The step's limit passed."""

    PAUSE_NOW = "pause-now"
    """The operator paused the run without a drain."""

    DRAIN_EXPIRED = "drain-expired"
    """A paused run's drain deadline passed while the step still ran."""

    RUN_ENDING = "run-ending"
    """The run is ending and closes what is open."""

    WITHDRAWN = "withdrawn"
    """The asker no longer wants the step's result."""

    INTERRUPTED = "interrupted"
    """An attempt was interrupted and the step's policy is ``InterruptPolicy.CANCEL``."""


class Interruption(StrEnum):
    """Why an attempt ended without a result. Closed; the log records the same set."""

    PAUSE = "pause"
    """The operator paused the run: not a failure and not charged to the step's retry bound."""

    SHUTDOWN = "shutdown"
    """The run process shut down in an orderly way: not charged."""

    RAISED = "raised"
    """The runner raised or gave nothing back: charged, as a crash of the attempt is."""

    @property
    def charged(self) -> bool:
        """Whether the attempt this ended counts against the step's retry bound."""
        return self is Interruption.RAISED


class Finished(Value):
    """The step ran to the end; ``observation`` is what the executor reported.

    A step whose executor reports more than the observation (a turn's reply, evidence, a
    revision) carries the whole report in ``observed``, whose observation is ``observation``.
    ``owner_events`` are the events the executor returned beside the report. Core applies the
    report first and then each of them in order, as the live shell does, and one record holds all
    of them, so a crash loses none.
    """

    kind: Literal["finished"] = "finished"
    observation: Observation
    observed: RequestObserved | None = Field(default=None, exclude_if=unset)
    owner_events: tuple[OwnerEvent, ...] = Field(default=(), exclude_if=unset)

    @model_validator(mode="after")
    def report_is_of_the_observation(self) -> Finished:
        """The report and the observation are one fact, not two."""
        if self.observed is not None and self.observed.observation != self.observation:
            raise ContractValidationError(
                "observed", "its observation differs from the finished one"
            )
        return self

    @property
    def report(self) -> RequestObserved:
        """What the executor reported, as the ledger consumes it."""
        return self.observed or RequestObserved(observation=self.observation)


class Cancelled(Value):
    """The step was cancelled and has no result of its own."""

    kind: Literal["cancelled"] = "cancelled"
    reason: CancelReason


type StepResult = Annotated[Finished | Failed | Cancelled, Field(discriminator="kind")]


class StepAttempts(Value):
    """What the log says about the attempts of one open step.

    Attempts are numbered from 1 and run one at a time, so the latest one stands for the rest.
    It runs now until a record ends it: an interruption of it, or the first record of a later
    epoch, which proves its process dead. ``uncharged`` counts the attempts a pause or shutdown
    ended; every other attempt that began, a crashed or a raised one included, counts against
    the step's retry bound.
    """

    tag: RequestId
    started: int = Field(ge=1)
    """The latest attempt that began."""
    ended: bool = False
    """Whether the latest attempt ended without a result."""
    uncharged: Count = 0
    """The attempts a pause or shutdown ended."""

    @property
    def charged(self) -> int:
        """The attempts that count against the step's retry bound."""
        return self.started - self.uncharged


class StepsState(Value):
    """The epoch the log is in, the attempts of the steps still open, and the end of the run.

    A record of a later epoch opens it (see ``vs_core._step_attempts.epoch_opened``): the one
    writer of a run wrote nothing else in the earlier epoch after its process died.
    ``ended`` is the result a ``RunEnded`` record published; it stands only while the run still
    has that result, so a resumed run is not ended.
    """

    epoch: Count = 0
    attempts: tuple[StepAttempts, ...] = ()
    ended: RunResultProposal | None = Field(default=None, exclude_if=unset)

    def of(self, tag: RequestId) -> StepAttempts | None:
        """The attempts of the open step ``tag``, or None before its first began."""
        return next((row for row in self.attempts if row.tag == tag), None)


__all__ = [
    "CancelReason",
    "Cancelled",
    "Failed",
    "Finished",
    "InterruptPolicy",
    "Interruption",
    "OwnerEvent",
    "StepAttempts",
    "StepResult",
    "StepsState",
]
