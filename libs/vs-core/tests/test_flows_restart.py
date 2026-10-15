"""A code change between stop and resume: flows restart from their latest checkpoint.

The journal replays only code that is unchanged. After a code change every running flow is
restarted instead: the root from its latest checkpoint (refused without one), a task from its latest ``ctx.checkpoint`` snapshot
when the new code starts the flow registered under the same name with the same group key, and
the results of the decisions that were in flight are dropped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import FlowRun, RestartPolicy, fake_search
from vs_sim.api.testing import examples

from .flow_support import ROLES, Config, Plan, ask, open_workspace, start

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from vs_core.api import SearchDef


class Job(core.Value):
    """What the worker flow can be restarted from."""

    step: int = 0


class Seen(core.Value):
    """What a root publishes to show which code ran."""

    marker: str


class Gave(core.Value):
    """What the root publishes once its worker returned."""

    result: int


@core.flow(snapshot=Job, name="worker")
async def worker_v1(ctx: core.Flow, _job: Job) -> int:
    await ask(ctx, "one")
    ctx.checkpoint(Job(step=1))
    await ask(ctx, "two")
    return 2


@core.flow(snapshot=Job, name="worker")
async def worker_v2(ctx: core.Flow, job: Job) -> int:
    if job.step < 1:
        await ask(ctx, "uno")
        ctx.checkpoint(Job(step=1))
    await ask(ctx, "two")  # same label: ids must still not repeat
    await ask(ctx, "three")
    return 3


@core.flow(snapshot=Job, name="other")
async def renamed(ctx: core.Flow, _job: Job) -> int:
    await ask(ctx, "elsewhere")
    return 0


def search_for(
    worker: Callable[[core.Flow, Job], Coroutine[Any, Any, int]],
) -> SearchDef[Config]:
    @fake_search("flows", Config, ROLES)
    async def root(ctx: core.Flow, config: Config) -> None:
        ctx.checkpoint(config)  # the root resumes only from a snapshot it recorded
        async with ctx.group() as workers:
            workers.start("w", worker, Job())
            results = [result async for result in workers]
        ctx.publish(Gave(result=results[0]))  # ty: ignore[invalid-argument-type]

    return root


def _run_v1(*, restarting: bool = False) -> FlowRun[Config]:
    return FlowRun.start(search_for(worker_v1), Config(), restarting=restarting)


def test_after_a_code_change_flows_resume_from_their_latest_checkpoint() -> None:
    run = _run_v1()
    run.reply(run.expect_turn("one"), Plan(n=1))
    in_flight = run.expect_turn("two")
    assert in_flight.decision.root == "w/worker.2"

    run.swap(search_for(worker_v2))
    run.reply(in_flight, Plan(n=2))  # the first event the new code sees: a result of the old run

    # the task resumed at step 1 by its key: no "uno"; numbering continued, so no id repeats
    assert [a.decision.root for a in run.open if a is not in_flight] == ["w/worker.3"]
    assert run.expect_turn("two").decision.root == "w/worker.3"
    assert [d.decision for d in run.state.dropped] == [in_flight.decision]
    assert run.state.base is not None

    run.reply(run.expect_turn("two"), Plan(n=2))
    run.reply(run.expect_turn("three"), Plan(n=3))
    assert run.driver.published(run.state, Gave) == Gave(result=3)
    assert run.finished is not None


def test_a_restarted_run_can_itself_be_restarted_from_json_after_every_step() -> None:
    run = _run_v1()
    run.reply(run.expect_turn("one"), Plan(n=1))
    stale = run.expect_turn("two")
    run.swap(search_for(worker_v2))
    run.restarting = True
    run.reply(stale, Plan(n=2))
    run.reply(run.expect_turn("two"), Plan(n=2))
    run.reply(run.expect_turn("three"), Plan(n=3))
    assert run.driver.published(run.state, Gave) == Gave(result=3)
    assert run.finished is not None
    assert run.restarts >= 3


def test_a_child_without_a_checkpoint_is_refused_after_a_code_change() -> None:
    run = _run_v1()
    stale = run.expect_turn("one")  # worker_v1 has not checkpointed yet
    run.swap(search_for(worker_v2))
    with pytest.raises(core.FlowCodeChanged):
        run.reply(stale, Plan(n=1))


@core.flow(snapshot=Job, name="careful")
async def careful_v1(ctx: core.Flow, job: Job) -> int:
    ctx.checkpoint(job)  # the idiom: restartable from its arguments
    await ask(ctx, "first")
    return 1


@core.flow(snapshot=Job, name="careful")
async def careful_v2(ctx: core.Flow, job: Job) -> int:
    ctx.checkpoint(job)
    await ask(ctx, "again")
    return 2


def test_a_child_that_checkpoints_its_arguments_first_restarts_from_them() -> None:
    run = FlowRun.start(search_for(careful_v1), Config())
    stale = run.expect_turn("first")
    run.swap(search_for(careful_v2))
    run.reply(stale, Plan(n=1))
    run.reply(run.expect_turn("again"), Plan(n=2))
    assert run.finished is not None


@core.flow(snapshot=Job, name="holder")
async def holder_v1(ctx: core.Flow, job: Job) -> int:
    ctx.checkpoint(job)
    async with open_workspace(ctx, "ws") as ws:
        assert not isinstance(ws, core.NotOpened)
        await ask(ctx, "inside")
    return 1


@core.flow(snapshot=Job, name="holder")
async def holder_v2(ctx: core.Flow, job: Job) -> int:
    ctx.checkpoint(job)
    await ask(ctx, "outside")
    return 2


@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restart-every-step"])
def test_a_restart_settles_the_workspaces_a_flow_opened_after_its_checkpoint(
    *, restarting: bool
) -> None:
    run = FlowRun.start(search_for(holder_v1), Config(), restarting=restarting)
    opened = run.open_workspace(run.expect(core.StartAttempt))
    inside = run.expect_turn("inside")
    run.swap(search_for(holder_v2))
    run.reply(inside, Plan(n=1))  # the first event the new code sees
    release = run.expect(core.Withdraw, label="restart-settle")
    assert release.request.target == opened  # ty: ignore[unresolved-attribute]
    assert release.request.disposition.outcome == "cancelled"  # ty: ignore[unresolved-attribute]
    run.close_workspace(release)
    run.reply(run.expect_turn("outside"), Plan(n=2))
    assert run.finished is not None
    assert not [a for a in run.open if isinstance(a.request, core.Withdraw)]


def test_a_published_record_and_a_stop_survive_a_restart() -> None:
    @fake_search("flows", Config, ROLES)
    async def v1(ctx: core.Flow, config: Config) -> None:
        ctx.checkpoint(config)
        ctx.publish(Seen(marker="before"))
        await ask(ctx, "a")

    @fake_search("flows", Config, ROLES)
    async def v2(ctx: core.Flow, _config: Config) -> None:
        ctx.publish(Seen(marker="before"))
        await ask(ctx, "renamed")

    run = FlowRun.start(v1, Config())
    run.swap(v2)
    run.stop()
    assert run.driver.published(run.state, Seen) == Seen(marker="before")
    assert run.state.live.stop is not None
    assert run.state.base is not None


def test_a_change_to_a_root_without_a_checkpoint_is_refused() -> None:
    async def v1(ctx: core.Flow, _config: Config) -> None:
        await ask(ctx, "a")
        await ask(ctx, "b")

    async def v2(ctx: core.Flow, _config: Config) -> None:
        await ask(ctx, "a")
        await ask(ctx, "c")

    run = start(v1)
    run.reply(run.expect_turn("a"), Plan(n=1))
    stale = run.expect_turn("b")
    run.swap(fake_search("flows", Config, ROLES)(v2))
    with pytest.raises(core.FlowCodeChanged):
        run.reply(stale, Plan(n=2))


def test_a_change_to_a_root_that_checkpointed_restarts_it_from_the_snapshot() -> None:
    async def v1(ctx: core.Flow, config: Config) -> None:
        await ask(ctx, "a")
        ctx.checkpoint(Config(width=config.width + 1))
        await ask(ctx, "b")

    async def v2(ctx: core.Flow, config: Config) -> None:
        ctx.publish(Seen(marker=f"width {config.width}"))
        await ask(ctx, "c")

    run = start(v1)
    run.reply(run.expect_turn("a"), Plan(n=1))
    stale = run.expect_turn("b")
    run.swap(fake_search("flows", Config, ROLES)(v2))
    run.reply(stale, Plan(n=2))
    assert [d.decision for d in run.state.dropped] == [stale.decision]
    assert run.driver.published(run.state, Seen) == Seen(marker="width 3")
    run.reply(run.expect_turn("c"), Plan(n=3))
    assert run.finished is not None


def test_a_task_the_new_code_starts_as_another_flow_cannot_be_restarted() -> None:
    run = _run_v1()
    run.reply(run.expect_turn("one"), Plan(n=1))
    stale = run.expect_turn("two")
    run.swap(search_for(renamed))
    with pytest.raises(core.FlowCodeChanged):
        run.reply(stale, Plan(n=2))


class Renumbered(core.Value):
    """A snapshot type whose field the old worker's snapshot does not have."""

    stage: int


@core.flow(snapshot=Renumbered, name="worker")
async def retyped(ctx: core.Flow, _job: Renumbered) -> int:
    await ask(ctx, "two")
    return 2


def test_a_task_whose_snapshot_the_new_code_cannot_read_cannot_be_restarted() -> None:
    """The same flow name with an incompatible snapshot type is a code change, not a crash."""
    run = _run_v1()
    run.reply(run.expect_turn("one"), Plan(n=1))
    stale = run.expect_turn("two")

    @fake_search("flows", Config, ROLES)
    async def root(ctx: core.Flow, config: Config) -> None:
        ctx.checkpoint(config)
        async with ctx.group() as workers:
            workers.start("w", retyped, Renumbered(stage=0))

    run.swap(root)
    with pytest.raises(core.FlowCodeChanged):
        run.reply(stale, Plan(n=2))


def _parent_of(label: str) -> SearchDef[Config]:
    """A root that checkpoints, then runs one task that is not a registered flow."""

    async def loose(ctx: core.Flow) -> int:
        await ask(ctx, label)
        return 1

    @fake_search("flows", Config, ROLES)
    async def parent(ctx: core.Flow, config: Config) -> None:
        ctx.checkpoint(config)
        async with ctx.group() as tasks:
            tasks.start("l", loose)
            results = [result async for result in tasks]
        ctx.publish(Gave(result=results[0]))  # ty: ignore[invalid-argument-type]

    return parent


@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restart-every-step"])
def test_a_task_that_is_not_a_registered_flow_is_dropped_and_its_parent_starts_it_again(
    *, restarting: bool
) -> None:
    """A planning-like task without a snapshot does not block the restart of the whole run."""
    run = FlowRun.start(_parent_of("loose"), Config(), restarting=restarting)
    stale = run.expect_turn("loose")
    run.swap(_parent_of("loose again"))
    run.reply(stale, Plan(n=1))  # the first event the new code sees
    assert [d.decision for d in run.state.dropped] == [stale.decision]
    again = run.expect_turn("loose again")
    assert again.decision != stale.decision  # numbering continued: no decision id repeats
    run.reply(again, Plan(n=2))
    assert run.driver.published(run.state, Gave) == Gave(result=1)
    assert run.finished is not None


def test_only_a_registered_flow_has_checkpoints() -> None:
    async def sloppy(ctx: core.Flow) -> None:
        ctx.checkpoint(Job())

    async def parent(ctx: core.Flow, _config: Config) -> None:
        async with ctx.group() as tasks:
            tasks.start("s", sloppy)

    with pytest.raises(core.ContractError, match="registered"):
        start(parent)


def test_a_checkpoint_must_be_the_registered_snapshot_type() -> None:
    async def wrong(ctx: core.Flow, _config: Config) -> None:
        ctx.checkpoint(Plan(n=1))

    with pytest.raises(core.ContractError, match="snapshots are"):
        start(wrong)


def _answered(policy: RestartPolicy) -> int:
    run = _run_v1()
    run.restarting = policy
    run.reply(run.expect_turn("one"), Plan(n=1))
    return run.restarts


def test_a_restart_policy_restarts_only_after_the_steps_it_names() -> None:
    every = _answered(RestartPolicy(every=True))
    assert every >= 1
    assert _answered(RestartPolicy()) == 0
    assert _answered(RestartPolicy(steps=frozenset(range(1000)))) == every
    assert _answered(RestartPolicy(after_stop=True)) == 0


def _reader(reads: int, capabilities_at: int | None) -> SearchDef[Config]:
    """A root that reads the clock then asks, ``reads`` times; one read may be another type."""

    @fake_search("flows", Config, ROLES)
    async def root(ctx: core.Flow, config: Config) -> None:
        ctx.checkpoint(config)
        for index in range(reads):
            if index == capabilities_at:
                ctx.publish(ctx.capabilities)
            else:
                ctx.clock.now()
            await ask(ctx, f"s{index}")

    return root


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(data=st.data(), reads=st.integers(min_value=1, max_value=5))
def test_a_recorded_read_the_new_code_reads_as_another_type_restarts_the_run(
    data: st.DataObject, reads: int
) -> None:
    """Version B reads the capabilities where version A's journal holds a clock read.

    Wherever that read falls in the journal, the resume restarts the flows from their
    checkpoints instead of failing to decode the recorded value.
    """
    diverges = data.draw(st.integers(min_value=0, max_value=reads - 1), label="diverges")
    answered = data.draw(st.integers(min_value=diverges, max_value=reads - 1), label="answered")
    run = FlowRun.start(_reader(reads, None), Config())
    for index in range(answered):
        run.reply(run.expect_turn(f"s{index}"), Plan(n=index))
    stale = run.expect_turn(f"s{answered}")

    run.swap(_reader(reads, diverges))
    run.reply(stale, Plan(n=answered))  # replaying A's journal reaches the read B changed

    assert run.state.base is not None  # restarted, not replayed
    assert [d.decision for d in run.state.dropped] == [stale.decision]
    for index in range(reads):
        run.reply(run.expect_turn(f"s{index}"), Plan(n=index))
    assert run.published(core.Capabilities) is not None
    assert run.finished is not None
