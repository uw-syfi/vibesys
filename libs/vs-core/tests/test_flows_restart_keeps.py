"""A code change keeps what the flows hold: their open workspaces, their measurements in flight.

A flow that checkpoints while it holds a workspace gets the same workspace back after a code
change (``ctx.workspaces.reopen``). A workspace the restart cannot give back is settled as the
flow's salvage says, and the restart records why. A measurement in flight at the change answers
the resumed flow that asks for it again, instead of being measured twice. A root that declares a
snapshot model resumes from what it knew.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

import vs_core.api as core
from vs_core.api.testing import FlowRun, RestartPolicy, code_changed, fake_search

from .flow_support import ROLES, Config, Plan, ask, open_workspace

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from vs_core.api import SearchDef
    from vs_core.api.testing import Asked

REVISION = core.RevisionRef.of_git_commit("c1")
WIP = core.Assessment(outcome="cancelled", retention="wip")
KEPT = core.Assessment(outcome="succeeded", eligible=True)


class Job(core.Value):
    """Where the holder resumes: from its start, or inside the workspace it holds."""

    attempt: core.AttemptRef | None = None


type Worker = Callable[[core.Flow, Job], Coroutine[Any, Any, int]]


def holder(label: str, *, reopens: bool = True, checkpoints_inside: bool = True) -> Worker:
    """A flow that opens a workspace, salvages it as work in progress and asks ``label`` in it.

    With ``checkpoints_inside`` it checkpoints while it holds the workspace; resumed from that
    checkpoint it reopens the workspace (unless not ``reopens``) and asks again.
    """

    @core.flow(snapshot=Job, name="holder")
    async def run(ctx: core.Flow, job: Job) -> int:
        ctx.checkpoint(job)
        if job.attempt is not None:
            if not reopens:
                return 0
            async with ctx.workspaces.reopen(job.attempt) as ws:
                if isinstance(ws, core.NotReopened):
                    return -1
                await ask(ctx, label)
                ctx.workspaces.keep(ws, REVISION, KEPT)
            return 2
        async with open_workspace(ctx, "ws") as ws:
            assert not isinstance(ws, core.NotOpened)
            ctx.workspaces.salvage(ws, REVISION, WIP)
            if checkpoints_inside:
                ctx.checkpoint(Job(attempt=ws.ref))
            await ask(ctx, label)
            ctx.workspaces.keep(ws, REVISION, KEPT)
        return 1

    return run


class Gave(core.Value):
    """What the root publishes once its worker returned, and checkpoints so it is not redone."""

    kind: str = "gave"
    result: int


def parent(worker: Worker | None) -> SearchDef[Config]:
    """A root that runs ``worker`` under the key ``w`` (none: the new code dropped it)."""

    @fake_search("keeps", Config, ROLES, snapshot=Gave)
    async def root(ctx: core.Flow, start: Config | Gave) -> None:
        ctx.checkpoint(start)
        if isinstance(start, Gave):
            ctx.publish(start)
            return
        if worker is None:
            await ask(ctx, "no worker")
            return
        async with ctx.group() as workers:
            workers.start("w", worker, Job())
            results = [result async for result in workers]
        gave = Gave(result=results[0])  # ty: ignore[invalid-argument-type]
        ctx.checkpoint(gave)
        ctx.publish(gave)

    return root


def _held(run: FlowRun[Config], label: str) -> tuple[core.AttemptRef, Asked]:
    opened = run.open_workspace(run.expect(core.StartAttempt))
    return opened, run.expect_turn(label)


def _settles(run: FlowRun[Config], label: str) -> list[Asked]:
    return [a for a in run.open if isinstance(a.request, core.Withdraw) and a.label == label]


@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restart-every-step"])
def test_a_workspace_held_at_the_checkpoint_is_reopened_after_a_code_change(
    *, restarting: bool
) -> None:
    run = FlowRun.start(parent(holder("v1")), Config(), restarting=restarting)
    opened, stale = _held(run, "v1")
    run.swap(parent(holder("v2")))
    run.reply(stale, Plan(n=1))  # the first event the new code sees: the flows restart

    assert not _settles(run, "restart-settle")
    assert run.state.restart_settled == ()
    run.reply(run.expect_turn("v2"), Plan(n=2))
    settle = run.expect(core.Withdraw, label="settle")
    assert settle.request.target == opened  # ty: ignore[unresolved-attribute]
    assert settle.request.disposition.retention == "candidate"  # ty: ignore[unresolved-attribute]
    run.close_workspace(settle)
    assert run.driver.published(run.state, Gave) == Gave(result=2)
    assert len([d for d in run.decisions if _starts(d)]) == 1  # never opened twice
    assert run.finished is not None


def _starts(decision: core.Decision) -> bool:
    return isinstance(decision, core.Wait) and isinstance(decision.request, core.StartAttempt)


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        (
            holder("v1", checkpoints_inside=False),
            holder("v2", checkpoints_inside=False),
            core.RestartSettleReason.OPENED_AFTER_CHECKPOINT,
        ),
        (holder("v1"), None, core.RestartSettleReason.NOT_RESTARTED),
        (holder("v1"), holder("v2", reopens=False), core.RestartSettleReason.NOT_REOPENED),
    ],
    ids=["opened-after-checkpoint", "not-restarted", "not-reopened"],
)
def test_a_workspace_the_restart_cannot_give_back_settles_by_its_salvage_with_its_reason(
    before: Worker, after: Worker | None, reason: core.RestartSettleReason
) -> None:
    run = FlowRun.start(parent(before), Config())
    opened, stale = _held(run, "v1")
    run.swap(parent(after))
    run.reply(stale, Plan(n=1))

    (settle,) = _settles(run, "restart-settle")
    assert settle.request.target == opened  # ty: ignore[unresolved-attribute]
    disposition = settle.request.disposition  # ty: ignore[unresolved-attribute]
    assert isinstance(disposition, core.Settle)
    assert (disposition.retention, disposition.candidate) == ("wip", REVISION)
    assert run.state.restart_settled == (
        core.RestartSettled(attempt=opened, flow="w/", reason=reason, retention="wip"),
    )
    run.close_workspace(settle)


def test_a_workspace_whose_settlement_is_in_flight_is_left_to_it() -> None:
    run = FlowRun.start(parent(holder("v1")), Config())
    opened, asked = _held(run, "v1")
    run.reply(asked, Plan(n=1))
    settle = run.expect(core.Withdraw, label="settle")
    run.swap(parent(None))
    run.advance_to(1.0)

    assert not _settles(run, "restart-settle")
    assert run.state.restart_settled == (
        core.RestartSettled(
            attempt=opened,
            flow="w/",
            reason=core.RestartSettleReason.SETTLING,
            retention="candidate",
        ),
    )
    run.close_workspace(settle)
    assert run.state.live.in_flight == ()


def test_a_workspace_opening_at_the_change_opens_for_the_resumed_flow() -> None:
    run = FlowRun.start(parent(holder("v1")), Config())
    start = run.expect(core.StartAttempt)
    run.swap(parent(holder("v2")))
    run.advance_to(1.0)

    assert len([d for d in run.decisions if _starts(d)]) == 1  # asked for again, not sent again
    opened = run.open_workspace(start)
    run.reply(run.expect_turn("v2"), Plan(n=1))
    settle = run.expect(core.Withdraw, label="settle")
    assert settle.request.target == opened  # ty: ignore[unresolved-attribute]
    run.close_workspace(settle)
    assert run.driver.published(run.state, Gave) == Gave(result=1)
    assert run.state.restart_settled == ()


def verifier(label: str, *, opens: bool = True) -> Worker:
    """A flow that asks ``verify`` before it opens a workspace, so it is redone from its start.

    Without ``opens`` it ends after ``verify`` and never asks for the workspace.
    """

    @core.flow(snapshot=Job, name="holder")
    async def run(ctx: core.Flow, job: Job) -> int:
        ctx.checkpoint(job)
        await ask(ctx, "verify")
        if not opens:
            return 0
        async with open_workspace(ctx, "ws") as ws:
            assert not isinstance(ws, core.NotOpened)
            await ask(ctx, label)
            ctx.workspaces.keep(ws, REVISION, KEPT)
        return 1

    return run


@pytest.mark.parametrize("opens", [True, False], ids=["asks-later", "never-asks"])
def test_work_in_flight_that_the_resumed_flow_does_not_ask_for_in_the_restart_step_is_let_go(
    *, opens: bool
) -> None:
    """The new code decides what it needs, and the restart step is where it says so.

    The flow is redone from its start and asks ``verify`` first, so it has not asked for the
    workspace when the restart step ends. What was in flight then has no owner: the workspace that
    opens for it is settled as unclaimed (through core, which carries it out), never kept for a
    flow that may ask later. A flow that does ask later gets new work, and the old attempt is not
    its own: the narrowing is deliberate, and it costs one more workspace, never a hung slot.
    """
    run = FlowRun.start(parent(verifier("v1")), Config())
    run.reply(run.expect_turn("verify"), Plan(n=1))
    start = run.expect(core.StartAttempt)
    run.swap(code_changed(parent(verifier("v2", opens=opens))))
    run.advance_to(1.0)  # the first event the new code sees: the flows restart
    verify = run.expect_turn("verify")
    assert run.state.live.in_flight == ()  # nothing waits for a flow that may ask later

    opened = run.open_workspace(start)  # answered after the restart step: for no one
    (settle,) = _settles(run, "restart-settle")
    assert settle.request.target == opened  # ty: ignore[unresolved-attribute]
    assert [item.reason for item in run.state.restart_settled] == [
        core.RestartSettleReason.UNCLAIMED
    ]
    run.close_workspace(settle)
    run.reply(verify, Plan(n=1))
    if not opens:
        assert len([d for d in run.decisions if _starts(d)]) == 1
        return
    assert len([d for d in run.decisions if _starts(d)]) == 2  # asked again later: new work
    again = run.expect(core.StartAttempt)
    assert again.decision != start.decision
    second = run.open_workspace(again)
    run.reply(run.expect_turn("v2"), Plan(n=1))
    settle = run.expect(core.Withdraw, label="settle")
    assert settle.request.target == second  # ty: ignore[unresolved-attribute]
    run.close_workspace(settle)
    assert run.driver.published(run.state, Gave) == Gave(result=1)
    assert run.state.live.in_flight == ()


def test_a_workspace_that_opens_for_no_flow_is_settled_not_leaked() -> None:
    run = FlowRun.start(parent(holder("v1")), Config())
    start = run.expect(core.StartAttempt)
    run.swap(parent(None))
    run.advance_to(1.0)
    opened = run.open_workspace(start)

    (settle,) = _settles(run, "restart-settle")
    assert settle.request.target == opened  # ty: ignore[unresolved-attribute]
    assert settle.request.disposition.retention == "discard"  # ty: ignore[unresolved-attribute]
    assert [item.reason for item in run.state.restart_settled] == [
        core.RestartSettleReason.UNCLAIMED
    ]


def test_a_workspace_without_a_salvage_settles_discarded_as_before() -> None:
    @core.flow(snapshot=Job, name="holder")
    async def bare(ctx: core.Flow, job: Job) -> int:
        ctx.checkpoint(job)
        async with open_workspace(ctx, "ws") as ws:
            assert not isinstance(ws, core.NotOpened)
            await ask(ctx, "v1")
        return 1

    run = FlowRun.start(parent(bare), Config())
    _opened, stale = _held(run, "v1")
    run.swap(parent(None))
    run.reply(stale, Plan(n=1))
    (settle,) = _settles(run, "restart-settle")
    assert settle.request.disposition.retention == "discard"  # ty: ignore[unresolved-attribute]
    assert [item.reason for item in run.state.restart_settled] == [
        core.RestartSettleReason.OPENED_AFTER_CHECKPOINT
    ]


def test_a_flow_reopens_only_a_workspace_the_restart_kept_for_it() -> None:
    elsewhere = core.AttemptRef(attempt_id=core.AttemptId(root="elsewhere"), generation=0)

    @core.flow(snapshot=Job, name="holder")
    async def stranger(ctx: core.Flow, job: Job) -> int:
        ctx.checkpoint(job)
        async with ctx.workspaces.reopen(elsewhere) as ws:
            reopened = not isinstance(ws, core.NotReopened)
        return 1 if reopened else -1

    run = FlowRun.start(parent(stranger), Config())
    assert run.driver.published(run.state, Gave) == Gave(result=-1)
    assert not [d for d in run.decisions if isinstance(d, core.Wait) and _withdraws(d)]


def _withdraws(decision: core.Wait) -> bool:
    return isinstance(decision.request, core.Withdraw)


@pytest.mark.parametrize("oldest_first", [True, False], ids=["oldest-first", "newest-first"])
@pytest.mark.parametrize("step", range(5))
def test_a_held_workspace_settles_exactly_once_whatever_step_the_code_changes_at(
    step: int, *, oldest_first: bool
) -> None:
    """The workspace opens once and is settled once, never discarded, wherever the change falls.

    Core answers what is open oldest first or newest first: a settlement in flight at the change
    answers the resumed flow's close either way, before or after the flow asks for it.
    """
    search = parent(holder("v1"))
    policy = RestartPolicy(steps=frozenset({step}), changes=(code_changed(search),))
    run = FlowRun.start(search, Config(), restarting=policy)
    opened = run.open_workspace(run.expect(core.StartAttempt))
    while run.finished is None:
        waits = [a for a in run.open if a.until is not None]
        asked = waits[0] if oldest_first else waits[-1]
        if isinstance(asked.request, core.Withdraw):
            run.close_workspace(asked)
        else:
            run.reply(asked, Plan(n=1))

    assert run.restarts == 1
    assert len([d for d in run.decisions if _starts(d)]) == 1
    settles = [d.request for d in run.decisions if isinstance(d, core.Wait) and _withdraws(d)]
    assert [s.target for s in settles] == [opened]  # ty: ignore[unresolved-attribute]
    assert settles[0].disposition.retention != "discard"  # ty: ignore[unresolved-attribute]


def test_a_hold_persisted_as_a_bare_attempt_is_read_and_settled_as_before() -> None:
    attempt = core.AttemptRef(attempt_id=core.AttemptId(root="old"), generation=0)
    legacy = {
        "path": "w/",
        "name": "holder",
        "snapshot_json": "{}",
        "holds": [json.loads(attempt.model_dump_json())],
        "checkpointed": True,
    }
    live = core.FlowState.model_fields["live"].annotation
    assert live is not None
    flow = live.model_fields["flows"].annotation.__args__[0]
    read = flow.model_validate_json(json.dumps(legacy))
    (held,) = read.holds
    assert (held.attempt, held.carried, held.salvage) == (attempt, False, None)


# -- measurements in flight ------------------------------------------------------------------------


def measurement(revision: core.RevisionRef, *, at: float) -> core.MeasurementPlan:
    """A one-stage measurement of ``revision``, submitted at ``at``."""
    return core.MeasurementPlan(
        purpose="local-validation",
        candidate=revision,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        stages=(core.MeasurementStage(stage_id="measure", execution_budget=10.0),),
        policy="ordered",
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="recipe"),
        submitted_at=at,
        queue_allowance=0.0,
        accuracy_gated=(),
    )


class Measured(core.Value):
    """What the measuring root publishes: the status of the measurement it waited for."""

    status: str


def measuring(revision: core.RevisionRef) -> SearchDef[Config]:
    """A root that measures ``revision`` (asked at the current time) and publishes the result."""

    @fake_search("keeps", Config, ROLES)
    async def root(ctx: core.Flow, config: Config) -> None:
        ctx.checkpoint(config)
        plan = measurement(revision, at=ctx.clock.now())
        found = await ctx.evaluation.measure(plan, within=30.0)
        ctx.publish(Measured(status=type(found).__name__))

    return root


def _result(asked: Asked) -> core.MeasurementFinished:
    request = asked.request
    assert isinstance(request, core.Measure)
    result = core.MeasurementResult(
        scope=request.scope, evidence=(), status=core.ObservationStatus.SUCCEEDED
    )
    return core.MeasurementFinished(decision=asked.decision, result=result)


@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restart-every-step"])
def test_a_measurement_in_flight_answers_the_resumed_flow_that_asks_for_it_again(
    *, restarting: bool
) -> None:
    search = measuring(REVISION)
    run = FlowRun.start(search, Config(), restarting=restarting)
    (in_flight,) = [a for a in run.open if isinstance(a.request, core.Measure)]
    run.view.now_at = 5.0  # the resumed flow asks later: the same measurement, another time
    run.swap(code_changed(search))
    run.advance_to(5.0)  # the first event the new code sees: the flows restart

    assert run.state.base is not None
    measures = [d for d in run.decisions if isinstance(d, core.Wait) and _measures(d)]
    assert len(measures) == 1  # not measured twice
    run.answer(in_flight, _result(in_flight))
    assert run.driver.published(run.state, Measured) == Measured(status="MeasurementResult")
    assert run.state.dropped == ()
    assert run.finished is not None


def _measures(decision: core.Wait) -> bool:
    return isinstance(decision.request, core.Measure)


def test_a_measurement_the_new_code_asks_differently_is_measured_again() -> None:
    run = FlowRun.start(measuring(REVISION), Config())
    (in_flight,) = [a for a in run.open if isinstance(a.request, core.Measure)]
    run.swap(measuring(core.RevisionRef.of_git_commit("c2")))
    run.advance_to(1.0)

    fresh = [a for a in run.open if isinstance(a.request, core.Measure) and a is not in_flight]
    assert len(fresh) == 1
    run.answer(in_flight, _result(in_flight))  # the old one's result reaches no flow
    assert [d.decision for d in run.state.dropped] == [in_flight.decision]
    run.answer(fresh[0], _result(fresh[0]))
    assert run.finished is not None


# -- a root that resumes from what it knew -------------------------------------------------------


class Progress(core.Value):
    """What the counting root knows: how many answers it has added up, and their sum."""

    kind: str = "progress"
    asked: int
    total: int


class Total(core.Value):
    """What the counting root publishes when it is done."""

    total: int


def counting(label: str) -> SearchDef[Config]:
    """A root that asks ``config.width`` times and adds the answers up, checkpointing each."""

    @fake_search("keeps", Config, ROLES, snapshot=Progress)
    async def root(ctx: core.Flow, start: Config | Progress) -> None:
        width = start.width if isinstance(start, Config) else 3
        known = start if isinstance(start, Progress) else Progress(asked=0, total=0)
        ctx.checkpoint(known)
        while known.asked < width:
            answer = await ask(ctx, f"{label}{known.asked}")
            assert isinstance(answer, core.TurnFinished)
            assert isinstance(answer.reply, Plan)
            known = Progress(asked=known.asked + 1, total=known.total + answer.reply.n)
            ctx.checkpoint(known)
        ctx.publish(Total(total=known.total))

    return root


def test_a_root_that_declares_a_snapshot_resumes_from_what_it_knew() -> None:
    run = FlowRun.start(counting("a"), Config(width=3))
    run.reply(run.expect_turn("a0"), Plan(n=5))
    stale = run.expect_turn("a1")
    run.swap(counting("b"))
    run.reply(stale, Plan(n=7))  # dropped: asked again under the new code

    assert not [a for a in run.open if a.prompt is not None and a.prompt.text == "b0"]
    run.reply(run.expect_turn("b1"), Plan(n=1))
    run.reply(run.expect_turn("b2"), Plan(n=1))
    assert run.driver.published(run.state, Total) == Total(total=7)


class Incompatible(core.Value):
    """A root snapshot the old snapshot's JSON does not decode as."""

    kind: str = "incompatible"
    stage: int


def test_a_root_snapshot_the_new_code_cannot_read_is_a_code_change_not_a_crash() -> None:
    @fake_search("keeps", Config, ROLES, snapshot=Incompatible)
    async def staged(ctx: core.Flow, start: Config | Incompatible) -> None:
        ctx.checkpoint(start if isinstance(start, Incompatible) else Incompatible(stage=0))
        await ask(ctx, "b0")

    run = FlowRun.start(counting("a"), Config(width=3))
    run.reply(run.expect_turn("a0"), Plan(n=5))
    stale = run.expect_turn("a1")
    run.swap(staged)
    with pytest.raises(core.FlowCodeChanged):
        run.reply(stale, Plan(n=7))
