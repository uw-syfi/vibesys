"""The cases every ``StepLog`` passes, the Fake and the log of a real run alike.

A test module subclasses ``StepLogContract``, names the subclass ``Test<Variant>`` and implements
``step_log_under_test``. The cases drive a log only through the interface and the few probes
``StepLogUnderTest`` names. They hold a log to what the loop relies on: an append is in the state
when it returns, a record the fold refuses is never appended, what was appended survives a restart,
and a charged attempt is charged only for a raise.

A result is judged on the state its own append extends: one that reaches a parked run is not
recorded as a result and leaves the log as it was (``Interrupted``), whatever the loop last read.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vs_core.api import RequestId
from vs_core.api.recovery import Cancelled, CancelReason, Interruption, Run, RunRest, at_rest
from vs_runtime._step_loop import Interrupted
from vs_sim.api import yield_now
from vs_sim.api.testing import HANG_GUARD_S

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from vs_runtime._step_loop import StepLog

type Drive = Callable[[Coroutine[Any, Any, Any]], Any]
"""Run a coroutine to completion on the loop the log under test belongs to."""


@dataclass(frozen=True)
class StepLogUnderTest:
    """A log over a run with open steps, and the means to restart it and to feed it an input."""

    log: StepLog
    reopen: Callable[[], StepLog]
    """A new process's log over everything the first one made durable."""
    push_input: Callable[[], None]
    """Append an input from outside the loop, which wakes ``next_input``."""
    drive: Drive
    runs: tuple[Run, ...]
    """The first attempt of each open step, as ``decide`` asks for it."""


class StepLogContract:
    """Cases for ``StepLog``. Implement :meth:`step_log_under_test`."""

    def step_log_under_test(self) -> StepLogUnderTest:
        """A fresh log over a fresh run, with its harness."""
        raise NotImplementedError

    def test_a_started_attempt_is_in_the_state_and_the_open_step_when_the_append_returns(
        self,
    ) -> None:
        """The loop decides on ``state`` right after an append, so it must already show it."""
        subject = self.step_log_under_test()
        run = subject.runs[0]

        async def main() -> None:
            before = subject.log.open_step(run.tag)
            assert before is not None
            assert before.started == 0
            await subject.log.step_started(run)
            open_step = subject.log.open_step(run.tag)
            assert open_step is not None
            assert (open_step.step, open_step.started) == (run.step, run.attempt)
            attempts = subject.log.state.steps.of(run.tag)
            assert attempts is not None
            assert (attempts.started, attempts.ended) == (run.attempt, False)

        subject.drive(main())

    def test_a_finished_step_is_no_longer_open_and_its_siblings_are(self) -> None:
        """One result ends one step."""
        subject = self.step_log_under_test()
        run = subject.runs[0]

        async def main() -> None:
            await subject.log.step_started(run)
            await subject.log.step_finished(
                run.tag, run.attempt, Cancelled(reason=CancelReason.WITHDRAWN)
            )
            assert subject.log.open_step(run.tag) is None
            for other in subject.runs[1:]:
                assert subject.log.open_step(other.tag) is not None

        subject.drive(main())

    def test_an_interrupted_attempt_leaves_the_step_open_and_only_a_raise_is_charged(self) -> None:
        """Clause of the retry bound: a pause or shutdown costs the step nothing."""
        subject = self.step_log_under_test()
        run = subject.runs[0]

        async def main() -> None:
            charged = 0
            reasons = (Interruption.PAUSE, Interruption.RAISED, Interruption.SHUTDOWN)
            for attempt, why in enumerate(reasons, start=1):
                await subject.log.step_started(Run(run.step, run.tag, attempt, run.deadline_at))
                await subject.log.attempt_interrupted(run.tag, attempt, why)
                charged += int(why.charged)
                open_step = subject.log.open_step(run.tag)
                assert open_step is not None
                assert open_step.started == attempt
                attempts = subject.log.state.steps.of(run.tag)
                assert attempts is not None
                assert attempts.ended
                assert attempts.charged == charged

        subject.drive(main())

    def test_a_record_the_fold_refuses_is_not_appended(self) -> None:
        """An attempt that skips a number is refused, and neither state nor log changes."""
        subject = self.step_log_under_test()
        run = subject.runs[0]
        skipped = Run(run.step, run.tag, run.attempt + 1, run.deadline_at)

        async def main() -> None:
            before = subject.log.state
            refused = False
            try:
                await subject.log.step_started(skipped)
            except ValueError:
                refused = True
            assert refused, "a log accepted an attempt out of order"
            assert subject.log.state == before
            after = subject.reopen().open_step(run.tag)
            assert after is not None
            assert after.started == 0

        subject.drive(main())

    def test_what_was_appended_survives_a_restart(self) -> None:
        """The reopened log has every open step as the first one left it, attempts ended."""
        subject = self.step_log_under_test()
        first, *rest = subject.runs

        async def main() -> None:
            await subject.log.step_started(first)
            if rest:
                await subject.log.step_started(rest[0])
                await subject.log.step_finished(
                    rest[0].tag, rest[0].attempt, Cancelled(reason=CancelReason.WITHDRAWN)
                )
            reopened = subject.reopen()
            assert reopened.open_step(first.tag) == subject.log.open_step(first.tag)
            attempts = reopened.state.steps.of(first.tag)
            assert attempts is not None
            assert attempts.ended, "the dead process's attempt must read as ended"
            if rest:
                assert reopened.open_step(rest[0].tag) is None

        subject.drive(main())

    def test_the_open_step_of_a_tag_the_run_never_had_is_none(self) -> None:
        """A tag that is not in the fold is not an open step."""
        subject = self.step_log_under_test()
        known = subject.runs[0].tag
        assert subject.log.open_step(RequestId(root="not-a-step-of-this-run")) is None
        assert known != RequestId(root="not-a-step-of-this-run")

    def test_next_input_returns_after_an_input_and_not_before(self) -> None:
        """The loop sleeps on it, so it must neither miss an input nor return for none."""
        subject = self.step_log_under_test()

        async def main() -> None:
            waiter = asyncio.ensure_future(subject.log.next_input())
            for _ in range(5):
                await yield_now()
            assert not waiter.done(), "next_input returned with no input"
            subject.push_input()
            done, _ = await asyncio.wait({waiter}, timeout=HANG_GUARD_S)
            assert done, "next_input did not return after an input"

        subject.drive(main())

    def test_a_result_that_reaches_a_parked_run_is_not_recorded_and_leaves_the_log_unchanged(
        self,
    ) -> None:
        """A pause lands, the attempt is interrupted, the run parks; the runner's result comes late.

        Core drops the result of a step in a parked epoch, so the log must not hold one: it
        answers ``Interrupted`` and appends nothing. A loop that had decided on a state from before
        the pause is corrected by this answer, not by its own check.
        """
        subject = self.step_log_under_test()
        run = subject.runs[0]

        async def main() -> None:
            await subject.log.step_started(run)
            subject.push_input()
            await subject.log.attempt_interrupted(run.tag, run.attempt, Interruption.PAUSE)
            assert at_rest(subject.log.state) is RunRest.PARKED
            before = subject.log.state
            late = Cancelled(reason=CancelReason.WITHDRAWN)
            assert isinstance(
                await subject.log.step_finished(run.tag, run.attempt, late), Interrupted
            )
            assert subject.log.state == before
            assert subject.reopen().open_step(run.tag) is not None

        subject.drive(main())

    def test_a_pause_now_parks_the_run_and_the_attempts_result_ends_the_attempt_not_the_step(
        self,
    ) -> None:
        """Pausing now parks the run at once, while the attempt still runs, so its result is late.

        The log turns it into the end of the attempt (``AttemptInterrupted``) and answers
        ``Interrupted``: the step has no result and runs again after the resume.
        """
        subject = self.step_log_under_test()
        run = subject.runs[0]

        async def main() -> None:
            await subject.log.step_started(run)
            assert at_rest(subject.log.state) is None
            subject.push_input()
            assert at_rest(subject.log.state) is RunRest.PARKED
            result = Cancelled(reason=CancelReason.WITHDRAWN)
            assert isinstance(
                await subject.log.step_finished(run.tag, run.attempt, result), Interrupted
            )
            attempts = subject.log.state.steps.of(run.tag)
            assert attempts is not None
            assert attempts.ended
            assert subject.log.open_step(run.tag) is not None

        subject.drive(main())

    def test_an_attempt_the_run_does_not_let_start_is_not_recorded(self) -> None:
        """A start the rule forbids (the run is parked) answers ``Interrupted`` and appends nothing."""
        subject = self.step_log_under_test()
        run = subject.runs[0]

        async def main() -> None:
            await subject.log.step_started(run)
            subject.push_input()
            await subject.log.attempt_interrupted(run.tag, run.attempt, Interruption.PAUSE)
            before = subject.log.state
            again = Run(run.step, run.tag, run.attempt + 1, run.deadline_at)
            assert isinstance(await subject.log.step_started(again), Interrupted)
            assert subject.log.state == before

        subject.drive(main())
