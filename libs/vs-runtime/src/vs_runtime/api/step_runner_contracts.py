"""The cases every ``StepRunner`` passes, the Fake and each real runner alike.

A test module subclasses ``StepRunnerContract``, names the subclass ``Test<Variant>`` and
implements ``step_runner_under_test``; pytest then runs every case against that runner. The
cases drive a runner only through the interface and the few probes ``StepRunnerUnderTest``
names, and none of them reads a clock to decide an outcome.

They are the six clauses of ``docs/contributing/run-recovery.md``: a run returns a result or
raises; after ``clear`` the same tag starts clean at a higher attempt; ``clear`` is idempotent
and leaves nothing running; ``Adopted`` means the next ``run`` takes over what was left; a set
cancel signal ends a run; an unreachable backend raises ``ClearUnavailableError``. A seventh case
holds the runner's thread work shut and cancels its calls: the loop starts nothing more on a tag
until its ``run`` or ``clear`` has returned, so neither may return while work they started in a
thread still runs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vs_core.api import RequestId
from vs_core.api.recovery import Cancelled, CancelReason, Failed, Finished
from vs_runtime._step_runner import Adopted, CancelSignal, Cleared, ClearUnavailableError
from vs_sim.api import SeededRandom, yield_now
from vs_sim.api.testing import HANG_GUARD_S, examples, yield_until

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from contextlib import AbstractContextManager

    from vs_core.api import Request
    from vs_core.api.recovery import StepResult
    from vs_runtime._step_runner import StepRunner
    from vs_sim.api.testing import DetachedBlockingRunner

type Run = Callable[[Coroutine[Any, Any, Any]], Any]
"""Run a coroutine to completion on the loop the runner under test belongs to."""

_ACTIONS = ("finish", "kill", "cancel", "clear")
_DEADLINE_AT = 1_000_000.0
"""A step limit no case reaches: the cases end work by the signal, a kill or a clear."""


@dataclass(frozen=True)
class StepRunnerUnderTest:
    """A runner, the steps it can be given, and the probes the cases read."""

    runner: StepRunner
    run: Run
    finishing_step: Callable[[RequestId], Request]
    """A step that ends by itself, with a result or by raising."""
    running_step: Callable[[RequestId], Request]
    """A step that runs until it is cancelled."""
    left_running: Callable[[RequestId], Coroutine[Any, Any, int]]
    """How many pieces of work carrying the tag still run, read from the backend itself."""
    unreachable: Callable[[], AbstractContextManager[None]] | None = None
    """Make the backend refuse every ``clear`` for as long as the context is open.

    ``None`` for a runner whose ``clear`` reaches no backend, such as one whose steps only read
    or are safe to repeat as they stand. Clause 6 then has no backend to refuse, and its case
    checks that ``clear`` answers."""
    blocking: DetachedBlockingRunner | None = None
    """The runner the runner under test does its own thread work through, open until a case holds it.

    It models a thread: a call goes on after its caller is cancelled, and ``in_flight`` counts it
    until its function has ended. ``None`` for a runner that blocks in no thread of its own (the
    executors and workspace layers it calls shield theirs). A runner that gives one must call it
    from ``run`` or ``clear``, or the case fails: it would prove nothing.
    """
    restart: Callable[[], Coroutine[Any, Any, StepRunner]] | None = None
    """A runner of a new process over the same world, which remembers nothing of the old one.

    Recovery clears what a dead process left from a new one, so the case that cancels a ``clear``
    uses it where there is one. ``None`` for a runner whose world has no process to replace.
    """
    ran: dict[RequestId, Request] = field(default_factory=dict)
    """The step each tag last ran, kept by the cases: ``clear`` gets the step the log holds."""


class StepRunnerContract:
    """Cases for ``StepRunner``. Implement :meth:`step_runner_under_test`."""

    def step_runner_under_test(self) -> StepRunnerUnderTest:
        """A fresh runner over a fresh world, with its harness."""
        raise NotImplementedError

    def test_a_step_that_finishes_ends_with_a_step_result(self) -> None:
        """Clause 1: ``run`` returns a result, or raises; it never returns anything else."""
        subject = self.step_runner_under_test()
        tag = _tag("finishing")

        async def main() -> None:
            outcome = await _run_to_the_end(subject, tag, attempt=1)
            assert isinstance(outcome, Exception | Finished | Failed | Cancelled)
            assert await subject.left_running(tag) == 0

        subject.run(main())

    def test_after_a_kill_and_a_clear_the_same_tag_starts_clean_at_a_higher_attempt(self) -> None:
        """Clause 2, whatever ``clear`` answered: nothing is left running and the step ends."""
        subject = self.step_runner_under_test()
        tag = _tag("restarted")

        async def main() -> None:
            await _kill_mid_run(subject, tag, attempt=1)
            await _finish_after_clear(subject, tag, attempt=2)
            assert await subject.left_running(tag) == 0

        subject.run(main())

    def test_clear_of_a_tag_that_never_started_is_cleared(self) -> None:
        """Clause 3: there is nothing to remove or adopt."""
        subject = self.step_runner_under_test()
        assert subject.run(_clear(subject, _tag("never-started"))) == Cleared()

    def test_clear_is_idempotent_and_leaves_nothing_running(self) -> None:
        """Clause 3: clearing again changes nothing, and what is left running is only adopted work."""
        subject = self.step_runner_under_test()
        tag = _tag("cleared-twice")

        async def main() -> None:
            await _kill_mid_run(subject, tag, attempt=1)
            first = await _clear(subject, tag)
            second = await _clear(subject, tag)
            if first == Cleared():
                assert second == Cleared()
            if not (first == Adopted(None) or second == Adopted(None)):
                assert await subject.left_running(tag) == 0

        subject.run(main())

    def test_adopted_work_is_taken_over_by_the_next_run_and_never_started_twice(self) -> None:
        """Clause 4: with no result the run attaches, with one it returns that result."""
        subject = self.step_runner_under_test()
        tag = _tag("adopted")

        async def main() -> None:
            await _kill_mid_run(subject, tag, attempt=1)
            adopted = await _clear(subject, tag)
            if isinstance(adopted, Adopted) and adopted.result is not None:
                again = await _run_bounded(
                    subject, subject.running_step(tag), tag, 2, CancelSignal()
                )
                assert again == adopted.result
            elif isinstance(adopted, Adopted):
                task, cancel = await _start(subject, tag, attempt=2)
                assert await subject.left_running(tag) == 1
                cancel.set(CancelReason.WITHDRAWN)
                assert isinstance(await _ends(task, tag), Cancelled)
            assert await subject.left_running(tag) == 0

        subject.run(main())

    def test_a_set_cancel_signal_ends_the_run_with_cancelled_and_nothing_running(self) -> None:
        """Clause 5: the run returns ``Cancelled`` for the reason it was given."""
        subject = self.step_runner_under_test()
        tag = _tag("cancelled")

        async def main() -> None:
            for reason in CancelReason:
                task, cancel = await _start(subject, tag, attempt=1)
                cancel.set(reason)
                assert await _ends(task, tag) == Cancelled(reason=reason)
                assert await subject.left_running(tag) == 0

        subject.run(main())

    def test_an_unreachable_backend_raises_instead_of_clearing(self) -> None:
        """Clause 6: ``clear`` proves nothing while unreachable, and works once reachable."""
        subject = self.step_runner_under_test()
        tag = _tag("unreachable")

        async def main() -> None:
            await _kill_mid_run(subject, tag, attempt=1)
            if subject.unreachable is None:
                assert await _refusal(subject, tag) is None, (
                    "clear refused with no backend to reach"
                )
            else:
                with subject.unreachable():
                    for _ in range(2):
                        refusal = await _refusal(subject, tag)
                        assert refusal is not None, (
                            "clear answered while its backend was unreachable"
                        )
                        assert refusal.tag == tag
            await _finish_after_clear(subject, tag, attempt=2)
            assert await subject.left_running(tag) == 0

        subject.run(main())

    def test_a_cancelled_call_returns_only_after_the_thread_work_it_started_has_ended(self) -> None:
        """Clause 7: the loop waits for ``run`` and ``clear``, so they must not outlive-by-return a thread."""
        subject = self.step_runner_under_test()
        if subject.blocking is None:
            return
        blocking = subject.blocking
        reached = [False, False]

        async def main() -> None:
            running, left = _tag("blocking-run"), _tag("blocking-clear")
            reached[0] = await _cancel_while_blocking(subject, blocking, running, run=True)
            await _finish_after_clear(subject, running, attempt=2)
            await _kill_mid_run(subject, left, attempt=1)
            reached[1] = await _cancel_while_blocking(subject, blocking, left, run=False)
            await _finish_after_clear(subject, left, attempt=2)

        subject.run(main())
        assert any(reached), "the runner gave a blocking runner and never called it in run or clear"

    def test_any_order_of_kills_cancels_and_clears_never_runs_a_tag_twice_at_once(self) -> None:
        """Clauses 2 to 5 over generated sequences: one copy at most, and none after a clear."""
        for seed in range(examples(pr=6, nightly=60)):
            subject = self.step_runner_under_test()
            subject.run(_exercise(subject, _tag(f"sequence-{seed}"), _actions(seed)))


_SETTLE_YIELDS = 20
"""Turns of the loop a call gets to return in before a case says it is still waiting."""


async def _cancel_while_blocking(
    subject: StepRunnerUnderTest, blocking: DetachedBlockingRunner, tag: RequestId, *, run: bool
) -> bool:
    """Cancel a ``run`` (or a ``clear``) while its thread work is in flight; whether it reached any.

    A run is first given a cancel signal, which sends it down its stop path; the work is held
    shut there, the call's task is cancelled, and the call must still be waiting: it ends only
    once the work is let go. ``False`` when the call ended without ever calling the runner.
    """

    async def in_flight() -> int:
        return blocking.in_flight

    if run:
        step = subject.ran[tag] = subject.running_step(tag)
        cancel = CancelSignal()
        call = asyncio.ensure_future(subject.runner.run(step, tag, 1, _DEADLINE_AT, cancel))
        await yield_until(
            lambda: subject.left_running(tag), lambda work: work > 0 or call.done(), str(tag)
        )
    clearing = subject.runner if subject.restart is None else await subject.restart()
    blocking.hold()
    try:
        if run:
            cancel.set(CancelReason.PAUSE_NOW)  # the runner is now in its stop path
        else:
            call = asyncio.ensure_future(
                clearing.clear(subject.ran.get(tag) or subject.finishing_step(tag), tag)
            )
        await yield_until(in_flight, lambda held: held > 0 or call.done(), f"{tag} thread work")
        if blocking.in_flight == 0:
            await asyncio.gather(call, return_exceptions=True)
            return False
        call.cancel()
        for _ in range(_SETTLE_YIELDS):
            await yield_now()
        which = "run" if run else "clear"
        assert not call.done(), f"{which} of {tag} returned while its thread work was in flight"
    finally:
        blocking.release()
    await asyncio.gather(call, return_exceptions=True)
    assert blocking.in_flight == 0
    return True


async def _refusal(subject: StepRunnerUnderTest, tag: RequestId) -> ClearUnavailableError | None:
    try:
        await _clear(subject, tag)
    except ClearUnavailableError as error:
        return error
    return None


def _clear(subject: StepRunnerUnderTest, tag: RequestId) -> Coroutine[Any, Any, Adopted | Cleared]:
    """``clear`` as the loop calls it: with the request the log holds for the step."""
    return subject.runner.clear(subject.ran.get(tag) or subject.finishing_step(tag), tag)


async def _ends(task: asyncio.Task[StepResult], tag: RequestId) -> StepResult:
    """The result of a run that was told to stop; a run that never returns fails the case."""
    done, _ = await asyncio.wait({task}, timeout=HANG_GUARD_S)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        message = f"run of {tag} did not return within {HANG_GUARD_S:g} s of its cancel signal"
        raise AssertionError(message)
    return task.result()


async def _run_bounded(
    subject: StepRunnerUnderTest,
    step: Request,
    tag: RequestId,
    attempt: int,
    cancel: CancelSignal,
) -> StepResult | BaseException:
    """``run`` once; what it returned or raised. A run that never returns fails the case.

    A runner whose ``clear`` left work behind can wait on that work for ever, on a clock
    that never stops advancing, so every ``run`` the cases make carries the hang guard.
    """
    subject.ran[tag] = step
    task = asyncio.ensure_future(subject.runner.run(step, tag, attempt, _DEADLINE_AT, cancel))
    done, _ = await asyncio.wait({task}, timeout=HANG_GUARD_S)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        message = f"run of {tag} attempt {attempt} did not return within {HANG_GUARD_S:g} s"
        raise AssertionError(message)
    (outcome,) = await asyncio.gather(task, return_exceptions=True)
    return outcome


def _tag(name: str) -> RequestId:
    return RequestId(root=name)


def _actions(seed: int) -> tuple[str, ...]:
    rng = SeededRandom(seed)
    return tuple(rng.choice(_ACTIONS) for _ in range(rng.randint(1, 8)))


async def _start(
    subject: StepRunnerUnderTest, tag: RequestId, *, attempt: int
) -> tuple[asyncio.Task[StepResult], CancelSignal]:
    """Start an attempt of a step that runs until cancelled; return once its work exists."""
    cancel = CancelSignal()
    step = subject.ran[tag] = subject.running_step(tag)
    task = asyncio.ensure_future(subject.runner.run(step, tag, attempt, _DEADLINE_AT, cancel))
    await yield_until(
        lambda: subject.left_running(tag), lambda running: running > 0 or task.done(), str(tag)
    )
    return task, cancel


async def _kill_mid_run(subject: StepRunnerUnderTest, tag: RequestId, *, attempt: int) -> None:
    """Cancel the task awaiting a running attempt: the program that started it died."""
    task, _ = await _start(subject, tag, attempt=attempt)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _finish_after_clear(
    subject: StepRunnerUnderTest, tag: RequestId, *, attempt: int
) -> None:
    """Clear, then run the next attempt to its end, taking over what ``clear`` adopted."""
    adopted = await _clear(subject, tag)
    if isinstance(adopted, Adopted) and adopted.result is None:
        task, cancel = await _start(subject, tag, attempt=attempt)
        cancel.set(CancelReason.WITHDRAWN)
        await _ends(task, tag)
        return
    await _run_to_the_end(subject, tag, attempt=attempt)


async def _run_to_the_end(
    subject: StepRunnerUnderTest, tag: RequestId, *, attempt: int
) -> StepResult | BaseException:
    """Run a step that ends by itself; a raise is a legitimate end, and ``clear`` follows it."""
    outcome = await _run_bounded(subject, subject.finishing_step(tag), tag, attempt, CancelSignal())
    if isinstance(outcome, BaseException):
        await _clear(subject, tag)
    return outcome


async def _exercise(subject: StepRunnerUnderTest, tag: RequestId, actions: tuple[str, ...]) -> None:
    attempt = 0
    dirty = False
    for action in actions:
        attempt += 1
        if dirty and action != "clear":
            await _finish_after_clear(subject, tag, attempt=attempt)
            dirty = False
        match action:
            case "finish":
                await _finish_after_clear(subject, tag, attempt=attempt)
            case "kill":
                await _kill_mid_run(subject, tag, attempt=attempt)
                dirty = True
            case "cancel":
                task, cancel = await _start(subject, tag, attempt=attempt)
                cancel.set(CancelReason.PAUSE_NOW)
                await _ends(task, tag)
            case "clear":
                outcome = await _clear(subject, tag)
                dirty = outcome == Adopted(None)
        assert await subject.left_running(tag) <= 1
    await _finish_after_clear(subject, tag, attempt=attempt + 1)
    assert await subject.left_running(tag) == 0
