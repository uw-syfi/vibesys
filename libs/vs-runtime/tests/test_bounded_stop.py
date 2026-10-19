"""A stop is bounded: new work is refused at once and the run ends after a grace period."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import StopMode
from vs_runtime.api import BenchmarkEvaluation, Evaluation
from vs_runtime.api.infrastructure import (
    RunStopped,
    StopGraceError,
    bounded_stop,
    create_run_control_channel,
    stop_gated_evaluation,
)
from vs_runtime.api.testing import (
    FakeEvaluation,
    FakeRunControlEventSink,
    FakeStopTimer,
    FakeWorkspace,
)
from vs_sim.api import DISCARD, yield_now
from vs_sim.api.testing import SimThreads, arrival, yield_until

_GRACE_S = 60.0


async def _until_armed(timer: FakeStopTimer) -> None:
    async def delays() -> list[float]:
        return timer.delays

    await yield_until(delays, bool, "the grace wait to arm")


class _StopWork:
    """Record each on_stop call, optionally failing it."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.calls = 0
        self._failure = failure

    async def __call__(self) -> None:
        self.calls += 1
        if self._failure is not None:
            raise self._failure


@pytest.mark.asyncio
async def test_a_stop_that_outlives_the_grace_cancels_the_block_as_a_landed_stop() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    timer = FakeStopTimer(threads=SimThreads())
    on_stop = _StopWork()
    turn_cancelled = asyncio.Event()

    async def run() -> None:
        async with bounded_stop(channel, grace_s=_GRACE_S, on_stop=on_stop, timer=timer):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                turn_cancelled.set()
                raise

    task = asyncio.create_task(run())
    await yield_now()
    channel.request_stop(StopMode.DRAIN)
    await _until_armed(timer)
    assert on_stop.calls == 1
    assert not task.done()
    timer.expire()

    with pytest.raises(RunStopped):
        await task
    assert turn_cancelled.is_set()
    assert timer.delays == [_GRACE_S]
    assert task.cancelling() == 0


@pytest.mark.xfail(strict=True, reason="PR #3225 G2: fixed in the next commit")
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_stop_that_outlives_the_grace_says_so() -> None:
    """PR #3225 G2: a forced stop raised a bare ``RunStopped``, so the run's result had no reason."""
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    timer = FakeStopTimer(threads=SimThreads())

    async def run() -> None:
        async with bounded_stop(channel, grace_s=_GRACE_S, on_stop=_StopWork(), timer=timer):
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await yield_now()
    channel.request_stop(StopMode.DRAIN)
    await _until_armed(timer)
    timer.expire()

    with pytest.raises(RunStopped) as stopped:
        await task
    assert str(stopped.value) == (
        "the stop outlived its 60 s grace, so the work still running was cancelled"
    )


@pytest.mark.asyncio
async def test_a_block_that_ends_within_the_grace_is_not_cancelled() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    timer = FakeStopTimer(threads=SimThreads())
    finish = asyncio.Event()

    async def run() -> str:
        async with bounded_stop(channel, grace_s=_GRACE_S, on_stop=_StopWork(), timer=timer):
            await finish.wait()
        return "drained"

    task = asyncio.create_task(run())
    await yield_now()
    channel.request_stop(StopMode.DRAIN)
    await _until_armed(timer)
    finish.set()

    assert await task == "drained"


@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_resume_within_the_grace_does_not_disarm_the_cancellation() -> None:
    """PR #3209 MED-3: a resume cleared the stop, so a hung turn held the closing run forever."""
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    timer = FakeStopTimer(threads=SimThreads())

    async def run() -> None:
        async with bounded_stop(channel, grace_s=_GRACE_S, on_stop=_StopWork(), timer=timer):
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await yield_now()
    channel.request_stop(StopMode.DRAIN)
    await _until_armed(timer)
    assert not channel.resume()
    timer.expire()

    with pytest.raises(RunStopped):
        await task


@pytest.mark.asyncio
async def test_a_stop_pending_at_entry_is_bounded_and_a_cleanup_failure_is_kept() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    channel.request_stop(StopMode.DRAIN)
    timer = FakeStopTimer(threads=SimThreads())
    on_stop = _StopWork(RuntimeError("scancel failed"))

    async def run() -> None:
        async with bounded_stop(channel, grace_s=_GRACE_S, on_stop=on_stop, timer=timer):
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await _until_armed(timer)
    timer.expire()

    with pytest.raises(RunStopped) as stopped:
        await task
    assert on_stop.calls == 1
    assert any("scancel failed" in note for note in stopped.value.__notes__)


@pytest.mark.asyncio
async def test_a_cleanup_failure_is_raised_when_the_block_ends_on_its_own() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    timer = FakeStopTimer(threads=SimThreads())

    async def run() -> None:
        async with bounded_stop(
            channel,
            grace_s=_GRACE_S,
            on_stop=_StopWork(RuntimeError("scancel failed")),
            timer=timer,
        ):
            channel.request_stop(StopMode.DRAIN)
            await _until_armed(timer)

    with pytest.raises(RuntimeError, match="scancel failed"):
        await asyncio.create_task(run())


@pytest.mark.asyncio
async def test_an_outside_cancellation_stays_a_cancellation() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())

    async def run() -> None:
        async with bounded_stop(
            channel,
            grace_s=_GRACE_S,
            on_stop=_StopWork(),
            timer=FakeStopTimer(threads=SimThreads()),
        ):
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await yield_now()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@given(st.one_of(st.floats(max_value=0.0), st.just(math.inf), st.just(math.nan)))
def test_a_grace_that_is_not_positive_and_finite_is_rejected(grace_s: float) -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())

    async def enter() -> None:
        async with bounded_stop(
            channel, grace_s=grace_s, on_stop=_StopWork(), timer=FakeStopTimer(threads=SimThreads())
        ):
            pass

    with pytest.raises(StopGraceError, match="positive finite"):
        asyncio.run(enter())


def test_stop_listeners_see_each_request_until_unsubscribed() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    seen: list[bool] = []
    unsubscribe = channel.on_stop_requested(lambda: seen.append(channel.stop_requested()))

    assert not channel.stop_requested()
    channel.request_stop(StopMode.DRAIN)
    unsubscribe()
    unsubscribe()
    channel.request_stop(StopMode.CANCEL)

    assert seen == [True]
    assert channel.stop_requested()


@pytest.mark.asyncio
async def test_gated_evaluation_refuses_new_work_after_a_stop() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    inner = FakeEvaluation()
    evaluation = stop_gated_evaluation(inner, channel)
    workspace = FakeWorkspace(path=Path("/workspace"), workspace_id="candidate")
    await evaluation.accuracy(workspace, output=DISCARD)

    channel.request_stop(StopMode.DRAIN)
    with pytest.raises(RunStopped):
        await evaluation.accuracy(workspace, output=DISCARD)
    with pytest.raises(RunStopped):
        await evaluation.benchmark(workspace, output=DISCARD)
    with pytest.raises(RunStopped):
        await evaluation.validate_local(
            workspace, recipe_artifact="recipe.sh", report_location="report.md"
        )

    assert len(inner.accuracy_calls) == 1
    assert inner.benchmark_calls == []
    assert inner.local_validation_calls == []


@pytest.mark.asyncio
async def test_gated_evaluation_cancels_a_running_evaluation_at_the_stop() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    inner = FakeEvaluation()
    gate = inner.gate("benchmark", 0)
    evaluation = stop_gated_evaluation(inner, channel)
    workspace = FakeWorkspace(path=Path("/workspace"), workspace_id="candidate")

    running = asyncio.create_task(evaluation.benchmark(workspace, output=DISCARD))
    await arrival(gate.entered.wait(), running)
    channel.request_stop(StopMode.DRAIN)

    with pytest.raises(RunStopped):
        await running
    assert gate.cancelled_while_live
    assert not gate.released


class _CancelledBenchmark(FakeEvaluation):
    """An evaluation whose benchmark is cancelled from inside, with no stop requested."""

    async def benchmark(self, workspace: object, **kwargs: object) -> BenchmarkEvaluation:
        del workspace, kwargs
        raise asyncio.CancelledError


@pytest.mark.xfail(strict=True, reason="PR #3225 G2: fixed in the next commit")
@pytest.mark.regression
@pytest.mark.asyncio
async def test_an_evaluation_cancelled_without_a_stop_lands_naming_why() -> None:
    """PR #3225 G2: the gate's fallback raised a bare ``RunStopped``, told as a stop on request."""
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    evaluation = stop_gated_evaluation(_CancelledBenchmark(), channel)
    workspace = FakeWorkspace(path=Path("/workspace"), workspace_id="candidate")

    with pytest.raises(RunStopped) as stopped:
        await evaluation.benchmark(workspace, output=DISCARD)
    assert not channel.stop_requested()
    assert str(stopped.value) == "an evaluation was cancelled before it finished"


@pytest.mark.asyncio
async def test_gated_evaluation_returns_a_result_that_finishes_before_any_stop() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    inner = FakeEvaluation()
    evaluation = stop_gated_evaluation(inner, channel)
    workspace = FakeWorkspace(path=Path("/workspace"), workspace_id="candidate")

    result = await evaluation.benchmark(workspace, output=DISCARD)

    assert result == inner.default_benchmark


def _evaluation_members() -> list[str]:
    return sorted(
        name
        for name, value in vars(Evaluation).items()
        if not name.startswith("_") and callable(value)
    )


@pytest.mark.parametrize("member", _evaluation_members())
def test_gated_evaluation_offers_every_evaluation_member(member: str) -> None:
    """A member added to Evaluation later must reach the gated wrapper too (#1227 queue)."""
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    evaluation = stop_gated_evaluation(FakeEvaluation(), channel)

    assert callable(getattr(evaluation, member, None)), member


@pytest.mark.asyncio
async def test_a_block_that_ends_during_on_stop_does_not_abort_the_stop_cleanup() -> None:
    """on_stop cancels external work (evaluations, Slurm jobs); the block ending must not abort it.

    Chaos seed 11: a stop cancels an evaluation whose sbatch is in flight; the run
    body then leaves with RunStopped, bounded_stop cancels its watcher, and that
    cancellation reaches the executor's submission, so the job's identity is lost.
    """
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    timer = FakeStopTimer(threads=SimThreads())
    cleanup_started = asyncio.Event()
    external_work_settled = asyncio.Event()
    outcome: list[str] = []

    async def on_stop() -> None:
        cleanup_started.set()
        try:
            await external_work_settled.wait()
        except asyncio.CancelledError:
            outcome.append("aborted")
            raise
        outcome.append("finished")

    async def run() -> None:
        async with bounded_stop(channel, grace_s=_GRACE_S, on_stop=on_stop, timer=timer):
            try:
                await cleanup_started.wait()
            finally:
                # The external work settles as the block unwinds.
                external_work_settled.set()

    task = asyncio.create_task(run())
    await yield_now()
    channel.request_stop(StopMode.DRAIN)
    await task

    assert outcome == ["finished"]


@pytest.mark.asyncio
async def test_a_stop_cleanup_that_fails_after_the_block_ended_is_still_raised() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    cleanup_started = asyncio.Event()
    block_ended = asyncio.Event()

    async def on_stop() -> None:
        cleanup_started.set()
        await block_ended.wait()
        message = "cancel failed"
        raise RuntimeError(message)

    async def run() -> None:
        async with bounded_stop(
            channel, grace_s=_GRACE_S, on_stop=on_stop, timer=FakeStopTimer(threads=SimThreads())
        ):
            await cleanup_started.wait()
            block_ended.set()

    task = asyncio.create_task(run())
    await yield_now()
    channel.request_stop(StopMode.DRAIN)
    with pytest.raises(RuntimeError, match="cancel failed"):
        await task
