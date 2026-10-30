"""How a step ends, and what an interrupted attempt of it means.

A step is one request core asks the shell to perform. It ends with one result: ``Finished``
with the observation the work produced, ``Failed`` with a typed reason, or ``Cancelled`` with
the reason core asked for it. An attempt that stops without a result (a pause, a shutdown) is
not a result: the step stays open and runs again.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from .common import Observation, Value
from .until import Failed


class InterruptPolicy(StrEnum):
    """What core does with a step whose attempt was interrupted without a result."""

    RERUN = "rerun"
    """Run it again as the next attempt."""

    CANCEL = "cancel"
    """Settle it as ``Cancelled(INTERRUPTED)``: a person watched the attempt and decides."""


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
    """The step ran to the end; ``observation`` is what the executor reported."""

    kind: Literal["finished"] = "finished"
    observation: Observation


class Cancelled(Value):
    """The step was cancelled and has no result of its own."""

    kind: Literal["cancelled"] = "cancelled"
    reason: CancelReason


type StepResult = Annotated[Finished | Failed | Cancelled, Field(discriminator="kind")]
