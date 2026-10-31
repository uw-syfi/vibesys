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

from .common import (
    ContractValidationError,
    Count,
    InterruptPolicy,
    Observation,
    RequestId,
    Value,
    unset,
)
from .records import RequestObserved
from .until import Failed


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


class Finished(Value):
    """The step ran to the end; ``observation`` is what the executor reported.

    A step whose executor reports more than the observation (a turn's reply, evidence, a
    revision) carries the whole report in ``observed``, whose observation is ``observation``.
    """

    kind: Literal["finished"] = "finished"
    observation: Observation
    observed: RequestObserved | None = Field(default=None, exclude_if=unset)

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
    It runs now when it began in the epoch the log is in and no interruption ended it; one that
    began in an earlier epoch ended with its process. ``interrupted`` counts the attempts an
    interruption record ended: they are not charged to the step's retry bound.
    """

    tag: RequestId
    started: int = Field(ge=1)
    """The latest attempt that began."""
    epoch: Count
    """The epoch it began in."""
    ended: bool = False
    """Whether an interruption record ended the latest attempt."""
    interrupted: Count = 0


class StepsState(Value):
    """The epoch the log is in and the attempts of the steps still open."""

    epoch: Count = 0
    attempts: tuple[StepAttempts, ...] = ()

    def of(self, tag: RequestId) -> StepAttempts | None:
        """The attempts of the open step ``tag``, or None before its first began."""
        return next((row for row in self.attempts if row.tag == tag), None)


__all__ = [
    "CancelReason",
    "Cancelled",
    "Failed",
    "Finished",
    "InterruptPolicy",
    "StepAttempts",
    "StepResult",
    "StepsState",
]
