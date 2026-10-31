"""The stage work reapers pass one contract; the production one finds work by the step's mark.

The production reaper runs over a Fake process table. A real process table, and real processes,
are in ``tests/e2e/test_stage_work_reaper_e2e.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import RequestId
from vs_runtime._evaluation_jobs import handle_for, handle_of_life, is_handle_of
from vs_runtime.api.evaluation_step_runners import MarkedStageWorkReaper
from vs_runtime.api.testing import (
    FakeStageWorkReaper,
    StageWorkReaperContract,
    StageWorkUnderTest,
)
from vs_sandbox.api import STEP_TAG_ENV
from vs_sim.api.testing import FakeProcessTable, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Iterator

tags = st.text(alphabet="abcdef-0123456789", min_size=1, max_size=12).map(
    lambda s: RequestId(root=s)
)


class TestFakeStageWorkReaper(StageWorkReaperContract):
    def stage_work_under_test(self) -> StageWorkUnderTest:
        fake = FakeStageWorkReaper()
        clock = VirtualClock(0.0)

        @contextmanager
        def unreachable() -> Iterator[None]:
            fake.refusing = True
            try:
                yield
            finally:
                fake.refusing = False

        return StageWorkUnderTest(
            reaper=fake,
            run=lambda main: run_virtual(clock, main),
            leave=fake.leave,
            running=lambda tag: int(tag in fake.alive),
            unreachable=unreachable,
        )


class TestMarkedStageWorkReaper(StageWorkReaperContract):
    def stage_work_under_test(self) -> StageWorkUnderTest:
        table = FakeProcessTable()
        clock = VirtualClock(0.0)
        lives = {"count": 0}

        def leave(tag: RequestId) -> None:
            lives["count"] += 1
            table.start(STEP_TAG_ENV, handle_of_life(tag, lives["count"] % 3))

        @contextmanager
        def unreachable() -> Iterator[None]:
            table.unreadable = True
            try:
                yield
            finally:
                table.unreadable = False

        return StageWorkUnderTest(
            reaper=MarkedStageWorkReaper(table),
            run=lambda main: run_virtual(clock, main),
            leave=leave,
            running=lambda tag: len(table.marked(STEP_TAG_ENV, lambda v: is_handle_of(tag, v))),
            unreachable=unreachable,
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
    assert is_handle_of(tag, value) == (
        junk == "" or (junk.startswith("-life-") and junk[6:].isdigit())
    )


def test_a_pid_the_kernel_reused_is_not_killed() -> None:
    table = FakeProcessTable()
    stale = table.start(STEP_TAG_ENV, "vs-a")
    table.exit(stale)
    reused = table.start(STEP_TAG_ENV, "vs-a", pid=stale.pid)
    assert table.kill(stale, STEP_TAG_ENV) is False
    assert table.alive(reused)
    assert table.kill(reused, STEP_TAG_ENV) is True
    assert not table.alive(reused)
