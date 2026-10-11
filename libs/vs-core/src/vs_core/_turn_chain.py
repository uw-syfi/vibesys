"""The bookkeeping of one role's chain of turns, and the protocol rules that move it.

A *turn chain* is the sequence of asks a strategy makes to get one logical answer from an
agent: the first ask (free or paid), a correction when the reply was invalid, a re-ask when the
transport lost the turn, and a resume after the agent yielded to wait for evaluations. Core
requires some rules of every caller; they live here once so a strategy cannot get them wrong:

- A correction names the invalid turn as its predecessor and counts against a limit.
- A lost turn is asked again as a correction of the lost one, after a backoff on the run clock,
  and counts against its own limit. A resume cannot be asked again, because core authorizes
  each continuation's resume once.
- A yield is completed only by core's `TurnSuspended`, and a resume names its continuation and
  the invocation core authorized, never a predecessor.

The chain holds no prompt knowledge: ``C`` is the strategy's own prompt-context type. Every
transition returns a new chain and never proposes a decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field

from vs_core._evidence_views import turn_candidate
from vs_core.types.common import (
    ArtifactRef,
    ContinuationId,
    InvocationId,
    InvocationRef,
    Value,
)

if TYPE_CHECKING:
    from vs_core.types.common import AttemptId, RevisionRef
    from vs_core.types.kernel import RunView

type Charge = Literal["free", "paid", "correction", "resume"]


class TurnChain[C: Value](Value):
    """Where one logical turn is: its serial, charge, limits spent and what core said."""

    serial: int = Field(default=0, ge=0)
    corrections: int = Field(default=0, ge=0)
    # Times this logical turn was lost to the transport and asked again.
    drops: int = Field(default=0, ge=0)
    # Run-clock time before which a lost turn must not be asked again; None when due.
    ask_not_before: float | None = Field(default=None, ge=0)
    charge: Charge = "paid"
    context: C | None = None
    prompts: tuple[ArtifactRef, ...] = ()
    tool_policy: ArtifactRef | None = None
    # Predecessor of a resumed turn: the suspended invocation core is resuming.
    invocation: InvocationRef | None = None
    # The invocation ID core authorized for a resume; None for every other turn.
    resume_as: InvocationId | None = None
    continuation: ContinuationId | None = None
    # The reply asked to wait for evaluations; core's `TurnSuspended` completes the yield.
    yielded: bool = False
    # Earlier turns of this logical answer, oldest first: the suspended turn a resume continues,
    # and the invalid or lost turns a correction replaces. A paid or free ask starts empty.
    lineage: tuple[InvocationRef, ...] = ()

    def with_prompts(
        self, prompts: tuple[ArtifactRef, ...], tool_policy: ArtifactRef | None
    ) -> Self:
        """The chain once its prompt render succeeded."""
        return self.model_copy(update={"prompts": prompts, "tool_policy": tool_policy})

    def followed_by(self, *, charge: Charge, context: C | None = None) -> Self:
        """A new logical turn after this one: serial one past, nothing else carried over."""
        return self.model_copy(
            update={
                "serial": self.serial + 1,
                "corrections": 0,
                "drops": 0,
                "ask_not_before": None,
                "charge": charge,
                "context": context,
                "prompts": (),
                "tool_policy": None,
                "invocation": None,
                "resume_as": None,
                "continuation": None,
                "yielded": False,
                "lineage": (),
            }
        )

    def corrected(self, invalid: InvocationRef, context: C | None, *, limit: int) -> Self | None:
        """Ask the role to fix its reply, or None once ``limit`` corrections are spent.

        The correction names the invalid turn as its predecessor and takes a new serial.
        """
        if self.corrections >= limit:
            return None
        return self.followed_by(charge="correction", context=context).model_copy(
            update={
                "corrections": self.corrections + 1,
                "invocation": invalid,
                "lineage": (*self.lineage, invalid),
            }
        )

    def asked_again(
        self, lost: InvocationRef, *, now: float, limit: int, backoff: float
    ) -> Self | None:
        """Ask the same question after the transport lost the turn, or None when it may not.

        The new turn corrects the lost one (it consumes no paid retry and no correction),
        keeps the question's context, and waits ``backoff`` seconds on the run clock. A resume
        cannot be asked again, and drops are limited per logical turn.
        """
        if self.charge == "resume" or self.drops >= limit:
            return None
        return self.model_copy(
            update={
                "serial": self.serial + 1,
                "drops": self.drops + 1,
                "charge": "correction",
                "invocation": lost,
                "lineage": (*self.lineage, lost),
                "prompts": (),
                "ask_not_before": now + backoff,
            }
        )

    def candidate(
        self, view: RunView, attempt: AttemptId, invocation: InvocationRef
    ) -> RevisionRef | None:
        """The latest revision retained by ``invocation`` or by an earlier turn of this chain.

        This is the one rule every strategy uses to read a reply's candidate. A resumed turn
        usually only reads its verdict and edits nothing, so its candidate is the checkpoint of
        the suspended turn it continues. A turn outside the chain, such as a paid retry, is not
        credited with an older turn's checkpoint.
        """
        return turn_candidate(view, attempt, invocation, predecessors=self.lineage)

    def yielding(self) -> Self:
        """The reply asked to wait: only core's `TurnSuspended` completes the yield."""
        return self.model_copy(update={"yielded": True})

    def suspended(self, invocation: InvocationRef, continuation: ContinuationId) -> Self:
        """Core recorded the continuation: wait for `ResumeAuthorized` of exactly that ID."""
        return self.model_copy(
            update={"invocation": invocation, "continuation": continuation, "yielded": True}
        )

    def resumed(
        self, *, authorized: InvocationId, continuation: ContinuationId, context: C | None
    ) -> Self:
        """The turn core authorized to resume: asked once, naming its continuation.

        It carries the suspended invocation as predecessor information only; the request
        built from it names the continuation, never a predecessor.
        """
        return self.followed_by(charge="resume", context=context).model_copy(
            update={
                "invocation": self.invocation,
                "lineage": self.lineage
                if self.invocation is None
                else (*self.lineage, self.invocation),
                "resume_as": authorized,
                "continuation": continuation,
            }
        )
