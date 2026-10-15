"""One whole run on the simulator, from the server's run API to a Docker sandbox and back.

A client subscribes to the server over ``SimNetwork``; the server drives the run through the
production launch wiring (``launch.default_runs``) and ``vs_runtime``; the run opens real
``DockerSandbox`` containers whose ``docker`` commands are scripted processes
(``FakeProcesses``) over an in-memory daemon. Everything runs on one ``SimThreads`` and its
virtual clock: the server's transport and journal, the run's event loop (``run_async``), its
blocking calls and its leases. No real thread, socket, process or wall clock is used; the
project and the run's state live under a temporary directory.

Each seed draws two faults: one ``docker`` child process dies by a signal (any command of the
run, any of three signals, or none), and the client's connection drops after a drawn number of
messages, after which it reconnects and resumes from its cursor. Whatever the seed, the run
reaches a defined end (it succeeds, or fails with the dead process named), the client is told
that end exactly once, every container the run created is removed, the run's host lease is
free again, and the server leaves no socket behind.

Replay a seed with ``--sim-seed=N``; the nightly depth sweeps ``_NIGHTLY_SEEDS``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from tests.server.support import REQUEST_TIME
from tests.support.core_plugins import capability_plugin
from tests.support.skeleton_strategy import (
    ATTEMPT,
    IMPLEMENTER_ROLE,
    OPERATIONS,
    SkeletonConfig,
    SkeletonStrategy,
)
from tests.support.task_project import write_task

from launch import LaunchSettings, default_runs
from server.api.protocol import SubscribeRequest
from server.runtime import ServerRuntime
from vibesys.api.request import RunEnvironmentSpec
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_task
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.plugin_catalog import OrchestrationRegistry
from vibesys.run.contracts import RunRequest, RunResult
from vs_agent.api import AgentCapabilities, reap_orphaned_agents
from vs_agent.api.testing import FakeAgentClient, FakeConfinement, FakeDockerBuildRunner
from vs_core.api import AttemptBudget, ItemId, WorkspaceMode, WorkspacePlan
from vs_core.api.search import Flow, NotOpened, NoWinner, search
from vs_project.api import OrchestrationDescriptor, Project
from vs_project.api.testing import FakeGitRepositories
from vs_runtime.api.core import RunTiming
from vs_sandbox.api import DockerSandbox, SandboxKind, SubprocessDockerCli
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api import Signals
from vs_sim.api.testing import (
    HANG_GUARD_S,
    FakeProcesses,
    FakeProcessSignaller,
    ProcessScript,
    Sim,
    SimThreads,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from vs_sandbox.api import CommandRunner, DockerCli, HostResource
    from vs_sim.api import Connection, Network, ProcessSpec

_PLUGIN_ID = "whole-run"
_RUN_ID = "whole-run-1"
_LEASE_S = 30.0
_RUN_LIMIT_S = 3600.0
"""Virtual seconds a run may take; past it the run is stuck, so the test fails instead of hanging."""
_DOCKER_COMMANDS = 16
"""More than a run issues, so some seeds kill no process."""
_SIGNALS = (Signals.SIGKILL, Signals.SIGSEGV, Signals.SIGTERM)
_TERMINAL = frozenset({"run_finished", "run_failed", "run_interrupted", "configuration_failed"})
_NIGHTLY_SEEDS = range(64)


@search(_PLUGIN_ID, config=SkeletonConfig, operations=OPERATIONS, roles=(IMPLEMENTER_ROLE,))
async def _one_attempt(ctx: Flow, config: SkeletonConfig) -> NoWinner | None:
    """Open one attempt workspace, so the run starts containers for the root and the attempt."""
    del config
    async with ctx.workspaces.open(
        attempt=ATTEMPT.attempt_id,
        item=ItemId(root="item-0"),
        plan=WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=ctx.facts.baseline),
        budget=AttemptBudget(),
    ) as workspace:
        if isinstance(workspace, NotOpened):
            return NoWinner(reason="the attempt workspace did not open")
    return None


@dataclass(frozen=True)
class _Faults:
    """What one seed breaks."""

    killed_command: int
    """The index, among the run's ``docker`` commands, of the one whose process dies."""
    killed_by: Signals
    created_before_death: bool
    """Whether a ``docker run`` that dies had already made its container."""
    drop_after: int
    """Messages the client reads before its connection drops."""

    @classmethod
    def draw(cls, sim: Sim) -> _Faults:
        rng = sim.random("whole-run-faults")
        return cls(
            killed_command=rng.randint(0, _DOCKER_COMMANDS - 1),
            killed_by=_SIGNALS[rng.randint(0, len(_SIGNALS) - 1)],
            created_before_death=rng.random() < 0.5,
            drop_after=rng.randint(1, 12),
        )


@dataclass
class _Daemon:
    """An in-memory Docker daemon behind scripted ``docker`` client processes."""

    faults: _Faults
    kills_removals: bool = False
    """Whether the drawn death may hit a ``docker rm``; see the expected failure below."""
    containers: dict[str, str] = field(default_factory=dict)
    """Live containers, id to name."""
    created: list[str] = field(default_factory=list)
    commands: int = 0
    killed: tuple[str, ...] | None = None

    def answer(self, spec: ProcessSpec) -> ProcessScript:
        argv = spec.argv
        index = self.commands
        self.commands += 1
        dies = index == self.faults.killed_command and (argv[1] != "rm" or self.kills_removals)
        if dies:
            self.killed = argv
        if argv[1] == "run":
            if dies and not self.faults.created_before_death:
                return ProcessScript(returncode=-self.faults.killed_by)
            name = argv[argv.index("--name") + 1]
            container = f"c{len(self.created):04x}"
            self.containers[container] = name
            self.created.append(container)
            if dies:
                return ProcessScript(returncode=-self.faults.killed_by)
            return ProcessScript(stdout=f"{container}\n".encode())
        if dies:
            return ProcessScript(returncode=-self.faults.killed_by)
        if argv[1] == "rm":
            self._remove(argv[-1])
        if argv[1] == "exec" and "id -u" in " ".join(argv):
            return ProcessScript(stdout=b"1000\n1000\n")
        return ProcessScript()

    def _remove(self, reference: str) -> None:
        for container, name in list(self.containers.items()):
            if reference in {container, name}:
                del self.containers[container]


class _Backend(FakeComputeBackend):
    """A compute backend whose Docker sandboxes are real ``DockerSandbox`` objects."""

    def __init__(self, docker: SubprocessDockerCli, threads: SimThreads) -> None:
        super().__init__()
        self._docker = docker
        self._threads = threads

    def make_sandbox(
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        resources: Sequence[HostResource] = (),
        container_image: str | None = None,
        run_id: str | None = None,
        **_other: object,
    ) -> CommandRunner:
        if kind is not SandboxKind.DOCKER:
            return super().make_sandbox(kind, host_workspace=host_workspace)
        return DockerSandbox(
            host_workspace=host_workspace,
            image=container_image or "agent-image",
            resources=resources,
            docker=self._docker,
            threads=self._threads,
            run_id=run_id,
        )


def _project(root: Path) -> None:
    root.mkdir()
    (root / "queue.py").write_text("VALUE = 1\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n'
        '[benchmark]\ncommand = ["run-benchmark"]\nresult_protocol = 2\n',
        objective="Improve the queue.\n",
    )


def _no_git_remote(spec: ProcessSpec) -> ProcessScript:
    """The project has no remote; the run asks Git once, for the origin's URL."""
    assert spec.argv[1:] == ("remote", "get-url", "origin"), spec.argv
    return ProcessScript(returncode=2, stderr=b"error: No such remote 'origin'\n")


def _request(root: Path, docker: SubprocessDockerCli) -> RunRequest:
    return RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(id=_PLUGIN_ID, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": _PLUGIN_ID}}),
        input_bundle=load_task(root),
        objective="Improve the queue.",
        exp_name=_PLUGIN_ID,
        run_id=_RUN_ID,
        agent_backend="cli",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
        run_environment=RunEnvironmentSpec(
            "docker",
            {
                "build_runner": FakeDockerBuildRunner(),
                "docker": docker,
                "orphan_reaper": _reaper,
            },
        ),
    )


def _reaper(run_id: str, *, docker: DockerCli, log: Callable[[str], None]) -> tuple[str, ...]:
    return reap_orphaned_agents(
        run_id, docker=docker, log=log, confinement_for=lambda _id: FakeConfinement()
    )


@dataclass
class _Client:
    """A frontend that subscribes, loses its connection once, and resumes from its cursor."""

    network: Network
    socket_path: Path
    drop_after: int
    events: list[dict[str, Any]] = field(default_factory=list)
    drops: int = 0

    def follow(self, runtime: ServerRuntime) -> None:
        assert runtime.transport_listening.wait(HANG_GUARD_S)
        cursor, store = 0, ""
        read = 0
        while True:
            connection = self.network.connect(str(self.socket_path), HANG_GUARD_S)
            request = SubscribeRequest(
                timestamp=REQUEST_TIME, after_sequence=cursor, store_id=store
            )
            connection.send(request.model_dump_json().encode() + b"\n")
            for message in _messages(connection):
                read += 1
                if message["type"] == "event_batch":
                    if message.get("store_id", "") != store:
                        # A new store supersedes what this client folded.
                        self.events.clear()
                    store = message.get("store_id", "")
                    self.events.extend(message["events"])
                    cursor = max(cursor, message["through_sequence"])
                if self.ended:
                    connection.close()
                    return
                if self.drops == 0 and read >= self.drop_after:
                    break
            connection.close()
            if self.drops > 0 and not self.ended:
                return  # the server hung up before the end: the assertions report it
            self.drops += 1

    @property
    def ended(self) -> bool:
        return any(event["type"] in _TERMINAL for event in self.events)


def _messages(connection: Connection) -> Iterator[dict[str, Any]]:
    buffer = b""
    while True:
        while b"\n" not in buffer:
            chunk = connection.recv(65536, HANG_GUARD_S)
            if not chunk:
                return
            buffer += chunk
        line, buffer = buffer.split(b"\n", 1)
        yield json.loads(line)


@dataclass
class _Outcome:
    result: RunResult | None
    error: BaseException | None
    client: _Client
    daemon: _Daemon
    socket_path: Path
    root: Path


def _run(tmp_path: Path, sim: Sim, *, kills_removals: bool = False) -> _Outcome:
    faults = _Faults.draw(sim)
    schedule = sim.seed if sim.schedule_seed is None else sim.schedule_seed
    threads = SimThreads(sim.clock, schedule_seed=schedule, trace=sim.trace)
    sim.clock.limit = sim.clock.now() + _RUN_LIMIT_S
    daemon = _Daemon(faults, kills_removals=kills_removals)
    docker = SubprocessDockerCli(processes=FakeProcesses(daemon.answer, pass_time=threads.sleep))
    root = tmp_path / "project"
    _project(root)
    registry = OrchestrationRegistry()
    registry.register_plugin(
        capability_plugin(
            _PLUGIN_ID,
            strategy=SkeletonStrategy.unmeasured(keeps_candidate=False),
            search=_one_attempt,
        )
    )

    def backend(_name: object, **_options: object) -> _Backend:
        return _Backend(docker, threads)

    socket_path = tmp_path / "control.sock"
    request = _request(root, docker)

    def main() -> _Outcome:
        platform = sim.platform(
            threads,
            processes=FakeProcesses(_no_git_remote, pass_time=threads.sleep),
            signaller=FakeProcessSignaller(set()),
        )
        runs = default_runs(
            LaunchSettings(
                timing=RunTiming(platform.clock, _LEASE_S, threads),
                processes=platform.processes,
                blocking=platform.blocking,
                network=platform.network,
                registry=registry,
                agent_client_factory=lambda **_: FakeAgentClient(
                    capabilities=AgentCapabilities(provider_session_resume=True), threads=threads
                ),
                backend_factory=backend,
                git_repository=FakeGitRepositories().repository,
            )
        )
        runtime = ServerRuntime(
            run_id=_RUN_ID,
            runs=runs,
            socket_path=socket_path,
            platform=platform,
        )
        client = _Client(platform.network, socket_path, faults.drop_after)
        follower = threads.spawn(lambda: client.follow(runtime), name="client", daemon=False)
        result: RunResult | None = None
        error: BaseException | None = None
        try:
            result = runtime.run(lambda: runtime.drive(request))
        except Exception as raised:  # noqa: BLE001  # LW-415607 [BLE001]; the invariants judge whichever failure the run ends with.
            # > Naming the expected types here would duplicate the invariant that a failed run names
            # > the killed command, and an unexpected type would escape before the cleanup checks run.
            error = raised
        follower.join(HANG_GUARD_S)
        assert not follower.is_alive(), "the client never saw the connection end"
        return _Outcome(result, error, client, daemon, socket_path, root)

    return threads.run(main)


def _assert_invariants(outcome: _Outcome, sim: Sim) -> None:
    faults = _Faults.draw(sim)
    context = f"seed {sim.seed}: {faults}; killed {outcome.daemon.killed}; error {outcome.error!r}"
    # A defined end: success, or a failure that names how the dead process ended.
    succeeded = outcome.result is not None and outcome.result.succeeded
    if outcome.daemon.killed is None:
        assert outcome.error is None, context
        assert succeeded, context
    elif not succeeded:
        failure = repr(outcome.error) if outcome.result is None else str(outcome.result.failure)
        assert f"exit {-faults.killed_by}" in failure, context
    # The client was told that end exactly once, across its dropped connection.
    terminal = [event["type"] for event in outcome.client.events if event["type"] in _TERMINAL]
    assert len(terminal) == 1, (context, terminal)
    assert (terminal[0] == "run_finished") == succeeded, (context, terminal)
    # Every container the run created is gone, and the run's host lease is free.
    assert outcome.daemon.containers == {}, context
    project = Project.open(outcome.root)
    with project.state.exclusive_run_host(_RUN_ID):
        pass
    assert not outcome.socket_path.exists(), context


def test_a_whole_run_ends_and_releases_everything_despite_a_dead_process_and_a_dropped_client(
    sim: Sim, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent container receives the provider credential, so the run needs one to open.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _assert_invariants(_run(tmp_path, sim), sim)


@pytest.mark.slow
@pytest.mark.parametrize("seed", _NIGHTLY_SEEDS)
def test_the_whole_run_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    sim = Sim(seed=seed)
    _assert_invariants(_run(tmp_path, sim), sim)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "a docker rm whose client dies is not retried: the container stays until interpreter "
        "exit, and an attempt workspace's failed removal is forgotten, so its retried discard "
        "reports success"
    ),
)
@pytest.mark.parametrize(
    "seed",
    [
        pytest.param(1, id="attempt-container-removal-killed"),
        pytest.param(8, id="root-container-removal-killed"),
    ],
)
def test_a_killed_container_removal_leaves_no_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    sim = Sim(seed=seed)
    outcome = _run(tmp_path, sim, kills_removals=True)
    assert outcome.daemon.killed is not None
    assert outcome.daemon.killed[1] == "rm"
    _assert_invariants(outcome, sim)
