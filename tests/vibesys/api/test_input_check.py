"""The public gates-only input-check run contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tests.support.docker_environment import fake_docker_environment
from tests.support.task_project import write_task

from launch import LaunchSettings, default_runs
from vibesys.api import (
    ComputeBackend,
    Config,
    CoreEvent,
    CoreEventType,
    OrchestrationDescriptor,
    ProfilerKind,
    RunRequest,
    RunResult,
    RunStatus,
)
from vibesys.api.metrics import MetricSpace
from vibesys.api.request import load_task
from vibesys.events import EventStatus, GateFinishedData, GateKind, RunStartedData
from vs_project.api.testing import FakeGitRepositories, FakeGitRepository
from vs_runtime.api.core import PRODUCTION_LEASE_SECONDS, RunTiming
from vs_runtime.api.testing import FakeStopTimer
from vs_sandbox.api import CommandResult, SandboxKind
from vs_sandbox.api.testing import FakeComputeBackend, FakeLifecycleRunner
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    FakeProcesses,
    InlineBlockingRunner,
    ProcessScript,
    Sim,
    SimNetwork,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import NoReturn

    from vibesys.api import AuxiliaryAgentLaunch, ManagedAgent
    from vibesys.api.wiring import RunResources
    from vs_agent.api import AgentEventSink, AgentInvocationStore, AgentSessionKey
    from vs_project.api import GitFaultSink
    from vs_runtime.api.infrastructure import RunState
    from vs_sandbox.api import HostResource
    from vs_sim.api import Event


@dataclass
class _ForbiddenAgentClientFactory:
    """Count an invalid provider-client construction before failing the test."""

    calls: int = 0

    def __call__(self, **_kwargs: object) -> NoReturn:
        self.calls += 1
        message = "a zero-agent run must not construct an agent client"
        raise AssertionError(message)


@dataclass
class _ForbiddenSessionAgents:
    """Count invalid session-environment acquisition before failing the test."""

    environment_opens: int = 0
    auxiliary_creations: int = 0

    def open_environment(
        self,
        resources: RunResources,
        *,
        mounts: tuple[HostResource, ...] = (),
        agent_backend: str | None = None,
        cli_provider: str | None = None,
    ) -> NoReturn:
        del resources, mounts, agent_backend, cli_provider
        self.environment_opens += 1
        message = "a zero-agent run must not open an agent environment"
        raise AssertionError(message)

    def create_agent(
        self,
        launch: AuxiliaryAgentLaunch,
        resources: RunResources,
        agent_events: AgentEventSink,
    ) -> ManagedAgent:
        del launch, resources, agent_events
        self.auxiliary_creations += 1
        message = "the input check did not request an auxiliary agent"
        raise AssertionError(message)


@dataclass
class _ForbiddenInvocationStoreFactory:
    """Count invalid invocation-journal construction before failing the test."""

    calls: int = 0

    def __call__(self, state: RunState, key: AgentSessionKey) -> AgentInvocationStore:
        del state, key
        self.calls += 1
        message = "a zero-agent run must not open an invocation journal"
        raise AssertionError(message)


def _request(root: Path, *, agent_backend: str = "stub") -> RunRequest:
    root.mkdir()
    (root / ".git").mkdir()
    (root / "queue.py").write_text("VALUE = 1\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["check-accuracy"]\n'
        '[benchmark]\ncommand = ["run-benchmark"]\nresult_protocol = 2\n',
        objective="Check the queue.\n",
    )
    return RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(
            id="check-input",
            config_version=1,
            options={"metric_space": MetricSpace().model_dump(mode="json")},
        ),
        config=Config.model_validate(
            {
                "model": {"name": "must-not-start"},
                "agent": {"roles": {"implementer": {"model": "must-not-resolve"}}},
            }
        ),
        input_bundle=load_task(root),
        objective="Check the queue.",
        exp_name="input-check",
        run_environment=fake_docker_environment(),
        agent_backend=agent_backend,
        cli_provider="claude" if agent_backend == "cli" else None,
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _run_input_check(
    root: Path,
    sim: Sim,
    *,
    agent_backend: str = "stub",
    sandbox: FakeLifecycleRunner | None = None,
) -> tuple[
    RunResult,
    list[CoreEvent],
    dict[str, object],
    _ForbiddenAgentClientFactory,
    _ForbiddenSessionAgents,
    _ForbiddenInvocationStoreFactory,
]:
    threads = sim.threads()
    client_factory = _ForbiddenAgentClientFactory()
    agents = _ForbiddenSessionAgents()
    invocation_stores = _ForbiddenInvocationStoreFactory()
    backend = FakeComputeBackend(threads=threads)
    selected_sandbox = sandbox or FakeLifecycleRunner(threads=threads)
    backend.script_sandbox_for_kind(SandboxKind.DOCKER, selected_sandbox)
    processes = FakeProcesses(lambda _spec: ProcessScript(), pass_time=lambda _seconds: None)
    runs = default_runs(
        LaunchSettings(
            timing=RunTiming(sim.clock, PRODUCTION_LEASE_SECONDS, threads),
            processes=processes,
            piped_processes=FakePipedProcessLauncher(),
            blocking=InlineBlockingRunner(),
            network=SimNetwork(threads),
            agent_client_factory=client_factory,
            agents=agents,
            invocation_store_factory=invocation_stores,
            backend_factory=lambda *_args, **_kwargs: backend,
            stop_timer=FakeStopTimer(threads=threads),
            git_repository=_WorktreeMarkerRepositories().repository,
        )
    )

    async def execute() -> tuple[RunResult, list[CoreEvent], dict[str, object]]:
        handle = runs.start(_request(root, agent_backend=agent_backend))
        result = await handle.result()
        events = [event async for event in handle.events()]
        projection = handle.session.view().projection
        assert projection is not None
        return result, events, dict(projection)

    result, events, projection = sim.run(execute(), threads=threads)
    assert backend.creations, "the run did not build its evaluation environment"
    return result, events, projection, client_factory, agents, invocation_stores


class _WorktreeMarkerRepository(FakeGitRepository):
    """Give linked Fake worktrees the ``.git`` marker real Git creates."""

    def add_worktree(self, destination: Path, commit: str) -> None:
        super().add_worktree(destination, commit)
        (destination / ".git").touch()


class _WorktreeMarkerRepositories(FakeGitRepositories):
    """Factory for the production-shaped linked-worktree Fake above."""

    def repository(self, root: Path, faults: GitFaultSink | None = None) -> FakeGitRepository:
        if faults is None:
            return super().repository(root)
        return _WorktreeMarkerRepository(root, self, faults=faults)


class _FailingAccuracySandbox(FakeLifecycleRunner):
    """Fail the environment-wrapped accuracy command and pass setup commands."""

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: Event | None = None,
    ) -> CommandResult:
        if command.endswith(" check-accuracy"):
            self.script_process(command, stderr="the input is incorrect\n", returncode=3)
        return super().execute(command, timeout=timeout, cancel=cancel)


def test_input_check_runs_both_gates_without_starting_an_agent(tmp_path: Path, sim: Sim) -> None:
    result, events, projection, client_factory, agents, invocation_stores = _run_input_check(
        tmp_path / "passing", sim
    )

    finished: list[GateFinishedData] = []
    statuses: list[EventStatus | None] = []
    for event in events:
        if isinstance(event.data, GateFinishedData):
            finished.append(event.data)
            statuses.append(event.status)
    assert result.succeeded
    assert len(finished) == 2
    assert dict(zip((event.gate for event in finished), statuses, strict=True)) == {
        GateKind.ACCURACY: EventStatus.COMPLETED,
        GateKind.BENCHMARK: EventStatus.COMPLETED,
    }
    started = [event.data for event in events if isinstance(event.data, RunStartedData)]
    assert len(started) == 1
    assert started[0].expected_roles == ()
    terminal = [
        event
        for event in events
        if event.type in {CoreEventType.RUN_FAILED, CoreEventType.RUN_FINISHED}
    ]
    assert len(terminal) == 1
    assert terminal[0].type is CoreEventType.RUN_FINISHED
    baseline = projection["baseline"]
    assert isinstance(baseline, dict)
    assert baseline["kind"] == "measured"
    assert client_factory.calls == 0
    assert agents.environment_opens == 0
    assert agents.auxiliary_creations == 0
    assert invocation_stores.calls == 0


def test_a_failed_accuracy_gate_returns_a_failed_run_result(tmp_path: Path, sim: Sim) -> None:
    sandbox = _FailingAccuracySandbox(threads=sim.threads())

    result, events, projection, client_factory, agents, invocation_stores = _run_input_check(
        tmp_path / "accuracy-failure",
        sim,
        agent_backend="cli",
        sandbox=sandbox,
    )

    failed = [
        event.data
        for event in events
        if isinstance(event.data, GateFinishedData) and event.status is EventStatus.FAILED
    ]
    assert result.status is RunStatus.FAILED
    assert not result.succeeded
    assert result.failure is not None
    accuracy_failures = [data for data in failed if data.gate is GateKind.ACCURACY]
    assert accuracy_failures, [call.command for call in sandbox.calls]
    assert accuracy_failures[0].output_tail == "the input is incorrect"
    assert projection == {
        "baseline": {
            "kind": "measured",
            "reading": {
                "accuracy_passed": False,
                "benchmark_passed": None,
                "metrics": [],
                "partial": None,
            },
        }
    }
    assert client_factory.calls == 0
    assert agents.environment_opens == 0
    assert invocation_stores.calls == 0


def test_unreadable_benchmark_evidence_returns_a_failed_run_result(
    tmp_path: Path, sim: Sim
) -> None:
    sandbox = FakeLifecycleRunner(
        default_result=CommandResult(output="not-json", exit_code=0, stdout="not-json", stderr=""),
        threads=sim.threads(),
    )
    sandbox.script_process("check-accuracy")

    result, events, projection, client_factory, agents, invocation_stores = _run_input_check(
        tmp_path / "benchmark-failure",
        sim,
        agent_backend="cli",
        sandbox=sandbox,
    )

    failed = [
        event.data
        for event in events
        if isinstance(event.data, GateFinishedData) and event.status is EventStatus.FAILED
    ]
    assert result.status is RunStatus.FAILED
    assert not result.succeeded
    assert result.failure is not None
    assert "benchmark" in result.failure.reason
    assert failed
    assert all(data.gate is GateKind.BENCHMARK for data in failed)
    assert any(
        "benchmark output did not include its result JSON" in (data.output_tail or "")
        for data in failed
    )
    assert projection == {
        "baseline": {
            "kind": "unmeasurable",
            "failure": {
                "kind": "benchmark-failed",
                "status": None,
                "output": (
                    "Framework benchmark failed.\n"
                    "not-json\n"
                    "benchmark output did not include its result JSON"
                ),
                "ended": None,
                "cause": None,
            },
            "reading": {
                "accuracy_passed": None,
                "benchmark_passed": False,
                "metrics": [],
                "partial": None,
            },
        }
    }
    assert client_factory.calls == 0
    assert agents.environment_opens == 0
    assert invocation_stores.calls == 0
