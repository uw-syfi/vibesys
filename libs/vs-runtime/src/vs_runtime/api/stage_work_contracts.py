"""The cases every ``StageWorkReaper`` passes, the Fake and each production reaper alike.

A test module subclasses ``StageWorkReaperContract``, names the subclass ``Test<Variant>`` and
implements ``stage_work_under_test``. The cases drive a reaper through ``reap`` and the probes
``StageWorkUnderTest`` names, and none reads a clock to decide an outcome. Each case runs for
every place of stage work the variant can hold (``StageWorkUnderTest.places``), so a reaper that
ends a leader but not its descendants, or a process but not its container, fails here.

A reaper that waits on a host (``ps``, ``docker``, a bounded wait for an exit) must do so through
the ``BlockingRunner`` it was built with, which the cases hold shut to prove it: whatever the
reaper does on the caller's thread stops every timer of the loop it shares. A ``reap`` that is
cancelled while that work is in flight returns only once the work has ended, because the loop
starts nothing more on the tag until its ``clear`` has returned.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from vs_core.api import RequestId
from vs_runtime._step_runner import ClearUnavailableError
from vs_sim.api import yield_now

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from contextlib import AbstractContextManager

    from vs_runtime._local_job_step_runner import StageWorkReaper
    from vs_sim.api.testing import GatedBlockingRunner


_SETTLE_YIELDS = 20
"""Turns of the loop a cancelled reap gets to return in before a case says it is still waiting."""


class StageWorkPlace(StrEnum):
    """Where a dead process can leave a step's stage work."""

    LEADER = "leader"
    """The stage command's process group leader."""
    DESCENDANT = "descendant"
    """A process the command started, in its group, that the host may not show a mark on."""
    CONTAINER = "container"
    """A Docker container opened for the step, or one of the run's containers that holds its processes."""
    REMOTE_JOB = "remote-job"
    """A job on a remote cluster that the step's agent asked for and that outlives the host."""


@dataclass(frozen=True)
class StageWorkUnderTest:
    """A reaper over a world, and the probes the cases read."""

    reaper: StageWorkReaper
    run: Callable[[Coroutine[Any, Any, Any]], Any]
    """Run a coroutine to completion on the loop the reaper belongs to."""
    places: tuple[StageWorkPlace, ...]
    """The places this variant's world can hold stage work in."""
    leave: Callable[[RequestId, StageWorkPlace], None]
    """A dead process left stage work carrying the tag in the place."""
    leave_in_other_run: Callable[[RequestId, StageWorkPlace], None]
    """The same, for a different run on the same host that happens to use the same tag."""
    running: Callable[[RequestId], int]
    """How many pieces of this run's work carrying the tag still run, read from the world itself."""
    running_in_other_run: Callable[[RequestId], int]
    """How many pieces of the other run's work carrying the tag still run."""
    unreachable: Callable[[], AbstractContextManager[None]]
    """Make the host refuse every ``reap`` for as long as the context is open."""
    stuck: Callable[[], AbstractContextManager[None]]
    """Make the work survive its kill for as long as the context is open."""
    blocking: GatedBlockingRunner | None = None
    """The runner the reaper was built with, open until a case holds it.

    ``None`` for a reaper that waits on nothing, such as the in-memory Fake.
    """


class StageWorkReaperContract:
    """Cases for ``StageWorkReaper``. Implement :meth:`stage_work_under_test`."""

    def stage_work_under_test(self) -> StageWorkUnderTest:
        """A fresh reaper over a fresh world, with its harness."""
        raise NotImplementedError

    def test_reap_leaves_nothing_with_the_tag_running(self) -> None:
        """Reaping a tag ends all of its work, in every place, however much was left."""
        for place in self._places():
            subject = self.stage_work_under_test()
            tag = RequestId(root="left")
            subject.leave(tag, place)
            subject.leave(tag, place)
            assert subject.running(tag) > 0
            subject.run(subject.reaper.reap(tag))
            assert subject.running(tag) == 0, place

    def test_reap_ends_work_left_in_every_place_at_once(self) -> None:
        """One tag can hold a leader, its descendants and a container together."""
        subject = self.stage_work_under_test()
        tag = RequestId(root="left")
        for place in subject.places:
            subject.leave(tag, place)
        subject.run(subject.reaper.reap(tag))
        assert subject.running(tag) == 0

    def test_reap_is_idempotent_and_a_tag_with_no_work_is_not_an_error(self) -> None:
        """Reaping again, or a tag nothing carries, succeeds and changes nothing."""
        for place in self._places():
            subject = self.stage_work_under_test()
            tag = RequestId(root="left")
            subject.run(subject.reaper.reap(tag))
            subject.leave(tag, place)
            subject.run(subject.reaper.reap(tag))
            subject.run(subject.reaper.reap(tag))
            assert subject.running(tag) == 0, place

    def test_reap_ends_no_work_of_another_tag(self) -> None:
        """Work under another tag, in the same run, keeps running."""
        for place in self._places():
            subject = self.stage_work_under_test()
            reaped, kept = RequestId(root="reaped"), RequestId(root="kept")
            subject.leave(reaped, place)
            subject.leave(kept, place)
            before = subject.running(kept)
            assert before > 0
            subject.run(subject.reaper.reap(reaped))
            assert subject.running(reaped) == 0, place
            assert subject.running(kept) == before, place

    def test_reap_ends_no_work_of_another_run_with_the_same_tag(self) -> None:
        """Two runs on one host can use one request id; reaping one run leaves the other's work."""
        for place in self._places():
            subject = self.stage_work_under_test()
            tag = RequestId(root="shared")
            subject.leave(tag, place)
            subject.leave_in_other_run(tag, place)
            before = subject.running_in_other_run(tag)
            assert before > 0
            subject.run(subject.reaper.reap(tag))
            assert subject.running(tag) == 0, place
            assert subject.running_in_other_run(tag) == before, place

    def test_an_unreachable_host_raises_and_ends_nothing(self) -> None:
        """A host that cannot be reached raises ``ClearUnavailableError`` and proves nothing."""
        for place in self._places():
            subject = self.stage_work_under_test()
            tag = RequestId(root="left")
            subject.leave(tag, place)
            with subject.unreachable():
                _expect_unavailable(subject, tag)
            assert subject.running(tag) > 0, place

    def test_work_that_survives_its_kill_is_never_reported_clear(self) -> None:
        """A reaper that cannot end the work raises; returning would claim an empty host."""
        for place in self._places():
            subject = self.stage_work_under_test()
            tag = RequestId(root="left")
            subject.leave(tag, place)
            with subject.stuck():
                _expect_unavailable(subject, tag)
            assert subject.running(tag) > 0, place

    def test_reap_waits_on_the_host_only_through_its_blocking_runner(self) -> None:
        """Held shut, the runner parks the reap before it touches the host; opened, the reap ends."""
        for place in self._places():
            subject = self.stage_work_under_test()
            if subject.blocking is None:
                return
            blocking = subject.blocking
            tag = RequestId(root="left")
            subject.leave(tag, place)
            left = subject.running(tag)

            assert subject.run(_reap_while_held(subject, blocking, tag)) == left, place
            assert subject.running(tag) == 0, place

    def test_a_cancelled_reap_returns_only_after_the_call_in_flight_has_ended(self) -> None:
        """The runner is held shut mid-call and the reap is cancelled: it must go on waiting."""
        for place in self._places():
            subject = self.stage_work_under_test()
            if subject.blocking is None:
                return
            blocking = subject.blocking
            tag = RequestId(root="left")
            subject.leave(tag, place)

            subject.run(_cancel_while_held(subject, blocking, tag))

            assert blocking.in_flight == 0, place

    def _places(self) -> tuple[StageWorkPlace, ...]:
        return self.stage_work_under_test().places


async def _reap_while_held(
    subject: StageWorkUnderTest, blocking: GatedBlockingRunner, tag: RequestId
) -> int:
    """Start a reap with the runner held; the work still running once a call parked, then reap on."""
    blocking.hold()
    reap = asyncio.ensure_future(subject.reaper.reap(tag))
    await blocking.wait_in_flight(1, reap)
    untouched = subject.running(tag)
    blocking.release()
    await reap
    return untouched


async def _cancel_while_held(
    subject: StageWorkUnderTest, blocking: GatedBlockingRunner, tag: RequestId
) -> None:
    """Cancel a reap with a call in flight; it is still waiting until the call is let go."""
    blocking.hold()
    try:
        reap = asyncio.ensure_future(subject.reaper.reap(tag))
        await blocking.wait_in_flight(1, reap)
        reap.cancel()
        for _ in range(_SETTLE_YIELDS):
            await yield_now()
        assert not reap.done(), "reap returned while a call of its own was in flight"
    finally:
        blocking.release()
    await asyncio.gather(reap, return_exceptions=True)


def _expect_unavailable(subject: StageWorkUnderTest, tag: RequestId) -> None:
    try:
        subject.run(subject.reaper.reap(tag))
    except ClearUnavailableError:
        return
    message = "reap returned although the host could not be shown clear"
    raise AssertionError(message)
