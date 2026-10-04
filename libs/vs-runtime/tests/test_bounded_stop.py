"""A stop is bounded: new work is refused at once and the run ends after a grace period."""

from __future__ import annotations

import asyncio
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api import CandidateProfileStatus, Evaluation
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

_GRACE_S = 60.0


async def _until_armed(timer: FakeStopTimer) -> None:
    # A deadlock guard: the grace wait is armed within a few loop iterations.
    assert await asyncio.to_thread(timer.wait_armed, 30.0)


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
    channel = create_run_control_channel(FakeRunControlEventSink())
    timer = FakeStopTimer()
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
    await asyncio.sleep(0)
    channel.request_stop()
    await _until_armed(timer)
    assert on_stop.calls == 1
    assert not task.done()
    timer.expire()

    with pytest.raises(RunStopped):
        await task
    assert turn_cancelled.is_set()
    assert timer.delays == [_GRACE_S]
    assert task.cancelling() == 0


@pytest.mark.asyncio
async def test_a_block_that_ends_within_the_grace_is_not_cancelled() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink())
    timer = FakeStopTimer()
    finish = asyncio.Event()

    async def run() -> str:
        async with bounded_stop(channel, grace_s=_GRACE_S, on_stop=_StopWork(), timer=timer):
            await finish.wait()
        return "drained"

    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    channel.request_stop()
    await _until_armed(timer)
    finish.set()

    assert await task == "drained"


@pytest.mark.asyncio
async def test_a_resume_within_the_grace_disarms_the_cancellation() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink())
    timer = FakeStopTimer()
    finish = asyncio.Event()

    async def run() -> str:
        async with bounded_stop(channel, grace_s=_GRACE_S, on_stop=_StopWork(), timer=timer):
            await finish.wait()
        return "resumed"

    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    channel.request_stop()
    await _until_armed(timer)
    channel.resume()
    timer.expire()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    finish.set()

    assert await task == "resumed"


@pytest.mark.asyncio
async def test_a_stop_pending_at_entry_is_bounded_and_a_cleanup_failure_is_kept() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink())
    channel.request_stop()
    timer = FakeStopTimer()
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
    channel = create_run_control_channel(FakeRunControlEventSink())
    timer = FakeStopTimer()

    async def run() -> None:
        async with bounded_stop(
            channel,
            grace_s=_GRACE_S,
            on_stop=_StopWork(RuntimeError("scancel failed")),
            timer=timer,
        ):
            channel.request_stop()
            await _until_armed(timer)

    with pytest.raises(RuntimeError, match="scancel failed"):
        await asyncio.create_task(run())


@pytest.mark.asyncio
async def test_an_outside_cancellation_stays_a_cancellation() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink())

    async def run() -> None:
        async with bounded_stop(
            channel, grace_s=_GRACE_S, on_stop=_StopWork(), timer=FakeStopTimer()
        ):
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@given(st.one_of(st.floats(max_value=0.0), st.just(math.inf), st.just(math.nan)))
def test_a_grace_that_is_not_positive_and_finite_is_rejected(grace_s: float) -> None:
    channel = create_run_control_channel(FakeRunControlEventSink())

    async def enter() -> None:
        async with bounded_stop(channel, grace_s=grace_s, on_stop=_StopWork()):
            pass

    with pytest.raises(StopGraceError, match="positive finite"):
        asyncio.run(enter())


def test_stop_listeners_see_each_request_until_unsubscribed() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink())
    seen: list[bool] = []
    unsubscribe = channel.on_stop_requested(lambda: seen.append(channel.stop_requested()))

    assert not channel.stop_requested()
    channel.request_stop()
    channel.resume()
    assert not channel.stop_requested()
    unsubscribe()
    unsubscribe()
    channel.request_stop()

    assert seen == [True]
    assert channel.stop_requested()


@pytest.mark.asyncio
async def test_gated_evaluation_refuses_new_work_after_a_stop() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink())
    inner = FakeEvaluation()
    evaluation = stop_gated_evaluation(inner, channel)
    workspace = FakeWorkspace(workspace_id="candidate")
    await evaluation.accuracy(workspace)

    channel.request_stop()
    with pytest.raises(RunStopped):
        await evaluation.accuracy(workspace)
    with pytest.raises(RunStopped):
        await evaluation.benchmark(workspace)
    with pytest.raises(RunStopped):
        await evaluation.validate_local(
            workspace, recipe_artifact="recipe.sh", report_location="report.md"
        )
    with pytest.raises(RunStopped):
        await evaluation.profile("fake-revision", "Where does time go?", member_id="h1")

    assert len(inner.accuracy_calls) == 1
    assert inner.benchmark_calls == []
    assert inner.local_validation_calls == []
    assert inner.profile_calls == []
    assert await evaluation.agent_evaluations(workspace) == ()


@pytest.mark.asyncio
async def test_gated_evaluation_cancels_a_running_evaluation_at_the_stop() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink())
    inner = FakeEvaluation()
    gate = inner.gate("benchmark", 0)
    evaluation = stop_gated_evaluation(inner, channel)
    workspace = FakeWorkspace(workspace_id="candidate")

    running = asyncio.create_task(evaluation.benchmark(workspace))
    await gate.entered.wait()
    channel.request_stop()

    with pytest.raises(RunStopped):
        await running
    assert gate.cancelled_while_live
    assert not gate.released


@pytest.mark.asyncio
async def test_gated_evaluation_returns_a_result_that_finishes_before_any_stop() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink())
    inner = FakeEvaluation()
    evaluation = stop_gated_evaluation(inner, channel)
    workspace = FakeWorkspace(workspace_id="candidate")

    result = await evaluation.benchmark(workspace)

    assert result == inner.default_benchmark
    profile = await evaluation.profile("fake-revision", "Where does time go?", member_id="h1")
    assert profile.revision == "fake-revision"


def _evaluation_members() -> list[str]:
    return sorted(
        name
        for name, value in vars(Evaluation).items()
        if not name.startswith("_") and callable(value)
    )


@pytest.mark.parametrize("member", _evaluation_members())
def test_gated_evaluation_offers_every_evaluation_member(member: str) -> None:
    """A member added to Evaluation later must reach the gated wrapper too (#1227 queue)."""
    channel = create_run_control_channel(FakeRunControlEventSink())
    evaluation = stop_gated_evaluation(FakeEvaluation(), channel)

    assert callable(getattr(evaluation, member, None)), member


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False])
async def test_gated_evaluation_reports_the_inner_profiling_capability_after_a_stop(
    *, supported: bool
) -> None:
    channel = create_run_control_channel(FakeRunControlEventSink())
    inner = FakeEvaluation()
    inner.profiling_supported = supported
    evaluation = stop_gated_evaluation(inner, channel)

    assert await evaluation.can_profile() is supported
    channel.request_stop()
    assert await evaluation.can_profile() is supported


@pytest.mark.asyncio
async def test_gated_evaluation_releases_jobs_after_a_stop_and_refuses_later_profiles() -> None:
    """Release is cleanup, so a stop does not refuse it; a repeat releases nothing."""
    channel = create_run_control_channel(FakeRunControlEventSink())
    inner = FakeEvaluation(profiling_supported=True)
    evaluation = stop_gated_evaluation(inner, channel)
    channel.request_stop()

    first = await evaluation.release_jobs("h1")
    again = await evaluation.release_jobs("h1")

    assert first.first_release
    assert not again.first_release
    assert (again.evaluations, again.profiler_operations) == ((), ())
    assert inner.released == ["h1", "h1"]
    assert await evaluation.jobs_released("h1")
    with pytest.raises(RunStopped):
        await evaluation.reopen_jobs("h1")
    assert await evaluation.jobs_released("h1")
    profile = await inner.profile("fake-revision", "Where does time go?", member_id="h1")
    assert profile.status is CandidateProfileStatus.FAILED
