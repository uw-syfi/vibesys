"""What a search says about the agents it asks, and what the agent pipeline hands back.

A search asks an agent through ``ctx.agents`` and gets one ``TurnFinished`` or one ``Failed``.
How the reply is rendered, validated, corrected, asked again and resumed is core's mechanism,
set per agent by its ``Role``. These are the values both sides share.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from vs_core._generated.vibesys.core.v1.leaves_cv import ChargeClass
from vs_core._generated.vibesys.core.v1.vocabulary_cv import Charge

from .common import (
    CheckpointDecline,
    ContractValidationError,
    InvocationRef,
    RevisionRef,
    Seconds,
    Value,
)
from .evaluation import EvidenceRef, settling_named
from .evaluation_history import RepeatedFailureGuidance
from .job_observations import WaitEnd
from .measurement import Comparison
from .until import ReplyError


class Backoff(Value):
    """Run-clock wait before a lost turn is asked again; each further ask doubles it."""

    first: Seconds
    cap: Seconds

    def delay(self, drops: int) -> float:
        """Seconds to wait before re-asking a turn already lost ``drops`` times."""
        return min(self.cap, self.first * 2**drops)


"""The charge class of a turn request: a role's own (free or paid), a correction or a resume."""


class Correctable(StrEnum):
    """The failures of a reply that core may ask the agent to correct, within ``corrections``."""

    INVALID_REPLY = "invalid-reply"
    """The reply breaks the role's model."""
    UNRECORDED_WAIT = "unrecorded-wait"
    """The reply says it waits for evaluations but the run recorded no wait."""


class Exchanges(Value):
    """The templates of the exchanges core itself starts with a role's agent.

    Each names a template file the renderer reads; the wording is the template's. A role needs
    exactly the exchanges it can meet (see ``Role``), and a missing one is a declaration error.
    """

    correction: str | None = None
    """Asks the agent to fix an invalid reply (renders a ``ReplyCorrection``)."""
    resume: str | None = None
    """Continues a turn that waited for evaluations (renders a ``ResumeFacts``)."""
    wait_unrecorded: str | None = None
    """Tells the agent its wait was not recorded (renders a ``WaitUnrecorded``)."""


class Correction(Value):
    """Base of an input that corrects an earlier turn of the same agent.

    A search that handles invalid replies itself asks again with an input of a subclass
    carrying why the earlier reply failed. The pipeline sends the new turn as a correction
    of ``of``, as core requires.
    """

    of: InvocationRef


class ResumeFacts(Value):
    """What the resume prompt of a suspended turn is built from."""

    kind: Literal["resume"] = "resume"
    agent: str
    retained: RevisionRef | None
    ended_by: WaitEnd | None
    """The bound that ended the wait before every evaluation was terminal; None when every
    one was. ``QUEUE_BUDGET`` means every evaluation was terminal or cancelled for its queue
    budget."""
    queue_budget_spent: bool = False
    """Whether core cancelled an awaited evaluation that waited in its queue past its queue
    budget: it never ran."""
    evidence: tuple[EvidenceRef, ...] = ()
    repeated_failure: RepeatedFailureGuidance | None = None
    comparisons: tuple[Comparison, ...] = ()
    """What the attempt's candidates measured against their base: the ratio, the noise and the
    verdict the framework reached, so the agent need not work them out from a lone number.
    Those the awaited evaluations measured, and any other whose verdict changed since the
    attempt's last resume."""
    settling: tuple[str, ...] = ()
    """The candidate inputs of ``comparisons`` the framework still has runs under way for: their
    verdicts are provisional."""

    @model_validator(mode="after")
    def budget_end_names_a_spent_budget(self) -> ResumeFacts:
        """A wait ended by a queue budget spent one; a wait that ended terminal spent none."""
        if (self.ended_by is WaitEnd.QUEUE_BUDGET and not self.queue_budget_spent) or (
            self.ended_by is None and self.queue_budget_spent
        ):
            raise ContractValidationError(
                "queue_budget_spent", "must agree with what ended the wait"
            )
        settling_named(self.comparisons, self.settling)
        return self


class ReplyCorrection(Value):
    """What the correction prompt of an invalid reply is built from."""

    kind: Literal["reply-correction"] = "reply-correction"
    role: str
    errors: tuple[ReplyError, ...]


class WaitUnrecorded(Value):
    """What the prompt is built from when a reply waited and the run recorded no wait."""

    kind: Literal["wait-unrecorded"] = "wait-unrecorded"
    role: str


type ProtocolContext = ReplyCorrection | ResumeFacts | WaitUnrecorded
"""The contexts of the exchanges core starts: what their templates render."""


class Retained(Value):
    """Core retained the exact revision this turn produced."""

    kind: Literal["retained"] = "retained"
    revision: RevisionRef


class Declined(Value):
    """Core declined or failed this turn's checkpoint."""

    kind: Literal["declined"] = "declined"
    reason: CheckpointDecline


type Checkpoint = Annotated[Retained | Declined, Field(discriminator="kind")]


__all__ = [
    "Backoff",
    "Charge",
    "ChargeClass",
    "Checkpoint",
    "Correctable",
    "Correction",
    "Declined",
    "Exchanges",
    "ProtocolContext",
    "ReplyCorrection",
    "ResumeFacts",
    "Retained",
    "WaitUnrecorded",
]
