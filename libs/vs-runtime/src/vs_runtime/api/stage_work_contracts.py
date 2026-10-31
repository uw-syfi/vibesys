"""The cases every ``StageWorkReaper`` passes, the Fake and the production reaper alike.

A test module subclasses ``StageWorkReaperContract``, names the subclass ``Test<Variant>`` and
implements ``stage_work_under_test``. The cases drive a reaper through ``reap`` and the probes
``StageWorkUnderTest`` names, and none reads a clock to decide an outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vs_core.api import RequestId
from vs_runtime._step_runner import ClearUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from contextlib import AbstractContextManager

    from vs_runtime._local_job_step_runner import StageWorkReaper


@dataclass(frozen=True)
class StageWorkUnderTest:
    """A reaper over a world, and the probes the cases read."""

    reaper: StageWorkReaper
    run: Callable[[Coroutine[Any, Any, Any]], Any]
    """Run a coroutine to completion on the loop the reaper belongs to."""
    leave: Callable[[RequestId], None]
    """A dead process left running stage work that carries the tag."""
    running: Callable[[RequestId], int]
    """How many pieces of work carrying the tag still run, read from the world itself."""
    unreachable: Callable[[], AbstractContextManager[None]]
    """Make the host refuse every ``reap`` for as long as the context is open."""


class StageWorkReaperContract:
    """Cases for ``StageWorkReaper``. Implement :meth:`stage_work_under_test`."""

    def stage_work_under_test(self) -> StageWorkUnderTest:
        """A fresh reaper over a fresh world, with its harness."""
        raise NotImplementedError

    def test_reap_leaves_nothing_with_the_tag_running(self) -> None:
        """Reaping a tag ends all of its work, however much was left."""
        subject = self.stage_work_under_test()
        tag = RequestId(root="left")
        subject.leave(tag)
        subject.leave(tag)
        assert subject.running(tag) > 0
        subject.run(subject.reaper.reap(tag))
        assert subject.running(tag) == 0

    def test_reap_is_idempotent_and_a_tag_with_no_work_is_not_an_error(self) -> None:
        """Reaping again, or a tag nothing carries, succeeds and changes nothing."""
        subject = self.stage_work_under_test()
        tag = RequestId(root="left")
        subject.run(subject.reaper.reap(tag))
        subject.leave(tag)
        subject.run(subject.reaper.reap(tag))
        subject.run(subject.reaper.reap(tag))
        assert subject.running(tag) == 0

    def test_reap_ends_no_work_of_another_tag(self) -> None:
        """Work under another tag keeps running."""
        subject = self.stage_work_under_test()
        reaped, kept = RequestId(root="reaped"), RequestId(root="kept")
        subject.leave(reaped)
        subject.leave(kept)
        subject.run(subject.reaper.reap(reaped))
        assert subject.running(reaped) == 0
        assert subject.running(kept) == 1

    def test_an_unreachable_host_raises_and_ends_nothing(self) -> None:
        """A host that cannot be reached raises ``ClearUnavailableError`` and proves nothing."""
        subject = self.stage_work_under_test()
        tag = RequestId(root="left")
        subject.leave(tag)
        with subject.unreachable():
            try:
                subject.run(subject.reaper.reap(tag))
            except ClearUnavailableError:
                pass
            else:  # pragma: no cover - every reaper must raise when it cannot reach the host.
                raise AssertionError
        assert subject.running(tag) == 1
