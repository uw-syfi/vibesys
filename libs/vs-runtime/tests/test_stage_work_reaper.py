"""The stage work reapers pass one contract; the production ones find work by the step's mark.

Each production reaper runs over Fakes: a process table that shows what Linux or macOS shows,
and an in-memory daemon of step containers. Real processes and a daemon's containers are in
``tests/e2e/test_stage_work_reaper_e2e.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import RequestId
from vs_runtime.api.evaluation_step_runners import (
    ContainerStageWorkReaper,
    EachStageWorkReaper,
    ProcessStageWorkReaper,
)
from vs_runtime.api.stage_marks import (
    handle_for,
    handle_of_life,
    is_handle_of,
    is_stage_mark_of,
    stage_mark,
)
from vs_runtime.api.testing import (
    FakeStageWorkReaper,
    StageWorkPlace,
    StageWorkReaperContract,
    StageWorkUnderTest,
)
from vs_sandbox.api import STEP_TAG_ENV
from vs_sandbox.api.testing import FakeStepContainers
from vs_sim.api.testing import FakeProcessTable, Shows, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator
    from contextlib import AbstractContextManager

    type _Context = Callable[[], AbstractContextManager[None]]

RUN = "run-a"
OTHER_RUN = "run-b"

tags = st.text(alphabet="abcdef-0123456789", min_size=1, max_size=12).map(
    lambda s: RequestId(root=s)
)


def _run[T](main: Coroutine[object, object, T]) -> T:
    return run_virtual(VirtualClock(0.0), main)


def _flag(owner: object, name: str) -> Callable[[], AbstractContextManager[None]]:
    @contextmanager
    def context() -> Iterator[None]:
        setattr(owner, name, True)
        try:
            yield
        finally:
            setattr(owner, name, False)

    return context


class TestFakeStageWorkReaper(StageWorkReaperContract):
    def stage_work_under_test(self) -> StageWorkUnderTest:
        fake = FakeStageWorkReaper()
        return StageWorkUnderTest(
            reaper=fake,
            run=_run,
            places=tuple(StageWorkPlace),
            leave=lambda tag, _place: fake.leave(tag),
            leave_in_other_run=lambda tag, _place: fake.leave_in_other_run(tag),
            running=lambda tag: int(tag in fake.alive),
            running_in_other_run=lambda tag: int(tag in fake.other_run_alive),
            unreachable=_flag(fake, "refusing"),
            stuck=_flag(fake, "stuck"),
        )


class _ProcessWorld:
    """Stage processes on a host whose process table shows ``shows``."""

    def __init__(self, shows: Shows) -> None:
        self.table = FakeProcessTable(shows)
        self.reaper = ProcessStageWorkReaper(self.table, RUN)
        self._lives = 0

    def leave(self, run_id: str, tag: RequestId, place: StageWorkPlace) -> None:
        self._lives += 1
        mark = stage_mark(run_id, handle_of_life(tag, self._lives % 3))
        leader = self.table.start_stage(STEP_TAG_ENV, mark)
        if place is StageWorkPlace.DESCENDANT:
            self.table.spawn(leader)

    def running(self, run_id: str, tag: RequestId) -> int:
        return self.table.running(STEP_TAG_ENV, lambda mark: is_stage_mark_of(run_id, tag, mark))

    def under_test(self) -> StageWorkUnderTest:
        return StageWorkUnderTest(
            reaper=self.reaper,
            run=_run,
            places=(StageWorkPlace.LEADER, StageWorkPlace.DESCENDANT),
            leave=lambda tag, place: self.leave(RUN, tag, place),
            leave_in_other_run=lambda tag, place: self.leave(OTHER_RUN, tag, place),
            running=lambda tag: self.running(RUN, tag),
            running_in_other_run=lambda tag: self.running(OTHER_RUN, tag),
            unreachable=_flag(self.table, "unreadable"),
            stuck=_flag(self.table, "survivors"),
        )


class TestProcessStageWorkReaperOnLinux(StageWorkReaperContract):
    def stage_work_under_test(self) -> StageWorkUnderTest:
        return _ProcessWorld(Shows.ENVIRONMENT).under_test()


class TestProcessStageWorkReaperOnMacOS(StageWorkReaperContract):
    def stage_work_under_test(self) -> StageWorkUnderTest:
        return _ProcessWorld(Shows.LEADER_ARGUMENTS).under_test()


class _ContainerWorld:
    def __init__(self) -> None:
        self.daemon = FakeStepContainers()
        self.reaper = ContainerStageWorkReaper(self.daemon.of_run(RUN), RUN)
        self._lives = 0

    def leave(self, run_id: str, tag: RequestId) -> None:
        self._lives += 1
        self.daemon.open(run_id, stage_mark(run_id, handle_of_life(tag, self._lives % 3)))

    def running(self, run_id: str, tag: RequestId) -> int:
        return sum(
            self.daemon.count(run_id, stage_mark(run_id, handle_of_life(tag, life)))
            for life in range(3)
        )

    def under_test(self) -> StageWorkUnderTest:
        return StageWorkUnderTest(
            reaper=self.reaper,
            run=_run,
            places=(StageWorkPlace.CONTAINER,),
            leave=lambda tag, _place: self.leave(RUN, tag),
            leave_in_other_run=lambda tag, _place: self.leave(OTHER_RUN, tag),
            running=lambda tag: self.running(RUN, tag),
            running_in_other_run=lambda tag: self.running(OTHER_RUN, tag),
            unreachable=_flag(self.daemon, "unreachable"),
            stuck=_flag(self.daemon, "removal_fails_silently"),
        )


class TestContainerStageWorkReaper(StageWorkReaperContract):
    def stage_work_under_test(self) -> StageWorkUnderTest:
        return _ContainerWorld().under_test()


class TestEachStageWorkReaper(StageWorkReaperContract):
    """A Docker run holds processes and containers; one reaper ends both."""

    def stage_work_under_test(self) -> StageWorkUnderTest:
        processes, containers = _ProcessWorld(Shows.LEADER_ARGUMENTS), _ContainerWorld()
        process_side, container_side = processes.under_test(), containers.under_test()

        def leave(tag: RequestId, place: StageWorkPlace) -> None:
            side = container_side if place is StageWorkPlace.CONTAINER else process_side
            side.leave(tag, place)

        def leave_in_other_run(tag: RequestId, place: StageWorkPlace) -> None:
            side = container_side if place is StageWorkPlace.CONTAINER else process_side
            side.leave_in_other_run(tag, place)

        @contextmanager
        def both(first: _Context, second: _Context) -> Iterator[None]:
            with first(), second():
                yield

        return StageWorkUnderTest(
            reaper=EachStageWorkReaper(processes.reaper, containers.reaper),
            run=_run,
            places=tuple(StageWorkPlace),
            leave=leave,
            leave_in_other_run=leave_in_other_run,
            running=lambda tag: process_side.running(tag) + container_side.running(tag),
            running_in_other_run=lambda tag: (
                process_side.running_in_other_run(tag) + container_side.running_in_other_run(tag)
            ),
            unreachable=lambda: both(process_side.unreachable, container_side.unreachable),
            stuck=lambda: both(process_side.stuck, container_side.stuck),
        )


@given(tag=tags, other=tags, life=st.integers(min_value=0, max_value=50))
def test_a_handle_belongs_to_its_tag_in_every_life_and_to_no_other_tag(
    tag: RequestId, other: RequestId, life: int
) -> None:
    assert is_handle_of(tag, handle_of_life(tag, life))
    assert is_handle_of(other, handle_of_life(tag, life)) == (other == tag)


@given(tag=tags, junk=st.text(max_size=8))
def test_a_value_that_only_starts_like_the_handle_is_not_the_tags(
    tag: RequestId, junk: str
) -> None:
    value = handle_for(tag) + junk
    is_life = junk.startswith("-life-") and junk[6:].isdigit()
    assert is_handle_of(tag, value) == (junk == "" or is_life)


@given(
    tag=tags,
    run=st.text(alphabet="ab:-", min_size=1, max_size=5),
    other=st.text(alphabet="ab:-", min_size=1, max_size=5),
)
def test_a_mark_belongs_to_its_run_only(tag: RequestId, run: str, other: str) -> None:
    mark = stage_mark(run, handle_of_life(tag, 1))
    assert is_stage_mark_of(run, tag, mark)
    assert is_stage_mark_of(other, tag, mark) == (other == run)


def test_a_pid_the_kernel_reused_is_not_killed_on_either_host() -> None:
    for shows in Shows:
        table = FakeProcessTable(shows)
        stale = table.start_stage(STEP_TAG_ENV, "m")
        table.exit(stale)
        reused = table.start_stage(STEP_TAG_ENV, "m", pid=stale.pid)
        assert table.kill(stale, STEP_TAG_ENV) is False
        assert table.alive(reused)
        assert table.kill(reused, STEP_TAG_ENV) is True
        assert not table.alive(reused)


def test_linux_shows_a_descendant_that_left_the_group_and_macos_does_not() -> None:
    mark = stage_mark(RUN, handle_of_life(RequestId(root="t"), 0))
    for shows, found in ((Shows.ENVIRONMENT, 2), (Shows.LEADER_ARGUMENTS, 1)):
        table = FakeProcessTable(shows)
        leader = table.start_stage(STEP_TAG_ENV, mark)
        table.spawn(leader, new_session=True)
        assert len(table.marked(STEP_TAG_ENV, lambda _value: True)) == found


def test_members_of_a_group_whose_leader_died_first_are_not_found_on_macos() -> None:
    """The named residual: nothing marks them once the leader that carried the mark is gone."""
    tag = RequestId(root="t")
    table = FakeProcessTable(Shows.LEADER_ARGUMENTS)
    leader = table.start_stage(STEP_TAG_ENV, stage_mark(RUN, handle_of_life(tag, 0)))
    member = table.spawn(leader)
    table.exit(leader)
    _run(ProcessStageWorkReaper(table, RUN).reap(tag))
    assert table.alive(member)
    linux = FakeProcessTable(Shows.ENVIRONMENT)
    leader = linux.start_stage(STEP_TAG_ENV, stage_mark(RUN, handle_of_life(tag, 0)))
    member = linux.spawn(leader)
    linux.exit(leader)
    _run(ProcessStageWorkReaper(linux, RUN).reap(tag))
    assert not linux.alive(member)
