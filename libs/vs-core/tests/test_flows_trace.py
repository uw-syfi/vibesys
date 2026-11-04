"""The run trace: a span per flow task and awaited operation, recorded where the runtime journals.

Every story runs live and with a save-and-restart after every step, and both must record the same
spans, ids and times. ``test_flows_properties.py`` sweeps generated programs for the trace's
shape; here each property names one fact and where it comes from: the run clock of the step that
recorded it, ``ctx.retry``'s tries, the stop, what a flow waits on, how a span ended, and a code
change that restarts the flows from their checkpoints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import FlowRun, fake_search
from vs_sim.api.testing import examples

from .flow_support import (
    LENIENT,
    ROLES,
    Config,
    Plan,
    answer_of,
    ask,
    assert_trace_ended,
    assert_trace_well_formed,
    start,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from vs_core.api import SearchDef

RESTARTING = pytest.mark.parametrize("restarting", [False, True], ids=["live", "restarting"])
_GAPS = st.lists(st.integers(0, 5).map(float), min_size=1, max_size=5)


def spans_of(run: FlowRun[Config], kind: core.SpanType) -> list[core.Span]:
    """The run's spans of one type, in the order they started."""
    return [span for span in run.trace().spans if span.type is kind]


def span_of(run: FlowRun[Config], root: str) -> core.Span:
    """The span ``root``; it must exist."""
    found = run.trace().span(root)
    assert found is not None, f"no span {root}: {[s.id for s in run.trace().spans]}"
    return found


def reply_later(run: FlowRun[Config], gap: float, text: str, n: int = 1) -> float:
    """Move the clock on by ``gap`` and answer the turn ``text``; the time it was answered."""
    at = run.view.now_at + gap
    run.advance_to(at)
    run.reply(run.expect_turn(text), Plan(n=n))
    return at


@RESTARTING
@settings(max_examples=examples(pr=4, nightly=40), deadline=None)
@given(gaps=_GAPS)
def test_spans_are_timed_by_the_run_clock_of_the_step_that_recorded_them(
    *, restarting: bool, gaps: list[float]
) -> None:
    async def root(ctx: core.Flow, _config: Config) -> None:
        for number in range(len(gaps)):
            await ask(ctx, f"a{number}")

    run = start(root, restarting=restarting)
    issued, answered = [0.0], []
    for number, gap in enumerate(gaps):
        answered.append(reply_later(run, gap, f"a{number}"))
        issued.append(answered[-1])
        assert_trace_well_formed(run.trace(), run.view.now_at)
    turns = [s for s in spans_of(run, core.SpanType.OPERATION) if s.name == "worker"]
    assert [s.started_at for s in turns] == issued[: len(gaps)]
    assert [s.ended_at for s in turns] == answered
    assert all(s.status is core.SpanStatus.DONE for s in turns)
    assert len({s.invocation for s in turns if s.invocation}) == len(turns)  # each its own
    assert run.finished is not None
    assert_trace_ended(run.trace())
    assert span_of(run, "flow:#1").ended_at == answered[-1]


@RESTARTING
@settings(max_examples=examples(pr=4, nightly=40), deadline=None)
@given(answers=st.lists(st.integers(0, 1), min_size=1, max_size=4), tries=st.integers(1, 3))
def test_each_try_of_a_retry_is_an_attempt_of_the_flow_span(
    *, restarting: bool, answers: list[int], tries: int
) -> None:
    async def attempt(ctx: core.Flow, t: core.Tries[str]) -> int | core.Again[str]:
        n = answer_of(await ask(ctx, "t"))
        return n if n > 0 else core.Again(reason=f"zero at {t.number}")

    async def root(ctx: core.Flow, _config: Config) -> None:
        await ctx.retry(lambda t: attempt(ctx, t), core.RetryPolicy(max_tries=tries))

    run = start(root, restarting=restarting)
    played: list[int] = []
    for answer in answers:
        if run.finished is not None:
            break
        reply_later(run, 1.0, "t", answer)
        played.append(answer)
    attempts = span_of(run, "flow:#1").attempts
    assert [a.n for a in attempts] == list(range(1, len(attempts) + 1))
    assert all(a.of == tries for a in attempts)
    assert [a.started_at for a in attempts] == [float(n) for n in range(len(attempts))]
    expected = [
        core.AttemptOutcome.DONE
        if answer
        else core.AttemptOutcome.GAVE_UP
        if n == tries
        else core.AttemptOutcome.AGAIN
        for n, answer in enumerate(played, start=1)
    ]
    assert [a.outcome for a in attempts if a.outcome is not None] == expected
    assert [a.reason for a in attempts if a.outcome is core.AttemptOutcome.AGAIN] == [
        f"zero at {n}" for n, answer in enumerate(played, start=1) if not answer and n < tries
    ]
    assert_trace_well_formed(run.trace(), run.view.now_at)


@RESTARTING
@settings(max_examples=examples(pr=4, nightly=40), deadline=None)
@given(stops=st.booleans(), at=st.integers(1, 9).map(float))
def test_the_stop_is_requested_of_every_live_flow_and_lands_where_it_interrupts(
    *, restarting: bool, stops: bool, at: float
) -> None:
    async def root(ctx: core.Flow, _config: Config) -> None:
        async with ctx.group(stops=stops) as tasks:
            tasks.start("w", ask, "inner")
            async for _ in tasks:
                pass
        await ctx.stopped

    run = start(root, restarting=restarting)
    run.advance_to(at)
    run.stop()
    task = span_of(run, "flow:w/#1")
    assert task.stop == core.SpanStop(requested_at=at, landed=stops)
    assert span_of(run, "flow:#1").stop == core.SpanStop(requested_at=at, landed=False)
    if stops:
        assert task.status is core.SpanStatus.STOPPED
        assert task.failure == core.SpanFailure(
            kind=core.FailureKind.INTERRUPTION, message="stopped"
        )
        turn = span_of(run, "op:w/worker.1")
        assert (turn.status, turn.ended_at) == (core.SpanStatus.STOPPED, at)
    else:
        assert task.status is core.SpanStatus.RUNNING
        run.reply(run.expect_turn("inner"), Plan(n=1))
    assert run.finished is not None
    assert_trace_ended(run.trace())


LEARNED = core.LatchKey[int]("learned")


@RESTARTING
def test_a_flow_waits_on_its_sources_as_data(*, restarting: bool) -> None:
    async def learner(ctx: core.Flow) -> None:
        ctx.latch(LEARNED).set(answer_of(await ask(ctx, "learn")))

    async def waiter(ctx: core.Flow) -> int:
        return await ctx.latch(LEARNED)

    async def root(ctx: core.Flow, _config: Config) -> None:
        async with ctx.group() as tasks:
            waiting = tasks.start("wait", waiter)
            tasks.start("learn", learner)
            tasks.start("slow", ask, "slow")
            await ctx.first(waiting, ctx.timer(5.0), ctx.stopped)
            async for _ in tasks:
                pass

    run = start(root, restarting=restarting)
    waiter_span, learner_span = span_of(run, "flow:wait/#1"), span_of(run, "flow:learn/#1")
    assert waiter_span.status is core.SpanStatus.WAITING
    assert waiter_span.waiting_on == (
        core.WaitingOn(kind=core.SpanWaitKind.SLOT, ref="latch learned", since=0.0),
    )
    assert learner_span.status is core.SpanStatus.RUNNING
    assert [(w.kind, w.ref) for w in learner_span.waiting_on] == [
        (core.SpanWaitKind.OPERATION, "op:learn/worker.1")
    ]
    root_span = span_of(run, "flow:#1")
    assert root_span.status is core.SpanStatus.RUNNING
    assert [(w.kind, w.ref) for w in root_span.waiting_on] == [
        (core.SpanWaitKind.SLOT, "flow:wait/#1"),
        (core.SpanWaitKind.TIME, "timer@5"),
        (core.SpanWaitKind.STOP, "stop"),
    ]
    run.advance_to(2.0)
    assert span_of(run, "flow:wait/#1").waiting_on[0].since == 0.0  # the same wait goes on
    run.reply(run.expect_turn("learn"), Plan(n=4))
    assert span_of(run, "flow:wait/#1").status is core.SpanStatus.DONE
    assert span_of(run, "flow:#1").waiting_on == (
        core.WaitingOn(kind=core.SpanWaitKind.SLOT, ref="group", since=2.0),
    )
    run.reply(run.expect_turn("slow"), Plan(n=1))
    assert run.finished is not None
    assert_trace_ended(run.trace())


@RESTARTING
@settings(max_examples=examples(pr=4, nightly=40), deadline=None)
@given(reason=st.sampled_from([core.FailureReason.TIMED_OUT, core.FailureReason.LOST]))
def test_a_failure_is_recorded_as_the_outcome_it_is(
    *, restarting: bool, reason: core.FailureReason
) -> None:
    async def doomed(ctx: core.Flow) -> core.GaveUp[str]:
        await ask(ctx, "doomed", LENIENT)
        return core.GaveUp(reasons=("a", "b"))

    async def root(ctx: core.Flow, _config: Config) -> core.NoWinner:
        async with ctx.group() as tasks:
            tasks.start("d", doomed)
            async for _ in tasks:
                pass
        return core.NoWinner(reason="nothing")

    run = start(root, restarting=restarting)
    run.fail(run.expect_turn("doomed"), reason)
    turn = span_of(run, "op:d/lenient.1")
    assert (turn.status, turn.failure) == (
        core.SpanStatus.FAILED,
        core.SpanFailure(kind=core.FailureKind.OUTCOME, message=reason.value),
    )
    task = span_of(run, "flow:d/#1")
    assert task.status is core.SpanStatus.FAILED
    assert task.failure == core.SpanFailure(
        kind=core.FailureKind.OUTCOME, message="gave up after 2 tries"
    )
    root_span = span_of(run, "flow:#1")
    assert root_span.status is core.SpanStatus.FAILED
    assert run.finished is not None
    assert root_span.failure == core.SpanFailure(
        kind=core.FailureKind.OUTCOME, message=run.finished.result.reason
    )
    assert_trace_ended(run.trace())


class Job(core.Value):
    """What the worker flow restarts from."""

    step: int = 0


@core.flow(snapshot=Job, name="worker")
async def worker_v1(ctx: core.Flow, job: Job) -> int:
    ctx.checkpoint(job)
    await ask(ctx, "one")
    ctx.checkpoint(Job(step=1))
    await ask(ctx, "two")
    return 2


@core.flow(snapshot=Job, name="worker")
async def worker_v2(ctx: core.Flow, job: Job) -> int:
    if job.step < 1:
        await ask(ctx, "uno")
        ctx.checkpoint(Job(step=1))
    await ask(ctx, "two")
    return 3


async def helper(ctx: core.Flow) -> None:
    await ask(ctx, "help")


def search_for(
    worker: Callable[[core.Flow, Job], Coroutine[Any, Any, int]],
) -> SearchDef[Config]:
    @fake_search("flows", Config, ROLES)
    async def root(ctx: core.Flow, config: Config) -> None:
        ctx.checkpoint(config)
        async with ctx.group() as workers:
            workers.start("w", worker, Job())
            workers.start("h", helper)
            async for _ in workers:
                pass

    return root


@settings(max_examples=examples(pr=4, nightly=40), deadline=None)
@given(
    checkpointed=st.booleans(),
    swap_at=st.integers(1, 9).map(float),
    replay_after=st.booleans(),
)
def test_a_code_change_keeps_the_spans_and_resumes_the_flows_in_theirs(
    *, checkpointed: bool, swap_at: float, replay_after: bool
) -> None:
    run = FlowRun.start(search_for(worker_v1), Config())
    if checkpointed:
        run.reply(run.expect_turn("one"), Plan(n=1))
    in_flight = run.expect_turn("two" if checkpointed else "one")
    before = run.trace()
    run.advance_to(swap_at)
    run.swap(search_for(worker_v2))
    run.restarting = replay_after
    run.reply(in_flight, Plan(n=1))  # the first event the new code sees

    after = run.trace()
    assert [s.id for s in after.spans[: len(before.spans)]] == [s.id for s in before.spans]
    worker = span_of(run, "flow:w/#1")
    assert worker.status is core.SpanStatus.RUNNING
    assert worker.started_at == 0.0  # the flow resumed in its own span
    helper_span = span_of(run, "flow:h/#1")
    assert helper_span.status is core.SpanStatus.STOPPED  # not a registered flow: dropped
    assert helper_span.failure is not None
    assert helper_span.failure.kind is core.FailureKind.INTERRUPTION
    assert span_of(run, "flow:h/#2").status is core.SpanStatus.RUNNING  # started afresh
    dropped = span_of(run, f"op:{in_flight.decision.root}")
    assert (dropped.status, dropped.ended_at) == (core.SpanStatus.STOPPED, swap_at)
    assert_trace_well_formed(after, run.view.now_at)

    for _ in range(4):  # the newest turns: "uno" (from step 0), "two", the new "help"
        if run.finished is None:
            run.reply(run.open[-1], Plan(n=1))
    assert run.finished is not None
    assert span_of(run, "flow:w/#1").status is core.SpanStatus.DONE
    assert_trace_ended(run.trace())
