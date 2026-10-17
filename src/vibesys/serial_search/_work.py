"""One round's workspace: open it, let the implementer work in it, settle it.

Both serial searches give a round an isolated candidate workspace and ask the implementer in it,
one paid turn at a time, until the attempt is over or its paid turns are spent. What each turn
does with the reply (a self-review, a judge, the trusted gates) is the search's; this is what
they share. The workspace is a resource, so it always settles: with the assertion the attempt
made, with a failure, or as cancelled when the operator stops the run. The round's workstream is
named for the hypothesis as the work begins and gets its verdict from the settlement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys.serial_search._spans import Settling, hypothesis_chosen, round_settled
from vs_core.api.search import (
    Again,
    AttemptBudget,
    Flow,
    NotOpened,
    RetryPolicy,
    Role,
    Settlement,
    Tries,
    Workspace,
    WorkspaceMode,
    WorkspacePlan,
    attempt_id,
    item_id,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vibesys.candidates.api import Baseline
    from vibesys.metrics import AcceptedReading
    from vibesys.serial_search._run import RoundStart
    from vs_core.api.search import Assessment, RevisionRef, Session


class Attempting(Settling, Protocol):
    """What the shared work needs to know of a search's attempt."""

    number: int
    baseline: Baseline
    """The input's measurement, which a candidate's gate compares it with."""
    readings: tuple[AcceptedReading, ...]
    """The trusted gates' readings of the candidate it keeps; empty when they did not measure it."""

    def failed(self, reason: str) -> object:
        """Record that the attempt cannot go on."""
        ...

    def asserted(self) -> tuple[RevisionRef | None, Assessment]:
        """The revision to keep and what to assert about it when the workspace closes."""
        ...


type Turn = Callable[[Session, Workspace], Awaitable[str | None]]
"""One paid turn of the implementer: None when the attempt is over, else why to ask again."""


@dataclass(frozen=True)
class Room:
    """Who works in a round, where, and for how long."""

    implementer: Role
    """The role that implements the round's hypothesis in its workspace."""
    mode: WorkspaceMode
    """How the workspace opens: its own copy, or the run's root on a host without sandboxes."""
    paid_turns: int
    """The paid implementer turns of the round, which is also the workspace's paid-turn limit."""
    started_by: tuple[str, ...] = ()
    """The agents whose plan the implementer works on: the orchestrator that designed the round's
    hypothesis; none for a round that continues a hypothesis an earlier round designed."""


async def work(
    ctx: Flow, start: RoundStart, attempt: Attempting, *, room: Room, turn: Turn
) -> Settlement | None:
    """Open the round's workspace, ask the implementer in it, settle it; core's settlement.

    The result is None when the workspace could not be opened: nothing was settled.
    """
    hypothesis_chosen(ctx, start, attempt.plan, attempt.base)
    settlement = await _work(ctx, attempt, room=room, turn=turn)
    round_settled(ctx, start, attempt, settlement)
    return settlement


async def _work(ctx: Flow, attempt: Attempting, *, room: Room, turn: Turn) -> Settlement | None:
    hypothesis = attempt.plan.hypothesis_id
    async with ctx.workspaces.open(
        attempt=attempt_id(hypothesis, attempt.number),
        item=item_id(hypothesis, attempt.number),
        plan=WorkspacePlan(mode=room.mode, base=attempt.base),
        budget=AttemptBudget(admission_charge=1, paid_invocation_limit=room.paid_turns),
    ) as opened:
        if isinstance(opened, NotOpened):
            attempt.failed(f"the workspace could not be opened ({opened.failed.reason.value})")
            return None
        session = ctx.agents.session(
            room.implementer, workspace=opened, revision=attempt.base, started_by=room.started_by
        )

        async def ask(tries: Tries[str]) -> bool | Again[str]:
            del tries
            again = await turn(session, opened)
            return True if again is None else Again(reason=again)

        await ctx.retry(ask, RetryPolicy(max_tries=room.paid_turns))
        revision, assessment = attempt.asserted()
        ctx.workspaces.keep(opened, revision, assessment)
    return opened.settlement if isinstance(opened, Workspace) else None
