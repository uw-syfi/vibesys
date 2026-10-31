"""AgentTurnStepRunner passes the StepRunner contract, and a killed turn leaves nothing behind."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from itertools import count
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.session_world import (
    SCOPE,
    FakeSessionResolver,
    dispatch_request,
    ensure_request,
)

import vs_core.api as core
from vs_agent.api import (
    STEP_TAG_ENV,
    AgentClient,
    AgentTurnRequest,
    reap_step_agents,
    step_tag_env,
)
from vs_agent.api.testing import (
    FakeAgentInvocationStore,
    FakeAgentProcesses,
    FakeAgentSessions,
    FakeProvider,
    FakeTurnScript,
)
from vs_core.api.recovery import Cancelled, CancelReason, Finished
from vs_project.api import Project
from vs_prompts.api import TemplateRenderer
from vs_runtime.api.agent_step_runners import (
    AgentTurnStepRunner,
    NotATurnError,
    TurnState,
    TurnWithoutResultError,
)
from vs_runtime.api.core import ReceiptStore, RuntimeSessionRequests
from vs_runtime.api.executor_contexts import context_for
from vs_runtime.api.step_runner import CancelSignal, Cleared
from vs_runtime.api.testing import (
    FakeWorkspace,
    StepRunnerContract,
    StepRunnerUnderTest,
)
from vs_sandbox.api import RUN_ID_LABEL
from vs_sandbox.fake_docker_engine import FakeDockerEngine
from vs_sim.api import DISCARD
from vs_sim.api.testing import (
    HANG_GUARD_S,
    FakePipedProcessLauncher,
    ManualClock,
    SimBlockingRunner,
    SimThreads,
    VirtualClock,
    examples,
    run_virtual,
    yield_until,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path

    from vs_agent.api import AgentTurnRequest as TurnRequest

_RUN = "run-a"


@dataclass
class Behavior:
    """What the Fake provider does with each turn, by the tag of the step that asked for it."""

    processes: FakeAgentProcesses
    container: str
    hangs: set[str] = field(default_factory=set)
    """Tags whose turn starts an agent process and runs until the provider is cancelled."""
    fails: set[str] = field(default_factory=set)
    """Tags whose turn is accepted and then dies with the provider."""
    turns: list[AgentTurnRequest] = field(default_factory=list)
    provider: FakeProvider | None = None

    def on_turn(self, request: TurnRequest) -> None:
        self.turns.append(request)
        tag = request.step_tag
        if tag is not None and tag in self.hangs:
            assert self.provider is not None
            self.processes.start(self.container, step_tag_env(tag))
            self.provider.hold_until_cancelled(HANG_GUARD_S)
        if tag is not None and tag in self.fails:
            message = "provider died after accepting the turn"
            raise ConnectionError(message)


@dataclass
class World:
    """A run's agent stack over Fakes: provider, journal, receipts, workspace, containers."""

    runner: AgentTurnStepRunner
    turns: RuntimeSessionRequests
    clock: VirtualClock
    threads: SimThreads
    behavior: Behavior
    engine: FakeDockerEngine
    workspace: FakeWorkspace
    containers: tuple[str, ...]

    @property
    def processes(self) -> FakeAgentProcesses:
        return self.behavior.processes

    @property
    def hangs(self) -> set[str]:
        return self.behavior.hangs

    @property
    def fails(self) -> set[str]:
        return self.behavior.fails

    @property
    def provider_turns(self) -> list[AgentTurnRequest]:
        return self.behavior.turns

    def step(self, tag: core.RequestId) -> core.DispatchTurn:
        return dispatch_request(request_id=tag.root, invocation=f"inv-{tag.root}")

    def finishing(self, tag: core.RequestId) -> core.Request:
        self.hangs.discard(tag.root)
        return self.step(tag)

    def running(self, tag: core.RequestId) -> core.Request:
        self.hangs.add(tag.root)
        return self.step(tag)

    async def left_running(self, tag: core.RequestId) -> int:
        return len(self.processes.alive(tag=tag.root))

    def run(self, main: Coroutine[object, object, object]) -> object:
        async def with_session() -> object:
            ensure = ensure_request()
            await self.turns.execute(ensure, context_for(ensure))
            return await main

        return run_virtual(self.clock, with_session(), driver=self.threads)


def tag_named(name: str) -> core.RequestId:
    return core.RequestId(root=name)


def open_world(root: Path) -> World:
    root.mkdir(parents=True, exist_ok=True)
    workspace_path = root / "workspace"
    workspace_path.mkdir()
    namespace = Project.open(root).state.state_store_namespace("run")
    clock = VirtualClock()
    threads = SimThreads(clock)
    store = ReceiptStore(namespace, threads=threads)
    engine = FakeDockerEngine(
        root / "docker", agent_ids=(1000, 1000), launcher=FakePipedProcessLauncher()
    )
    containers = tuple(
        engine.run(
            ["docker", "run", "-d", "--label", f"{RUN_ID_LABEL}={_RUN}", "image"],
            timeout_seconds=1,
            output=DISCARD,
        ).stdout.strip()
        for _ in range(2)
    )
    behavior = Behavior(FakeAgentProcesses(), containers[0])
    behavior.provider = FakeProvider(
        script=FakeTurnScript(({"value": 7},)), on_turn=behavior.on_turn, threads=threads
    )
    client = AgentClient(behavior.provider, clock=ManualClock(), threads=threads)
    sessions = FakeAgentSessions(client, FakeAgentInvocationStore(), threads=threads)
    blocking = SimBlockingRunner(threads)
    resolver = FakeSessionResolver(workspace_path, TemplateRenderer(workspace_path))
    workspace = FakeWorkspace(path=workspace_path)
    resolver.guarded = workspace
    turns = RuntimeSessionRequests(sessions, resolver, store, yields=None, blocking=blocking)

    def reap(tag: str) -> None:
        reap_step_agents(
            _RUN,
            tag,
            docker=engine,
            log=lambda _line: None,
            reap_tagged=behavior.processes.reap_tagged,
        )

    runner = AgentTurnStepRunner(turns, TurnState(sessions, store, blocking), context_for, reap)
    return World(runner, turns, clock, threads, behavior, engine, workspace, containers)


class AgentTurnContract(StepRunnerContract):
    """The contract over the turn runner, with turns that hang or finish by the tag's flag."""

    @pytest.fixture(autouse=True)
    def _scratch(self, tmp_path: Path) -> None:
        self._root = tmp_path
        self._worlds = count()

    def step_runner_under_test(self) -> StepRunnerUnderTest:
        world = open_world(self._root / f"world-{next(self._worlds)}")
        return StepRunnerUnderTest(
            runner=world.runner,
            run=world.run,
            finishing_step=world.finishing,
            running_step=world.running,
            left_running=world.left_running,
            unreachable=world.processes.unreachable,
        )


class TestAgentTurnStepRunner(AgentTurnContract):
    pass


def test_a_turn_starts_every_agent_process_with_its_step_s_tag(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    tag = tag_named("step-a")
    cancel = CancelSignal()

    async def main() -> None:
        task = asyncio.ensure_future(world.runner.run(world.running(tag), tag, 1, cancel))
        await yield_until(
            lambda: world.left_running(tag), lambda running: running == 1, "the turn's process"
        )
        (process,) = world.processes.alive(tag=tag.root)
        assert process.env == {STEP_TAG_ENV: tag.root}
        cancel.set(CancelReason.WITHDRAWN)
        assert await task == Cancelled(reason=CancelReason.WITHDRAWN)

    world.run(main())
    assert [request.step_tag for request in world.provider_turns] == [tag.root]


def test_a_killed_turn_leaves_its_process_until_clear_ends_it_and_no_container_goes(
    tmp_path: Path,
) -> None:
    world = open_world(tmp_path)
    tag = tag_named("step-a")
    bystander = world.processes.start(world.containers[1], step_tag_env("step-b"))

    async def main() -> None:
        task = asyncio.ensure_future(world.runner.run(world.running(tag), tag, 1, CancelSignal()))
        await yield_until(
            lambda: world.left_running(tag), lambda running: running == 1, "the turn's process"
        )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert await world.left_running(tag) == 1

        assert await world.runner.clear(tag) == Cleared()

        assert await world.left_running(tag) == 0
        assert world.processes.is_alive(bystander)
        assert sorted(c.container_id for c in world.engine.containers()) == sorted(world.containers)

    world.run(main())


def test_the_next_attempt_after_a_kill_is_asked_again_and_judged_from_the_recorded_baseline(
    tmp_path: Path,
) -> None:
    world = open_world(tmp_path)
    tag = tag_named("step-a")

    async def main() -> None:
        task = asyncio.ensure_future(world.runner.run(world.running(tag), tag, 1, CancelSignal()))
        await yield_until(
            lambda: world.left_running(tag), lambda running: running == 1, "the turn's process"
        )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert world.turns.settlement.fenced_by(world.workspace.path) != ()

        await world.runner.clear(tag)

        assert world.turns.settlement.fenced_by(world.workspace.path) == ()
        assert len(world.workspace.agent_restore_calls) == 1
        result = await world.runner.run(world.finishing(tag), tag, 2, CancelSignal())
        assert isinstance(result, Finished)
        assert result.report.outcome_json == '{"value":7}'
        assert world.turns.settlement.fenced_by(world.workspace.path) == ()

    world.run(main())
    assert len(world.provider_turns) == 2


def test_a_rerun_after_a_kill_is_judged_against_workspace_access_like_the_first_attempt(
    tmp_path: Path,
) -> None:
    """A receipt left settled by the kill's cleanup would judge nothing: the rerun's writes stay."""
    world = open_world(tmp_path)
    tag = tag_named("step-a")

    async def main() -> None:
        task = asyncio.ensure_future(world.runner.run(world.running(tag), tag, 1, CancelSignal()))
        await yield_until(
            lambda: world.left_running(tag), lambda running: running == 1, "the turn's process"
        )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await world.runner.clear(tag)
        world.workspace.script_pending_changes(["candidate.py"], [])

        result = await world.runner.run(world.finishing(tag), tag, 2, CancelSignal())

        assert isinstance(result, Finished)
        assert result.observation.status is core.ObservationStatus.FAILED
        assert "candidate.py" in result.observation.diagnostic

    world.run(main())


@settings(max_examples=examples(pr=8, nightly=60), deadline=None)
@given(
    later=st.lists(st.sampled_from(["run", "clear"]), min_size=0, max_size=8),
    tmp=st.just(None),
)
def test_an_answered_turn_is_never_asked_again_and_a_clear_never_reverts_its_writes(
    later: list[str], tmp: None, tmp_path_factory: pytest.TempPathFactory
) -> None:
    del tmp
    world = open_world(tmp_path_factory.mktemp("world"))
    tag = tag_named("step-a")

    async def main() -> None:
        answered = await world.runner.run(world.finishing(tag), tag, 1, CancelSignal())
        assert isinstance(answered, Finished)
        for attempt, action in enumerate(later, start=2):
            if action == "clear":
                assert await world.runner.clear(tag) == Cleared()
            else:
                again = await world.runner.run(world.finishing(tag), tag, attempt, CancelSignal())
                assert again == answered
        assert len(world.provider_turns) == 1
        assert world.workspace.agent_restore_calls == []

    world.run(main())


def test_a_step_that_is_not_a_turn_or_not_the_tags_own_is_refused(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    tag = tag_named("step-a")
    job = core.ObserveOwnedJob(request_id=tag, scope=SCOPE, resource_id=core.ResourceId(root="job"))

    async def main() -> None:
        with pytest.raises(NotATurnError, match="observe"):
            await world.runner.run(job, tag, 1, CancelSignal())
        with pytest.raises(ValueError, match="request id"):
            await world.runner.run(world.finishing(tag), tag_named("step-b"), 1, CancelSignal())

    world.run(main())
    assert world.provider_turns == []


def test_a_provider_that_dies_after_accepting_the_turn_gives_no_result_and_the_rerun_is_clean(
    tmp_path: Path,
) -> None:
    """The attempt raises, as a crash does; clear forgets the unanswered record; the rerun answers."""
    world = open_world(tmp_path)
    tag = tag_named("step-a")
    world.fails.add(tag.root)

    async def main() -> None:
        with pytest.raises(TurnWithoutResultError) as lost:
            await world.runner.run(world.finishing(tag), tag, 1, CancelSignal())
        assert lost.value.tag == tag
        assert not lost.value.observation.terminal

        world.fails.discard(tag.root)
        assert await world.runner.clear(tag) == Cleared()
        result = await world.runner.run(world.finishing(tag), tag, 2, CancelSignal())

        assert isinstance(result, Finished)

    world.run(main())
    assert len(world.provider_turns) == 2
