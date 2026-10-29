"""Story tests for the flow vocabulary, each run live and with a restart after every step.

A story reads as a script: the search opens waits, the test answers them. Every story runs
twice: once on one live search, once where the state is written to JSON and a new search
replays it after each step. Both must tell the same story. A flow never finishes the run, so
a story reads how its flow went off the run's ``Finish`` (the reason of a ``NoWinner`` the root
returns, a ``cancelled`` one after a stop) or off what the flow published.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import vs_core.api as core
from vs_core.api.testing import FakeView

from .flow_support import FACTS, LENIENT, Config, Plan, answer_of, ask, open_workspace, start

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Generator

    from vs_core.api.testing import FlowRun

type Begin = Callable[..., FlowRun[Config]]


@pytest.fixture(params=[False, True], ids=["live", "restart-every-step"])
def begin(request: pytest.FixtureRequest) -> Begin:
    """Starts a flow; the fixture's two settings are a live run and a restart after every step."""
    restarting = bool(request.param)
    return lambda root, view=None, adopting=True: start(
        root, restarting=restarting, view=view, adopting=adopting
    )


def ended(run: FlowRun[Config]) -> core.RunResultProposal:
    """The result of the ``Finish`` the framework issued."""
    assert run.finished is not None
    return run.finished.result


def waits(run: FlowRun[Config]) -> list[str]:
    """The open turns as ``<flow path><prompt text>``: who asks what."""
    return [f"{a.path}{a.prompt.text}" for a in run.open if a.prompt is not None]


def answer(run: FlowRun[Config], text: str, n: int = 0) -> None:
    """Answer the open turn whose prompt is ``text``."""
    run.reply(run.expect_turn(text), Plan(n=n))


async def worker(ctx: core.Flow, text: str = "work") -> int:
    """A task that asks one turn and gives the answer."""
    return answer_of(await ask(ctx, text))


# -- turns: effects, outcomes as values ---------------------------------------------------------


async def plan_then_end(ctx: core.Flow, _config: Config) -> core.NoWinner:
    match await ask(ctx, "planner", LENIENT):
        case core.TurnFinished(reply=Plan(n=n)):
            return core.NoWinner(reason=f"planned {n}")
        case core.Failed(reason=reason):
            return core.NoWinner(reason=reason.value)
        case other:
            raise AssertionError(other)


def test_a_turn_returns_the_typed_reply_and_the_flow_matches_on_it(begin: Begin) -> None:
    run = begin(plan_then_end)
    asked = run.expect_turn("planner")
    assert asked.decision.root == "lenient.1"
    assert asked.until == core.Within(limit=10.0)
    run.reply(asked, Plan(n=3))
    assert ended(run).reason == "no-winner: planned 3"
    assert run.timeline() == "lenient.1:Plan"


def test_a_failure_is_an_outcome_not_an_exception(begin: Begin) -> None:
    run = begin(plan_then_end)
    run.fail(run.expect_turn("planner"), core.FailureReason.TIMED_OUT)
    assert (ended(run).outcome, ended(run).reason) == ("failure", "no-winner: timed-out")


def test_the_runtime_numbers_each_label(begin: Begin) -> None:
    async def twice(ctx: core.Flow, _config: Config) -> core.NoWinner:
        for _ in range(2):
            await ask(ctx, "planner")
        return core.NoWinner(reason="two")

    run = begin(twice)
    answer(run, "planner", 1)
    assert [a.decision.root for a in run.open] == ["worker.2"]
    answer(run, "planner", 2)
    assert ended(run).reason == "no-winner: two"
    assert run.finished is not None
    assert run.finished.decision_id.root == "finish.1"


def test_a_refused_result_ends_the_run_with_the_plain_failure_instead(begin: Begin) -> None:
    run = begin(plan_then_end)
    answer(run, "planner", 1)
    run.fail(run.expect(core.Finish), core.FailureReason.REFUSED)
    assert run.finished is not None
    assert run.finished.decision_id.root == "finish.2"
    assert ended(run).reason == "finish-refused: refused"


# -- groups, first, sources ---------------------------------------------------------------------


async def race(ctx: core.Flow, _config: Config) -> core.NoWinner:
    async with ctx.group() as tasks:
        slow = tasks.start("slow", worker, "slow")
        quick = tasks.start("quick", worker, "quick")
        gate = ctx.timer(100.0)
        won = await ctx.first(quick, slow)
        other = await ctx.first(slow, quick, gate)
    return core.NoWinner(reason=f"{won} then {other}")


def test_first_gives_the_next_value_of_whichever_source_is_ready(begin: Begin) -> None:
    run = begin(race)
    assert len(run.waiting()[0].sources) == 2
    answer(run, "slow", 1)
    assert len(run.waiting()[0].sources) == 3
    assert run.waiting()[0].sources[-1] == "timer@100"
    answer(run, "quick", 2)
    assert ended(run).reason == "no-winner: 1 then 2"  # slow answered first, then quick


def test_when_several_sources_are_ready_the_earliest_argument_wins(begin: Begin) -> None:
    async def tie(ctx: core.Flow, _config: Config) -> core.NoWinner:
        async with ctx.group() as tasks:
            a = tasks.start("a", worker, "a")
            b = tasks.start("b", worker, "b")
            await ctx.timer(10.0)  # both answers arrive while the flow waits for this
            first = await ctx.first(b, a)
            second = await ctx.first(b, a)
        return core.NoWinner(reason=f"{first} {second}")

    run = begin(tie)
    answer(run, "a", 1)
    answer(run, "b", 2)
    run.advance_to(10.0)
    assert ended(run).reason == "no-winner: 2 1"


def test_a_timer_fires_once_when_the_clock_reaches_it(begin: Begin) -> None:
    async def nap(ctx: core.Flow, _config: Config) -> core.NoWinner:
        await ctx.timer(30.0)
        return core.NoWinner(reason="woke")

    run = begin(nap)
    assert run.sleeping_until == (30.0,)
    run.advance_to(10.0)
    assert run.finished is None
    run.advance_to(31.0)
    assert ended(run).reason == "no-winner: woke"
    assert run.timeline() == "t=10 t=31"


def test_the_stop_is_a_source_that_yields_the_event_once(begin: Begin) -> None:
    async def watcher(ctx: core.Flow, _config: Config) -> core.NoWinner:
        async with ctx.group() as tasks:
            pending = tasks.start("w", worker)
            match await ctx.first(pending, ctx.stopped):
                case core.StopRequested(control=control):
                    assert ctx.stop_requested
                    return core.NoWinner(reason=f"stopped by {control.control_id.root}")
                case other:
                    raise AssertionError(other)

    run = begin(watcher)
    run.stop()
    assert (ended(run).outcome, ended(run).reason) == ("cancelled", "stopped: stopped by stop")


def test_other_control_actions_are_ignored(begin: Begin) -> None:
    run = begin(plan_then_end)
    run.send(
        core.StopRequested(
            control=core.ControlInput(
                control_id=core.ControlId(root="c"), action="pause", owner=core.PauseOwner.OPERATOR
            )
        )
    )
    assert waits(run) == ["planner"]


def test_a_flow_that_waits_for_nothing_that_can_arrive_is_a_deadlock(begin: Begin) -> None:
    async def twice(ctx: core.Flow, _config: Config) -> None:
        async with ctx.group() as tasks:
            pending = tasks.start("a", worker)
            await pending
            await pending  # its one value is gone

    run = begin(twice)
    with pytest.raises(core.Deadlock) as caught:
        answer(run, "work", 1)
    assert caught.value.flow == ""


def test_a_flow_may_await_only_ctx_operations(begin: Begin) -> None:
    class Foreign:
        def __await__(self) -> Generator[int]:
            yield 1

    async def rogue(_ctx: core.Flow, _config: Config) -> None:
        await Foreign()

    with pytest.raises(core.ContractError, match="only ctx operations"):
        begin(rogue)


# -- groups and tasks ---------------------------------------------------------------------------


async def fan_out(ctx: core.Flow, _config: Config) -> core.NoWinner:
    async with ctx.group(limit=2) as workers:
        assert workers.free == 2
        workers.start("a", worker)
        workers.start("b", worker)
        assert workers.free == 0
        results = [done async for done in workers]
    return core.NoWinner(reason=f"results {results}")


def test_a_group_yields_results_as_tasks_finish_and_task_ids_carry_their_key(begin: Begin) -> None:
    run = begin(fan_out)
    assert [a.decision.root for a in run.open] == ["a/worker.1", "b/worker.1"]
    first, second = run.open
    run.reply(second, Plan(n=20))
    run.reply(first, Plan(n=1))
    assert ended(run).reason == "no-winner: results [20, 1]"


def test_starting_a_task_in_a_full_group_is_a_bug(begin: Begin) -> None:
    async def greedy(ctx: core.Flow, _config: Config) -> None:
        async with ctx.group(limit=1) as one:
            one.start("a", worker)
            one.start("b", worker)

    with pytest.raises(core.ContractError, match="full"):
        begin(greedy)


def test_a_task_is_a_source_and_tasks_nest(begin: Begin) -> None:
    async def middle(ctx: core.Flow) -> int | core.TaskStopped:
        async with ctx.group() as inner:
            return await inner.start("b", worker)

    async def outer(ctx: core.Flow, _config: Config) -> core.NoWinner:
        async with ctx.group() as outers:
            got = await outers.start("a", middle)
        return core.NoWinner(reason=f"got {got}")

    run = begin(outer)
    assert [a.decision.root for a in run.open] == ["a/b/worker.1"]
    assert [w.flow for w in run.waiting()] == ["", "a/", "a/b/"]
    answer(run, "work", 7)
    assert ended(run).reason == "no-winner: got 7"


def test_leaving_a_group_waits_for_every_task(begin: Begin) -> None:
    async def leaves_early(ctx: core.Flow, _config: Config) -> core.NoWinner:
        async with ctx.group() as workers:
            workers.start("a", worker)
            workers.start("b", worker)
        return core.NoWinner(reason="all done")

    run = begin(leaves_early)
    answer(run, "work", 1)
    assert run.finished is None
    answer(run, "work", 2)
    assert ended(run).reason == "no-winner: all done"


# -- latches ------------------------------------------------------------------------------------

LEARNED = core.LatchKey[int]("learned")


async def learns_then_shares(ctx: core.Flow, _config: Config) -> core.NoWinner:
    """Two tasks wait for what the root learns; one asks its own turn first."""

    async def waiter(ctx: core.Flow, turns: int) -> int:
        own = sum([answer_of(await ask(ctx, "own")) for _ in range(turns)])
        return own + await ctx.latch(LEARNED)

    async with ctx.group() as waiters:
        waiters.start("a", waiter, 1)
        waiters.start("b", waiter, 0)
        ctx.latch(LEARNED).set(answer_of(await ask(ctx, "learn")))
        results = sorted([done async for done in waiters], key=str)
    return core.NoWinner(reason=f"results {results}")


def test_a_latch_gives_every_waiter_the_value_set_once_whenever_it_waits(begin: Begin) -> None:
    run = begin(learns_then_shares)
    assert sorted(waits(run)) == ["a/own", "learn"]
    answer(run, "learn", 5)
    assert run.finished is None  # b has the value; a still waits for its own turn
    answer(run, "own", 1)
    assert ended(run).reason == "no-winner: results [5, 6]"


def test_setting_a_latch_twice_is_a_bug(begin: Begin) -> None:
    async def twice(ctx: core.Flow, _config: Config) -> None:
        ctx.latch(LEARNED).set(1)
        ctx.latch(LEARNED).set(2)

    with pytest.raises(core.ContractError, match="set once"):
        begin(twice)


def test_two_latch_keys_with_one_name_are_refused_whatever_their_types(begin: Begin) -> None:
    """A second key under a name in use could hold another type: one cell would mix them."""
    clashing = core.LatchKey[str](LEARNED.name)

    async def mixes(ctx: core.Flow, _config: Config) -> None:
        ctx.latch(LEARNED).set(1)
        await ctx.latch(clashing)

    with pytest.raises(core.ContractError, match="another latch key has this name"):
        begin(mixes)


VERDICT = core.LatchKey[int]("verdict")
REPORTS = core.ChannelKey[str]("reports")


async def reports_then_hears(ctx: core.Flow, _config: Config) -> core.NoWinner:
    """Two tasks report to the root mid-flight; the root answers each on its own item latch."""

    async def reporter(ctx: core.Flow, name: str) -> int:
        ctx.channel(REPORTS).send(name)
        return await ctx.latch(VERDICT.of(name))

    heard: list[str] = []
    results: list[int] = []
    async with ctx.group() as reporters:
        reporters.start("a", reporter, "a")
        reporters.start("b", reporter, "b")
        while len(results) < 2:
            match await ctx.first(ctx.channel(REPORTS), reporters):
                case str() as name:
                    heard.append(name)
                    ctx.latch(VERDICT.of(name)).set(len(heard) * 10)
                case int() as result:
                    results.append(result)
                case other:
                    raise AssertionError(other)
    return core.NoWinner(reason=f"heard {heard} results {sorted(results)}")


def test_a_channel_carries_reports_to_the_root_in_order_and_item_latches_answer_each(
    begin: Begin,
) -> None:
    run = begin(reports_then_hears)
    assert ended(run).reason == "no-winner: heard ['a', 'b'] results [10, 20]"


def test_item_latches_of_one_declaration_are_separate_and_a_clashing_name_is_refused(
    begin: Begin,
) -> None:
    async def items(ctx: core.Flow, _config: Config) -> core.NoWinner:
        ctx.latch(VERDICT.of("x")).set(1)
        ctx.latch(VERDICT.of("y")).set(2)
        found = (await ctx.latch(VERDICT.of("x")), await ctx.latch(VERDICT.of("y")))
        return core.NoWinner(reason=f"items {found}")

    assert ended(begin(items)).reason == "no-winner: items (1, 2)"

    clashing = core.LatchKey[int]("verdict/x")

    async def clashes(ctx: core.Flow, _config: Config) -> None:
        ctx.latch(VERDICT.of("x")).set(1)
        await ctx.latch(clashing)

    with pytest.raises(core.ContractError, match="another latch key has this name"):
        begin(clashes)


def test_two_channel_keys_with_one_name_are_refused(begin: Begin) -> None:
    clashing = core.ChannelKey[int](REPORTS.name)

    async def mixes(ctx: core.Flow, _config: Config) -> None:
        ctx.channel(REPORTS).send("a")
        ctx.channel(clashing).send(1)

    with pytest.raises(core.ContractError, match="another channel key has this name"):
        begin(mixes)


def test_a_second_flow_that_reads_a_channel_is_refused(begin: Begin) -> None:
    """Two readers would split the channel's values between them by scheduling order."""

    async def listener(ctx: core.Flow, _name: str) -> str:
        return await ctx.channel(REPORTS)

    async def two_readers(ctx: core.Flow, _config: Config) -> None:
        async with ctx.group() as listeners:
            listeners.start("a", listener, "a")
            await ctx.first(ctx.channel(REPORTS), listeners)

    with pytest.raises(core.ContractError, match="a channel has one reader"):
        begin(two_readers)


def test_a_flow_that_waits_only_on_a_channel_nobody_can_send_to_is_a_deadlock(
    begin: Begin,
) -> None:
    async def alone(ctx: core.Flow, _config: Config) -> None:
        async with ctx.group() as workers:
            await workers.start("a", worker)
        await ctx.channel(REPORTS)  # every other flow has ended: no value can come

    run = begin(alone)
    with pytest.raises(core.Deadlock) as caught:
        answer(run, "work", 1)
    assert caught.value.flow == ""
    assert caught.value.sources == ("channel reports",)


# -- stops, resources ---------------------------------------------------------------------------


@core.resource
async def attempt(ctx: core.Flow, name: str) -> AsyncGenerator[str]:
    await ask(ctx, "open")
    try:
        yield name
    except core.Stopped:
        await ask(ctx, "settle-stopped")
    else:
        await ask(ctx, "settle")


async def workstream(ctx: core.Flow, name: str) -> str:
    async with attempt(ctx, name):
        await ask(ctx, "work")
    return f"{name} done"


async def stop_aware(ctx: core.Flow, _config: Config) -> core.NoWinner:
    seen: list[str] = []
    async with ctx.group() as streams, ctx.group(stops=False) as planning:
        streams.start("a", workstream, "a")
        streams.start("b", workstream, "b")
        planning.start("p", worker, "plan")
        while streams.running + planning.running:
            match await ctx.first(planning, streams, ctx.stopped):
                case core.StopRequested():
                    seen.append("stop")
                case core.TaskStopped(key=key):
                    seen.append(f"{key} stopped")
                case int() | str() as done:
                    seen.append(str(done))
    return core.NoWinner(reason=" | ".join(seen))


def test_a_stop_reaches_tasks_of_stopping_groups_and_their_resources_settle(begin: Begin) -> None:
    run = begin(stop_aware)
    for asked in list(run.open):
        if asked.prompt is not None and asked.prompt.text == "open":
            run.reply(asked, Plan(n=0))
    assert waits(run) == ["p/plan", "a/work", "b/work"]
    run.stop()
    # the workstreams' resources settled as stopped; the planner (stops=False) was left alone
    assert waits(run)[-2:] == ["a/settle-stopped", "b/settle-stopped"]
    answer(run, "settle-stopped")
    answer(run, "settle-stopped")
    answer(run, "plan", 5)
    assert ended(run).reason == "stopped: stop | a done | b done | 5"


def test_setup_and_teardown_are_shielded_and_the_stop_comes_at_the_first_await_after(
    begin: Begin,
) -> None:
    run = begin(stop_aware)
    run.stop()  # while both workstreams are still opening
    assert waits(run) == ["a/open", "b/open", "p/plan"]
    answer(run, "open")
    # a's body is stopped before it asks for work; its teardown settles it as stopped
    assert "a/work" not in waits(run)
    assert waits(run)[-1] == "a/settle-stopped"


def test_a_stop_ends_a_task_that_lets_it_escape_as_task_stopped(begin: Begin) -> None:
    async def parent(ctx: core.Flow, _config: Config) -> core.NoWinner:
        async with ctx.group() as workers:
            workers.start("w", worker)
            done = await ctx.first(workers)
        return core.NoWinner(reason=f"{done!r}")

    run = begin(parent)
    run.stop()
    assert ended(run).reason == "stopped: TaskStopped(key='w')"


@pytest.mark.parametrize("body", ["turn", "first"])
def test_a_pending_stop_is_raised_before_anything_is_issued(begin: Begin, body: str) -> None:
    """A stop that came during a shielded setup is raised by the body's first turn or ``first``."""

    async def stoppable(ctx: core.Flow) -> str:
        async with attempt(ctx, "x"):
            if body == "turn":
                await ask(ctx, "after")
            else:
                await ctx.first(ctx.timer(99.0))
        return "settled"  # the resource handled the stop: the task goes on to return

    async def parent(ctx: core.Flow, _config: Config) -> core.NoWinner:
        async with ctx.group() as workers:
            workers.start("s", stoppable)
            done = await ctx.first(workers)
        return core.NoWinner(reason=f"{done!r}")

    run = begin(parent)
    run.stop()
    answer(run, "open")
    assert "s/after" not in waits(run)  # the pending stop is raised before it is issued
    assert waits(run) == ["s/settle-stopped"]
    answer(run, "settle-stopped")
    assert ended(run).reason == "stopped: 'settled'"


def test_a_stopped_task_never_acquires_a_resource(begin: Begin) -> None:
    async def uses_resource(ctx: core.Flow) -> str:
        async with attempt(ctx, "late"):
            await ask(ctx, "body")
        return "unreachable"

    async def parent(ctx: core.Flow, _config: Config) -> core.NoWinner:
        async with ctx.group() as workers:
            await ctx.stopped
            workers.start("late", uses_resource)
            done = await ctx.first(workers)
        return core.NoWinner(reason=f"{done!r}")

    run = begin(parent)
    run.stop()
    assert ended(run).reason == "stopped: TaskStopped(key='late')"
    assert waits(run) == []  # nothing acquired or released


def test_a_task_started_in_a_stopping_group_is_stopped_at_its_first_await(begin: Begin) -> None:
    async def parent(ctx: core.Flow, _config: Config) -> core.NoWinner:
        async with ctx.group() as workers:
            await ctx.stopped
            workers.start("late", worker)
            done = await ctx.first(workers)
        return core.NoWinner(reason=f"{done!r}")

    run = begin(parent)
    run.stop()
    assert ended(run).reason == "stopped: TaskStopped(key='late')"
    assert "late/work" not in waits(run)  # stopped before its first request is issued


def test_a_late_result_of_an_abandoned_wait_is_dropped_and_recorded(begin: Begin) -> None:
    async def parent(ctx: core.Flow, _config: Config) -> core.NoWinner:
        async with ctx.group() as workers:
            workers.start("w", worker)
            await ctx.first(workers)
        return core.NoWinner(reason="over")

    run = begin(parent)
    abandoned = run.expect_turn("work")
    run.stop()
    before = len(run.decisions)
    run.reply(abandoned, Plan(n=9))
    assert len(run.decisions) == before
    assert [d.decision for d in run.state.dropped] == [abandoned.decision]
    assert [d.event for d in run.state.dropped] == [core.EventKind.TURN_ENDED]


# -- retry --------------------------------------------------------------------------------------


POLICY = core.RetryPolicy(max_tries=3, backoff=core.Backoff(first=5.0, cap=20.0))


async def retrying(ctx: core.Flow, _config: Config) -> core.NoWinner:
    async def attempt_once(tries: core.Tries[str]) -> int | core.Again[str]:
        n = answer_of(await ask(ctx, "try"))
        return n if n > 0 else core.Again(reason=f"zero on try {tries.number}")

    match await ctx.retry(attempt_once, POLICY):
        case int() as value:
            return core.NoWinner(reason=f"got {value}")
        case core.GaveUp(reasons=reasons):
            return core.NoWinner(reason=" / ".join(reasons))


def test_retry_repeats_an_attempt_with_a_backoff_on_the_run_clock_until_it_gives_a_value(
    begin: Begin,
) -> None:
    run = begin(retrying, FakeView())
    answer(run, "try", 0)
    assert run.sleeping_until == (5.0,)  # the first backoff: Backoff.first
    run.advance_to(5.0)
    answer(run, "try", 7)
    assert ended(run).reason == "no-winner: got 7"


def test_retry_gives_up_after_the_policys_tries_and_reports_every_reason(begin: Begin) -> None:
    run = begin(retrying, FakeView())
    answer(run, "try", 0)
    run.advance_to(5.0)
    answer(run, "try", 0)
    assert run.sleeping_until == (15.0,)  # doubled, from the clock at that moment
    run.advance_to(15.0)
    answer(run, "try", 0)
    assert run.sleeping_until == ()  # no wait after the last try
    assert ended(run).reason == "no-winner: zero on try 1 / zero on try 2 / zero on try 3"


# -- the clock and publishing -------------------------------------------------------------------


class Progress(core.Value):
    """What the toy flow publishes."""

    replies: int


def test_the_clock_is_read_once_and_journaled(begin: Begin) -> None:
    async def timed(ctx: core.Flow, _config: Config) -> core.NoWinner:
        await ask(ctx, "a")
        first = ctx.clock.now()
        await ask(ctx, "b")
        return core.NoWinner(reason=f"read {first} now {ctx.clock.now()}")

    view = FakeView(now_at=5.0)
    run = begin(timed, view)
    answer(run, "a", 1)
    view.now_at = 99.0
    answer(run, "b", 2)
    # a replay takes the journaled first reading, not the view as it is now
    assert ended(run).reason == "no-winner: read 5.0 now 99.0"


def test_a_published_record_is_readable_from_the_state(begin: Begin) -> None:
    async def reporting(ctx: core.Flow, _config: Config) -> None:
        ctx.publish(Progress(replies=0))
        await ask(ctx, "a")
        ctx.publish(Progress(replies=1))
        await ask(ctx, "b")

    run = begin(reporting)
    assert run.driver.published(run.state, Progress) == Progress(replies=0)
    answer(run, "a", 1)
    assert run.driver.published(run.state, Progress) == Progress(replies=1)


# -- routing ------------------------------------------------------------------------------------


def test_a_close_answers_the_withdraw_of_its_attempt_though_core_names_the_opening_decision(
    begin: Begin,
) -> None:
    async def uses_workspace(ctx: core.Flow, _config: Config) -> core.NoWinner:
        async with open_workspace(ctx, "at") as workspace:
            assert isinstance(workspace, core.Workspace)
        return core.NoWinner(reason="closed")

    run = begin(uses_workspace)
    opening = run.expect(core.StartAttempt)
    ref = run.open_workspace(opening)
    closing = run.expect(core.Withdraw)
    assert closing.decision != opening.decision
    settlement = core.Settlement(
        settlement_id=core.SettlementId(root="s"),
        attempt=ref,
        candidate=None,
        assessments=(),
        eligible=False,
        retention="discard",
        outcome="succeeded",
    )
    # core names the decision that opened the attempt, not the one that closes it
    run.send(core.WorkspaceClosed(decision=opening.decision, settlement=settlement))
    assert ended(run).reason == "no-winner: closed"
    assert run.state.dropped == ()


def test_an_adoption_result_without_a_decision_answers_the_propose_winner(begin: Begin) -> None:
    async def nothing_chosen(_ctx: core.Flow, _config: Config) -> None:
        return None

    run = begin(nothing_chosen, adopting=False)
    proposal = run.expect(core.ProposeWinner)
    selection = core.TrustedBaseline(revision=FACTS.baseline)
    state = core.initial_state()
    observation = core.Observation(
        event_id=core.EventId(root="event"),
        request_id=core.RequestId(root="request"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.FAILED,
        terminal=True,
        released=True,
    )
    run.send(
        core.AdoptionFinished(
            decision=None,
            result=core.AdoptionFailed(
                selection=selection,
                reason=core.AdoptionFailureReason.ADOPT_FAILED,
                rounds=1,
                observation=observation,
            ),
        )
    )
    assert proposal.decision.root == "adopt.1"
    assert (ended(run).outcome, ended(run).reason) == ("failure", "adoption-failed: adopt-failed")
    assert run.state.dropped == ()
