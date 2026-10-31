"""WorkspaceStepRunner passes the StepRunner contract and returns a workspace to its recorded commit."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import count
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import Failed, Finished
from vs_project.api import Project
from vs_runtime.api import RuntimeContractError, WorkspaceAccess
from vs_runtime.api.agent_step_runners import WorkspaceStepRunner, WorkspaceTarget
from vs_runtime.api.core import AccessGrant, AccessKey, AccessSettlement, ReceiptStore
from vs_runtime.api.step_runner import CancelSignal, ClearUnavailableError
from vs_runtime.api.testing import (
    AttemptHangs,
    AttemptReturns,
    FakeStepRunner,
    FakeWorkspace,
    StepRunnerContract,
    StepRunnerUnderTest,
)
from vs_sim.api.testing import SimThreads, VirtualClock, examples, run_virtual, yield_until

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager
    from pathlib import Path

_SLOW = "slow"
_SCOPE = core.Scope(owner=core.RunId(root="run"), generation=0)


class FaultingWorkspace(FakeWorkspace):
    """The faithful in-memory workspace, whose restoration can be made to fail."""

    fail_restore = False

    async def restore_for_agent(self, revision: str, *, preserve_paths: tuple[str, ...]) -> None:
        if self.fail_restore:
            message = "workspace restore failed"
            raise RuntimeContractError(message)
        await super().restore_for_agent(revision, preserve_paths=preserve_paths)


@dataclass
class OneWorkspace:
    """Every step writes the one workspace, with the grant of one role."""

    workspace: FaultingWorkspace
    grant: AccessGrant

    async def target(self, step: core.Request) -> WorkspaceTarget | None:
        del step
        return WorkspaceTarget(_SCOPE, self.workspace, self.grant)


@dataclass
class World:
    runner: WorkspaceStepRunner
    inner: FakeStepRunner
    workspace: FaultingWorkspace
    settlement: AccessSettlement
    clock: VirtualClock


def a_step(tag: core.RequestId, resource: str = "quick") -> core.Request:
    return core.ObserveOwnedJob(
        request_id=tag, scope=_SCOPE, resource_id=core.ResourceId(root=resource)
    )


def tag_named(name: str) -> core.RequestId:
    return core.RequestId(root=name)


def open_world(
    root: Path, *, adopts: bool = False, access: WorkspaceAccess = WorkspaceAccess.READ_WRITE
) -> World:
    (root / "project").mkdir(parents=True)
    namespace = Project.open(root / "project").state.state_store_namespace("run")
    clock = VirtualClock()
    workspace = FaultingWorkspace(path=root / "project")

    async def lookup(ref: core.WorkspaceRef | core.Scope) -> FaultingWorkspace:
        del ref
        return workspace

    settlement = AccessSettlement(ReceiptStore(namespace, threads=SimThreads()), lookup)
    workspace.access_recovery.guard(lambda: settlement.fenced_by(workspace.path))

    def behave(
        step: core.Request, _tag: core.RequestId, _attempt: int
    ) -> AttemptHangs | AttemptReturns:
        slow = isinstance(step, core.ObserveOwnedJob) and step.resource_id.root == _SLOW
        return AttemptHangs() if slow else AttemptReturns(after_s=1.0)

    inner = FakeStepRunner(clock, adopts=adopts, behave=behave)
    grant = AccessGrant(role_id="worker", access=access)
    runner = WorkspaceStepRunner(inner, OneWorkspace(workspace, grant), settlement)
    return World(runner, inner, workspace, settlement, clock)


def refusing_restores(workspace: FaultingWorkspace) -> AbstractContextManager[None]:
    @contextmanager
    def refusing() -> Iterator[None]:
        workspace.fail_restore = True
        try:
            yield
        finally:
            workspace.fail_restore = False

    return refusing()


def harness(world: World, *, adopts: bool) -> StepRunnerUnderTest:
    async def left_running(tag: core.RequestId) -> int:
        return int(tag in world.inner.running())

    return StepRunnerUnderTest(
        runner=world.runner,
        run=lambda main: run_virtual(world.clock, main),
        finishing_step=a_step,
        running_step=lambda tag: a_step(tag, _SLOW),
        left_running=left_running,
        unreachable=world.inner.unreachable
        if adopts
        else lambda: refusing_restores(world.workspace),
    )


class WorkspaceContract(StepRunnerContract):
    """The contract over a workspace runner; the inner runner adopts or does not."""

    adopts = False

    @pytest.fixture(autouse=True)
    def _scratch(self, tmp_path: Path) -> None:
        self._root = tmp_path
        self._worlds = count()

    def step_runner_under_test(self) -> StepRunnerUnderTest:
        root = self._root / f"world-{next(self._worlds)}"
        return harness(open_world(root, adopts=self.adopts), adopts=self.adopts)


class TestWorkspaceStepRunner(WorkspaceContract):
    pass


class TestAdoptingWorkspaceStepRunner(WorkspaceContract):
    adopts = True


async def start_killed(world: World, tag: core.RequestId, attempt: int) -> None:
    """Start an attempt that never ends by itself, then cancel the task awaiting it."""
    task = asyncio.ensure_future(world.runner.run(a_step(tag, _SLOW), tag, attempt, CancelSignal()))

    async def observe() -> tuple[core.RequestId, ...]:
        return world.inner.running()

    await yield_until(observe, lambda running: tag in running, f"the work of {tag}")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def baseline_of(world: World, tag: core.RequestId, attempt: int) -> str:
    receipt = world.settlement.receipt(
        AccessKey(binding="step", invocation=f"{tag.root}/{attempt}")
    )
    assert receipt is not None, f"attempt {attempt} of {tag} recorded no commit"
    return receipt.baseline


@settings(max_examples=examples(pr=6, nightly=40), deadline=None)
@given(killed=st.integers(min_value=1, max_value=5))
def test_after_any_number_of_killed_attempts_clear_restores_the_recorded_commit_and_the_next_runs(
    killed: int, tmp_path_factory: pytest.TempPathFactory
) -> None:
    world = open_world(tmp_path_factory.mktemp("world"))
    tag = tag_named("step")

    async def main() -> None:
        for attempt in range(1, killed + 1):
            if attempt > 1:
                await world.runner.clear(tag)
            await start_killed(world, tag, attempt)
        await world.runner.clear(tag)
        restored_to = world.workspace.agent_restore_calls[-1][0]
        assert restored_to == baseline_of(world, tag, killed)
        restored = {revision for revision, _ in world.workspace.agent_restore_calls}
        assert restored == {baseline_of(world, tag, n) for n in range(1, killed + 1)}
        assert world.settlement.fenced_by(world.workspace.path) == ()
        result = await world.runner.run(a_step(tag), tag, killed + 1, CancelSignal())
        assert isinstance(result, Finished)
        assert world.settlement.fenced_by(world.workspace.path) == ()

    run_virtual(world.clock, main())


def test_a_second_attempt_started_without_clear_is_refused_by_the_unsettled_first(
    tmp_path: Path,
) -> None:
    """The workspace stays fenced until clear: the dead attempt's writes are not judged yet."""
    world = open_world(tmp_path)
    tag = tag_named("step")

    async def main() -> None:
        await start_killed(world, tag, 1)
        assert world.settlement.fenced_by(world.workspace.path) != ()
        await world.runner.clear(tag)
        assert world.settlement.fenced_by(world.workspace.path) == ()

    run_virtual(world.clock, main())


def test_a_clear_that_cannot_restore_raises_unavailable_and_a_later_clear_restores(
    tmp_path: Path,
) -> None:
    world = open_world(tmp_path)
    tag = tag_named("step")

    async def main() -> None:
        await start_killed(world, tag, 1)
        with refusing_restores(world.workspace), pytest.raises(ClearUnavailableError) as refused:
            await world.runner.clear(tag)
        assert refused.value.tag == tag
        await world.runner.clear(tag)
        assert world.workspace.agent_restore_calls[-1][0] == baseline_of(world, tag, 1)
        assert world.settlement.fenced_by(world.workspace.path) == ()

    run_virtual(world.clock, main())


def test_a_step_that_writes_outside_its_grant_fails_with_a_typed_reason_and_is_reverted(
    tmp_path: Path,
) -> None:
    world = open_world(tmp_path, access=WorkspaceAccess.READ_ONLY)
    tag = tag_named("step")
    world.workspace.script_pending_changes(["candidate.py"], [])

    async def main() -> None:
        result = await world.runner.run(a_step(tag), tag, 1, CancelSignal())
        assert isinstance(result, Failed)
        assert result.reason is core.FailureReason.REFUSED
        assert world.settlement.fenced_by(world.workspace.path) == ()

    run_virtual(world.clock, main())
