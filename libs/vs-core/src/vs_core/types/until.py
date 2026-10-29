"""How long a request may take, what else ends it, and the typed failure that reports it.

A request carries no deadline. The one who asks for work states, next to the request,
``Within(limit)`` (a duration on the run clock) extended with the conditions that make the
work pointless before the limit: ``Within(60) | WhileOpen(workspace) | WhileAlive(job)``.
Core stores the accepted form (``Until``, an absolute time) on the request record. When the
limit passes or a watch fires, the record ends and its owner receives ``Failed(reason)``
with ``TIMED_OUT``, ``WORKSPACE_CLOSED`` or ``OWNER_GONE``.

A watch is not a bound: ``WhileOpen(ws)`` alone is not a valid ``until``, only ``Within``
is, so every wait has a limit.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from vs_core._generated.vibesys.core.v1.failures_cv import (
    REPLY_ERROR_MESSAGE_LIMIT,
    ReplyError,
)
from vs_core._generated.vibesys.core.v1.vocabulary_cv import ReplyErrorCode

from .common import (
    AttemptRef,
    DecisionId,
    InvocationRef,
    RejectionCode,
    ResourceId,
    Seconds,
    TurnFailureKind,
    Value,
)


class FailureReason(StrEnum):
    """Why a requested piece of work ended without a result. Closed: no free-text reasons."""

    TIMED_OUT = "timed-out"
    """The limit of its ``Within`` passed (``Failed.limit_at`` is when)."""

    WORKSPACE_CLOSED = "workspace-closed"
    """A ``WhileOpen`` watch fired: the workspace it names is no longer open."""

    OWNER_GONE = "owner-gone"
    """A ``WhileAlive`` watch fired: the job it names is no longer alive."""

    REFUSED = "refused"
    """Core rejected the decision; ``Failed.cause`` says why."""

    LOST = "lost"
    """Reconciliation could not prove what happened to the request."""

    INVALID_REPLY = "invalid-reply"
    """The agent's reply broke its model and no correction is left; ``errors`` say how."""

    RENDER_FAILED = "render-failed"
    """The prompt of an agent's turn could not be rendered."""

    TURN_FAILED = "turn-failed"
    """A turn ended without a reply; ``turn_failure`` says how and ``digest`` fingerprints
    the diagnostic the executor printed."""

    WAIT_REFUSED = "wait-refused"
    """The agent's turn asked to wait for evaluations and the run could not honor it;
    ``refusal`` says why."""

    QUEUE_BUDGET_SPENT = "queue-budget-spent"
    """Its job waited in the executor's queue for the run's whole ``queue_wait_budget``
    without starting (``Failed.limit_at`` is when the budget ran out)."""

    OUT_OF_BUDGET = "out-of-budget"
    MEASUREMENT_UNTRUSTED = "measurement-untrusted"


class WaitRefusal(StrEnum):
    """Why a turn that asked to wait for its evaluations was not suspended. Closed."""

    NOT_OFFERED = "not-offered"
    """The run offers no suspend capability."""

    NOT_RECORDED = "not-recorded"
    """Core recorded no wait (it refused it, or the host lost it), so no resume will come."""

    NOT_PUBLISHED = "not-published"
    """Core recorded the wait but could not publish it."""

    NOT_RESUMABLE = "not-resumable"
    """Core recorded the wait, but its evaluations ended in a history that can never back a
    resume (``ResumeRefused``), so no resume will come."""


class WhileOpen(Value):
    """End the wait with ``WORKSPACE_CLOSED`` when this workspace stops being open."""

    kind: Literal["while_open"] = "while_open"
    workspace: AttemptRef


class WhileAlive(Value):
    """End the wait with ``OWNER_GONE`` when this job is no longer alive."""

    kind: Literal["while_alive"] = "while_alive"
    job: ResourceId


type Watch = Annotated[WhileOpen | WhileAlive, Field(discriminator="kind")]

type Limit = Annotated[float, Field(allow_inf_nan=False)]


class Within(Value):
    """A duration on the run clock, plus the watches that may end the wait earlier.

    ``limit`` is relative to the time core accepts the wait (an agent turn's requests count it
    from their dispatch instead: see ``FromDispatch``). A limit of zero or less is
    refused by core (``Failed(REFUSED)``); the type does not forbid it so that a bypassed
    value still reaches the runtime check instead of failing on construction.
    """

    limit: Limit
    watches: tuple[Watch, ...] = ()

    def __or__(self, watch: WhileOpen | WhileAlive) -> Within:
        """Extend with one more watch: ``Within(60) | WhileOpen(workspace)``."""
        return self.model_copy(update={"watches": (*self.watches, watch)})

    def __ror__(self, watch: WhileOpen | WhileAlive) -> Within:
        """Allow ``WhileOpen(workspace) | Within(60)`` as well."""
        return self.model_copy(update={"watches": (watch, *self.watches)})


class FromDispatch(Value):
    """Internal: ``limit`` seconds counted from the instant the shell dispatches the request.

    A request waits for the shell's free slot before it runs, and the time it waits is not
    the work's. Core therefore keeps the request open until the run deadline while it is
    queued, and stamps ``Until(at=dispatch time + limit)`` when ``DispatchAuthorized``
    arrives with the time the shell dispatched it (that time is journaled with the event, so
    replay stamps the same instant).
    """

    kind: Literal["from_dispatch"] = "from_dispatch"
    limit: Limit
    watches: tuple[Watch, ...] = ()


class Inherited(Value):
    """Internal: this request ends when the decision that caused it ends (same ``Until``)."""

    kind: Literal["inherited"] = "inherited"


class Until(Value):
    """What core stored when it accepted a ``Within``: an absolute time and the watches."""

    at: Seconds
    watches: tuple[Watch, ...] = ()


"""Characters kept of one validator message, so an unbounded reply cannot grow the state."""


_TURN_REASONS = frozenset(
    {
        FailureReason.INVALID_REPLY,
        FailureReason.LOST,
        FailureReason.TURN_FAILED,
        FailureReason.TIMED_OUT,
        FailureReason.WAIT_REFUSED,
    }
)


class Failed(Value):
    """A request ended without a result. Every field is typed; nothing is free text.

    Each reason carries its own fields, and only those: ``TIMED_OUT`` and
    ``QUEUE_BUDGET_SPENT`` the ``limit_at`` (the run-clock time of the bound that passed), ``WORKSPACE_CLOSED`` / ``OWNER_GONE`` the
    ``watch`` that fired, ``REFUSED`` the ``cause`` core gave, ``INVALID_REPLY`` the reply
    ``errors``.

    ``decision`` names the search's own decision that ended; it is None for a request core
    made for itself, and core keeps such a failure to itself. A failure never names a request
    id: requests are core's, and a search knows only its decisions.
    """

    kind: Literal["failed"] = "failed"
    reason: FailureReason
    decision: DecisionId | None = None
    watch: Watch | None = None
    limit_at: Seconds | None = None
    cause: RejectionCode | None = None
    errors: tuple[ReplyError, ...] = ()
    turn: InvocationRef | None = None
    """The turn that failed, for the reasons that end one (and for a lost turn)."""
    turn_failure: TurnFailureKind | None = None
    digest: str | None = None
    refusal: WaitRefusal | None = None

    @model_validator(mode="after")
    def fields_match_reason(self) -> Failed:
        """Refuse a field that does not belong to the reason, or a reason missing its field."""
        expected = {
            "watch": self.reason in (FailureReason.WORKSPACE_CLOSED, FailureReason.OWNER_GONE),
            "limit_at": self.reason in (FailureReason.TIMED_OUT, FailureReason.QUEUE_BUDGET_SPENT),
            "cause": self.reason == FailureReason.REFUSED,
            "errors": self.reason == FailureReason.INVALID_REPLY,
            "turn_failure": self.reason == FailureReason.TURN_FAILED,
            "refusal": self.reason == FailureReason.WAIT_REFUSED,
        }
        if self.turn is not None and self.reason not in _TURN_REASONS:
            message = f"turn does not belong to reason {self.reason.value}"
            raise ValueError(message)
        if self.digest is not None and self.reason != FailureReason.TURN_FAILED:
            message = f"digest does not belong to reason {self.reason.value}"
            raise ValueError(message)
        for name, allowed in expected.items():
            present = (
                bool(getattr(self, name)) if name == "errors" else getattr(self, name) is not None
            )
            if present and not allowed:
                message = f"{name} does not belong to reason {self.reason.value}"
                raise ValueError(message)
            if allowed and not present:
                message = f"reason {self.reason.value} needs {name}"
                raise ValueError(message)
        return self


__all__ = [
    "REPLY_ERROR_MESSAGE_LIMIT",
    "Failed",
    "FailureReason",
    "FromDispatch",
    "Inherited",
    "Limit",
    "ReplyError",
    "ReplyErrorCode",
    "Until",
    "WaitRefusal",
    "Watch",
    "WhileAlive",
    "WhileOpen",
    "Within",
]
