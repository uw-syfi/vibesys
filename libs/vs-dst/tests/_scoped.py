"""A search whose workspace scope holds an agent turn and a measurement job when it is left.

The search opens one workspace, starts a turn of a session in it and a measurement submitted
from it, and leaves the workspace after a time of its own. The world takes a time for each, so
the three events (leave, turn ends, job ends) fall in any order, and the scope is closed with its
turn, its job, both or neither still open.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from vs_core.api import (
    Access,
    AttemptBudget,
    AttemptId,
    DispatchTurn,
    EnsureWorkspace,
    ExecuteRegisteredOperation,
    Flow,
    FlowState,
    ItemId,
    NotOpened,
    NoWinner,
    ResourceId,
    ResumeSessionTurn,
    RetainRevision,
    RunEnvelope,
    SnapshotAndRetain,
    SubmitMeasurement,
    Value,
    Within,
    Workspace,
    WorkspaceMode,
    WorkspacePlan,
)
from vs_core.api.testing import (
    FAKE_REGISTRY,
    Harness,
    Prompt,
    Succeeded,
    fake_search,
    sample_limits,
    sample_retries,
)
from vs_dst.api import RunSetup
from vs_runtime.api.testing import Launch

from ._tiny import BASELINE, FACTS, KEPT
from ._turns import ASKER, TurnsWorld
from .test_close_withdraws import measure

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer

WORKER = ASKER.model_copy(
    update={
        "name": "worker",
        "workspace": WorkspaceMode.EXCLUSIVE_ROOT,
        "access": Access.WRITE_CANDIDATE,
    }
)
"""An agent that works in the workspace, so its turns belong to the scope that closes."""


class Held(Value):
    """What the scope holds, how long each takes, and when the search leaves the workspace."""

    turn: bool = True
    job: bool = True
    retain: bool = False
    turn_takes: float = 1.0
    render_takes: float = 0.0
    job_takes: float = 1.0
    leaves_at: float = 1.0


async def _turn(ctx: Flow, workspace: Workspace) -> None:
    session = ctx.agents.session(WORKER, workspace=workspace, revision=BASELINE)
    await session.turn(Prompt(text="work"), within=100.0)


@fake_search("hold-then-leave", Held, (WORKER,), snapshot=Held)
async def hold_then_leave(ctx: Flow, config: Held) -> NoWinner:
    """Start the scope's work, leave the workspace at ``leaves_at``, and end the run."""
    ctx.checkpoint(config)
    async with (
        ctx.group(stops=False) as work,
        ctx.workspaces.open(
            attempt=AttemptId(root="attempt:0"),
            item=ItemId(root="item:0"),
            plan=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=ctx.facts.baseline),
            budget=AttemptBudget(),
            within=Within(limit=1000.0),
        ) as workspace,
    ):
        if isinstance(workspace, NotOpened):
            return NoWinner(reason="not opened")
        if config.turn:
            work.start("turn", _turn, workspace)
        if config.job:
            work.start("job", measure, workspace)
        if config.retain:
            ctx.workspaces.keep(workspace, BASELINE, KEPT)
        await ctx.timer(ctx.clock.now() + config.leaves_at)
    return NoWinner(reason="left")


class HeldWorld(TurnsWorld):
    """The turns world with workspaces, whose turn and job take the time the config says."""

    def __init__(self, config: Held) -> None:
        super().__init__(1)
        self.config = config
        self.setup = RunSetup(
            new_launch=lambda: Launch(hold_then_leave, config),
            harness=Harness(
                registry=FAKE_REGISTRY,
                facts=FACTS,
                limits=sample_limits(
                    max_attempts=8, max_turns=8, max_parallel=1, request_retries=sample_retries(2)
                ),
                envelope_type=RunEnvelope[FlowState],
                schemas=hold_then_leave.reply_schemas,
            ),
            schemas=hold_then_leave.reply_schemas,
        )
        self.workspaces = 0
        self.asked: dict[str, Request] = {}
        """Every request the far side was asked, by the tag its step has in the log."""

    def takes(self, request: Request) -> float:
        match request:
            case DispatchTurn() | ResumeSessionTurn():
                return self.config.turn_takes
            case SubmitMeasurement():
                return self.config.job_takes
            case ExecuteRegisteredOperation():
                return self.config.render_takes
            case _:
                return 0.0

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if request.request_id is not None:
            self.asked[request.request_id.root] = request
        match request:
            case EnsureWorkspace():
                self.workspaces += 1
                return Succeeded(
                    resource_id=ResourceId(root=f"workspace:{self.workspaces}"),
                    revision=request.plan.base,
                )
            case SnapshotAndRetain() | RetainRevision():
                return Succeeded(revision=BASELINE)
            case _:
                return super().__call__(request, core)


JOB_NEVER_ENDS = 1_000.0
"""A measurement job that outlasts the run: only the close of its scope ends it (it is withdrawn)."""


def every_order() -> list[Held]:
    """Each way the scope can be closed: with its turn, its job, both or neither still open.

    The turn ends before the search leaves, after it, or long after it; the job is open when the
    scope closes and ends by the close's withdrawal. Each is with and without a retention.
    """
    cases: list[Held] = []
    for retain, leave, turn_takes in itertools.product((False, True), (1.0, 3.0), (0.5, 2.0)):
        for turn, job in ((True, False), (False, True), (True, True)):
            cases.append(
                Held(
                    turn=turn,
                    job=job,
                    retain=retain,
                    leaves_at=leave,
                    turn_takes=turn_takes,
                    job_takes=JOB_NEVER_ENDS,
                )
            )
    return cases


def settles_over_an_open_inspection(config: Held) -> bool:
    """The orders whose restarted host settles the scope while an inspection of its session setup is open.

    A kill that lands while a turn's session setup is in flight, with the search leaving the
    workspace before the setup runs again, settles the scope over the setup's open inspection
    (#3716). Closure is not claimed of these; progress is claimed of every order.
    """
    return config.turn and config.leaves_at <= 1.0
