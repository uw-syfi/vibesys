"""One whole run on the simulator, from the server's run API to a Docker sandbox and back.

A client subscribes to the server over ``SimNetwork``; the server drives the run through the
production launch wiring (``launch.default_runs``) and ``vs_runtime``; the run opens real
``DockerSandbox`` containers whose ``docker`` commands are scripted processes
(``FakeProcesses``) over an in-memory daemon. Everything runs on one ``SimThreads`` and its
virtual clock: the server's transport and journal, the run's event loop (``run_async``), its
blocking calls and its leases. No real thread, socket, process or wall clock is used; the
project and the run's state live under a temporary directory.

Each seed draws faults of two kinds. Up to two ``docker`` client processes die by a signal:
any command of the run, by any of three signals, whether it manages a container or is a
``docker exec`` that runs a command in one (such as the baseline measurement's accuracy
check and benchmark). And the client's first connection to the server meets a network fault
(``LinkFault``: a drop from either side, a reset, a one-way partition that heals or breaks
the connection after a bound, or delayed delivery) before a drawn send. A client whose
connection broke reconnects and resumes from its cursor. Whatever the seed, the run reaches
a defined end: it succeeds, or fails naming the dead process, or fails a measurement whose
command died without blaming the candidate. A measurement command that died is never
counted as a passed gate. The client is told the end exactly once, every container the run
created is removed, the run's host lease is free again, and the server leaves no socket
behind.

Replay a seed with ``--sim-seed=N``; the nightly depth sweeps ``_NIGHTLY_SEEDS``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
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
    measure,
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
from vs_core.api import AttemptBudget, ItemId, MeasurementFailure, WorkspaceMode, WorkspacePlan
from vs_core.api.search import Flow, NotOpened, NoWinner, search
from vs_project.api import OrchestrationDescriptor, Project
from vs_project.api.testing import FakeGitRepositories
from vs_runtime.api.core import RunTiming
from vs_sandbox.api import DockerSandbox, SandboxKind, SubprocessDockerCli
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api import Signals
from vs_sim.api.testing import (
    DEFAULT_BUFFER_BYTES,
    HANG_GUARD_S,
    End,
    FakePipedProcessLauncher,
    FakeProcesses,
    FakeProcessSignaller,
    FiredFault,
    LinkFault,
    LinkFaultKind,
    PipedStart,
    ProcessScript,
    ScriptedPipedProcess,
    Sim,
    SimNetwork,
    SimThreads,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from vs_sandbox.api import CommandRunner, DockerCli, HostResource
    from vs_sim.api import Connection, Event, Network, ProcessSpec

_PLUGIN_ID = "whole-run"
_RUN_ID = "whole-run-1"
_LEASE_S = 30.0
_RUN_LIMIT_S = 3600.0
"""Virtual seconds a run may take; past it the run is stuck, so the test fails instead of hanging."""
_DOCKER_COMMANDS = 40
"""More than a run issues, so some deaths hit no command."""
_MAX_DEATHS = 2
_LATEST_LINK_FAULT = 5
"""The last send on the client's first connection the network fault may come before; every
run sends more than this on it."""
_LINK_FAULT_S = 10.0
"""The longest a partition holds, or delivery is delayed, in virtual seconds."""
_MAX_CONNECTIONS = 4
"""Only the first connection meets a fault, so a client needs at most two to reach the end."""
_RETRY_NOT_NEEDED = frozenset({"stop", "read", "cleanup"})
"""Commands a run may lose without issuing them again: every ``stop`` is followed by an
``rm -f``, whose effect the ledger does check, a lost read of the agent user's ids makes
the sandbox remap them anyway, and removing the container removes a result file whose
cleanup was lost."""
_GATES = frozenset({"accuracy", "benchmark"})
"""Measurement commands whose loss the run reports as a failed gate instead of retrying."""
_AGENT_IDENTITY_READ = "id -u agent && id -g agent"
_BENCHMARK_OUTPUT = re.compile(r"run-benchmark --vs-output \S+;")
"""The task's benchmark, as the framed benchmark command runs it."""
_FRAMED_RESULT = (
    b"\n__VIBESYS_FRAMEWORK_BENCHMARK_JSON__\n"
    b'{"kind":"hello","protocol":2,"metrics":{"score":{"direction":"max","unit":"ops"}}}\n'
    b'{"kind":"result","values":{"score":1}}\n'
    b"\n__VIBESYS_FRAMEWORK_BENCHMARK_JSON_END__\n"
)
"""What the benchmark command prints: its result file, between the decoder's markers."""
_SIGNALS = (Signals.SIGKILL, Signals.SIGSEGV, Signals.SIGTERM)
_TERMINAL = frozenset({"run_finished", "run_failed", "run_interrupted", "configuration_failed"})
_NIGHTLY_SEEDS = range(64)
_NO_LINK_FAULT = LinkFault(LinkFaultKind.DELAY, seconds=0.0)
"""A fault that changes nothing, for cases that test only the ``docker`` deaths."""


@search(_PLUGIN_ID, config=SkeletonConfig, operations=OPERATIONS, roles=(IMPLEMENTER_ROLE,))
async def _one_attempt(ctx: Flow, config: SkeletonConfig) -> NoWinner | None:
    """Measure the baseline, then open one attempt workspace.

    The run starts containers for the root and the attempt; the measurement runs its
    accuracy check and benchmark in a container through ``docker exec``.
    """
    refused = await measure(ctx, config, ctx.facts.baseline, "baseline")
    if refused is not None:
        return refused
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
class _Death:
    """One ``docker`` client process that dies by a signal."""

    command: int | str
    """The index, among the run's ``docker`` commands, of the one whose process dies, or the
    kind of command (``_Command.effect[0]``) whose first one dies."""
    signal: Signals
    after_effect: bool
    """Whether the daemon applied the command before its client died."""


@dataclass(frozen=True)
class _Faults:
    """What one seed breaks."""

    deaths: tuple[_Death, ...]
    """Up to ``_MAX_DEATHS`` client deaths, on distinct commands."""
    link: LinkFault
    """What the network does to the client's first connection."""

    @classmethod
    def draw(cls, sim: Sim) -> _Faults:
        rng = sim.random("whole-run-faults")
        count = rng.randint(0, _MAX_DEATHS)
        commands = sorted({rng.randint(0, _DOCKER_COMMANDS - 1) for _ in range(count)})
        deaths = tuple(
            _Death(
                command=command,
                signal=_SIGNALS[rng.randint(0, len(_SIGNALS) - 1)],
                after_effect=rng.random() < 0.5,
            )
            for command in commands
        )
        link = LinkFault.draw(rng, sends=_LATEST_LINK_FAULT, seconds=_LINK_FAULT_S)
        return cls(deaths=deaths, link=link)

    def death_of(self, command: int, kind: str, earlier: Sequence[_Command]) -> _Death | None:
        """The death of the ``command``-th command, of kind ``kind``, after ``earlier``."""
        first_of_kind = all(issued.effect[0] != kind for issued in earlier)
        return next(
            (
                death
                for death in self.deaths
                if death.command == command or (death.command == kind and first_of_kind)
            ),
            None,
        )


@dataclass(frozen=True)
class _Command:
    """One ``docker`` command in the daemon's ledger."""

    argv: tuple[str, ...]
    effect: tuple[str, ...]
    """What the command does to the daemon, named so a retry of it has the same name."""
    died: Signals | None
    applied: bool
    """Whether the daemon applied the effect (a dead client's command may or may not have)."""


@dataclass
class _Daemon:
    """An in-memory Docker daemon behind scripted ``docker`` client processes.

    It answers both kinds of ``docker`` client: the captured commands that manage
    containers (``run``, ``stop``, ``rm``, one-shot reads) and the streamed ``docker exec``
    that runs a command in a container. Of the latter it tells apart the commands a
    measurement runs: the accuracy check, the benchmark (which prints its result file
    framed for the decoder) and the result file's removal.
    """

    faults: _Faults
    threads: SimThreads
    containers: dict[str, str] = field(default_factory=dict)
    """Live containers, id to name."""
    created: list[str] = field(default_factory=list)
    ledger: list[_Command] = field(default_factory=list)

    def answer(self, spec: ProcessSpec) -> ProcessScript:
        """Answer one captured ``docker`` command."""
        return self._issue(tuple(spec.argv))

    def start(self, request: PipedStart) -> ScriptedPipedProcess:
        """Run one streamed ``docker exec`` to its end."""
        script = self._issue(request.argv)
        process = ScriptedPipedProcess(self.threads, merge_stderr=request.merge_stderr)
        process.write(script.stdout.decode(), script.stderr.decode())
        process.exit(script.returncode)
        return process

    def _issue(self, argv: tuple[str, ...]) -> ProcessScript:
        effect = self._effect(argv)
        death = self.faults.death_of(len(self.ledger), effect[0], self.ledger)
        applied = death is None or death.after_effect
        self.ledger.append(_Command(argv, effect, None if death is None else death.signal, applied))
        reply = self._apply(argv) if applied else ProcessScript()
        if death is not None:
            return ProcessScript(returncode=-death.signal)
        return reply

    def _effect(self, argv: tuple[str, ...]) -> tuple[str, ...]:
        if argv[1] in {"rm", "stop"}:
            return (argv[1], self._resolve(argv[-1]))
        if argv[1] == "exec":
            return (_exec_kind(argv[-1]), argv[-1])
        return (argv[1],)

    def _resolve(self, reference: str) -> str:
        """The container id *reference* names (by id or name), or *reference* itself."""
        for container, name in self.containers.items():
            if reference in {container, name}:
                return container
        return reference

    def _apply(self, argv: tuple[str, ...]) -> ProcessScript:
        if argv[1] == "run":
            name = argv[argv.index("--name") + 1]
            container = f"c{len(self.created):04x}"
            self.containers[container] = name
            self.created.append(container)
            return ProcessScript(stdout=f"{container}\n".encode())
        if argv[1] == "rm":
            container = self._resolve(argv[-1])
            if self.containers.pop(container, None) is None:
                message = f"Error response from daemon: No such container: {argv[-1]}\n"
                return ProcessScript(returncode=1, stderr=message.encode())
        if argv[1] == "exec":
            return self._exec(self._resolve(argv[-4]), argv[-1])
        return ProcessScript()

    def _exec(self, container: str, script: str) -> ProcessScript:
        if container not in self.containers:
            message = f"Error response from daemon: No such container: {container}\n"
            return ProcessScript(returncode=1, stderr=message.encode())
        kind = _exec_kind(script)
        if kind == "read":
            return ProcessScript(stdout=b"1000\n1000\n")
        if kind == "benchmark":
            return ProcessScript(stdout=_FRAMED_RESULT)
        return ProcessScript()

    def unretried(self) -> list[_Command]:
        """Killed commands that never took effect and were never issued again."""
        missing = []
        for index, command in enumerate(self.ledger):
            if command.died is None or command.applied:
                continue
            if command.effect[0] in _RETRY_NOT_NEEDED | _GATES:
                continue
            if not any(later.effect == command.effect for later in self.ledger[index + 1 :]):
                missing.append(command)
        return missing


def _exec_kind(script: str) -> str:
    """What one ``docker exec`` script does, by the shape the run gives it."""
    if script == _AGENT_IDENTITY_READ:
        return "read"
    if _BENCHMARK_OUTPUT.search(script):
        return "benchmark"
    if script.startswith("rm -f -- "):
        return "cleanup"
    if script.endswith(" true"):
        return "accuracy"
    return "exec"


class _Backend(FakeComputeBackend):
    """A compute backend whose Docker sandboxes are real ``DockerSandbox`` objects."""

    def __init__(self, docker: SubprocessDockerCli, threads: SimThreads) -> None:
        super().__init__(threads=threads)
        self._docker = docker

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
    """A frontend that subscribes and, when its connection breaks, resumes from its cursor.

    The server drains the clients attached when its run ends, then closes. A client that
    is between connections then cannot resubscribe, so, like a frontend reopening a
    finished run, it reads the rest from the run's event log (:meth:`read_log`).
    """

    network: Network
    socket_path: Path
    events: list[dict[str, Any]] = field(default_factory=list)
    connections: int = 0
    cursor: int = 0
    store: str = ""
    read_from_log: bool = False

    def follow(self, runtime: ServerRuntime) -> None:
        assert runtime.transport_listening.wait(HANG_GUARD_S)
        while not self.ended:
            assert self.connections < _MAX_CONNECTIONS, self
            try:
                connection = self.network.connect(str(self.socket_path), HANG_GUARD_S)
            except ConnectionRefusedError:
                return  # the server closed between connections: read_log takes over
            self.connections += 1
            try:
                self._read_until_end(connection)
            except ConnectionError:
                pass  # the connection broke: resume from the cursor
            finally:
                connection.close()

    def _read_until_end(self, connection: Connection) -> None:
        """Subscribe from the cursor and fold batches until the end or the stream's end."""
        request = SubscribeRequest(
            timestamp=REQUEST_TIME, after_sequence=self.cursor, store_id=self.store
        )
        connection.send(request.model_dump_json().encode() + b"\n")
        for message in _messages(connection):
            if message["type"] == "event_batch":
                if message.get("store_id", "") != self.store:
                    # A new store supersedes what this client folded.
                    self.events.clear()
                self.store = message.get("store_id", "")
                self.events.extend(message["events"])
                self.cursor = max(self.cursor, message["through_sequence"])
            if self.ended:
                return

    def read_log(self, runtime: ServerRuntime) -> None:
        """After the server closed, fold what the run logged past this client's cursor."""
        if self.ended:
            return
        self.read_from_log = True
        for event in runtime.journal.read(after_sequence=self.cursor):
            self.events.append(event.model_dump(mode="json"))

    @property
    def ended(self) -> bool:
        return any(event["type"] in _TERMINAL for event in self.events)


_SMALL_BUFFER_BYTES = 4096
"""A socket buffer the run's event stream overflows, so a client that does not read blocks
the server's writes to it."""


class _Stall(StrEnum):
    """How a subscriber stops draining what the server writes to it."""

    NEVER_READS = "never-reads"
    """It subscribes and never reads again, holding its connection open."""
    BLACKHOLED = "blackholed"
    """Everything the server sends it vanishes, and the connection outlives the run."""

    def faults(self, link: LinkFault) -> tuple[LinkFault, ...]:
        """The network's faults: the stalled subscriber's connection is the first."""
        shifted = replace(link, connection=link.connection + 1)
        if self is _Stall.NEVER_READS:
            return (shifted,)
        blackhole = LinkFault(
            LinkFaultKind.BLACKHOLE, end=End.DIALER, before_send=2, seconds=10 * _RUN_LIMIT_S
        )
        return (blackhole, shifted)


@dataclass
class _StalledSubscriber:
    """A subscriber that stops draining, beside the client that follows the run.

    It holds the network's first connection, so the following client's connections and its
    drawn fault come after it.
    """

    stall: _Stall
    network: Network
    socket_path: Path
    threads: SimThreads
    done: Event = field(init=False)

    def __post_init__(self) -> None:
        self.done = self.threads.event()

    def then(self, client: _Client, runtime: ServerRuntime) -> None:
        """Subscribe and stop reading, then let ``client`` follow the run; wait for the end."""
        assert runtime.transport_listening.wait(HANG_GUARD_S)
        connection = self.network.connect(str(self.socket_path), HANG_GUARD_S)
        request = SubscribeRequest(timestamp=REQUEST_TIME, after_sequence=0)
        connection.send(request.model_dump_json().encode() + b"\n")
        client.follow(runtime)
        assert self.done.wait(HANG_GUARD_S)
        if self.stall is _Stall.NEVER_READS:
            # The server hung up on this subscriber: what it sent ends in an end of stream.
            while connection.recv(65536, HANG_GUARD_S):
                pass
        connection.close()

    def release(self) -> None:
        self.done.set()

    def drop_own(self, fired: tuple[FiredFault, ...]) -> tuple[FiredFault, ...]:
        """The faults that fired on the following client's connections, renumbered from 1."""
        return tuple(
            FiredFault(replace(each.fault, connection=each.fault.connection - 1), each.at)
            for each in fired
            if each.fault.connection > 1
        )


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
    link_faults: tuple[FiredFault, ...]
    daemon: _Daemon
    socket_path: Path
    root: Path


def _run(tmp_path: Path, sim: Sim, faults: _Faults, stall: _Stall | None = None) -> _Outcome:
    schedule = sim.seed if sim.schedule_seed is None else sim.schedule_seed
    threads = SimThreads(sim.clock, schedule_seed=schedule, trace=sim.trace)
    sim.clock.limit = sim.clock.now() + _RUN_LIMIT_S
    daemon = _Daemon(faults, threads)
    docker = SubprocessDockerCli(
        processes=FakeProcesses(daemon.answer, pass_time=threads.sleep),
        launcher=FakePipedProcessLauncher(factory=daemon.start),
    )
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
            network_faults=(faults.link,) if stall is None else stall.faults(faults.link),
            network_buffer_bytes=_SMALL_BUFFER_BYTES if stall is not None else DEFAULT_BUFFER_BYTES,
        )
        network = platform.network
        assert isinstance(network, SimNetwork)
        runs = default_runs(
            LaunchSettings(
                timing=RunTiming(platform.clock, _LEASE_S, threads),
                processes=platform.processes,
                piped_processes=FakePipedProcessLauncher(),
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
        client = _Client(network, socket_path)
        stalled = (
            None if stall is None else _StalledSubscriber(stall, network, socket_path, threads)
        )
        follower = threads.spawn(
            lambda: client.follow(runtime) if stalled is None else stalled.then(client, runtime),
            name="client",
            daemon=False,
        )
        result: RunResult | None = None
        error: BaseException | None = None
        try:
            result = runtime.run(lambda: runtime.drive(request))
        except Exception as raised:  # noqa: BLE001  # LW-415607 [BLE001]; the invariants judge whichever failure the run ends with.
            # > Naming the expected types here would duplicate the invariant that a failed run names
            # > the killed command, and an unexpected type would escape before the cleanup checks run.
            error = raised
        if stalled is not None:
            stalled.release()
        follower.join(HANG_GUARD_S)
        assert not follower.is_alive(), "the client never saw the connection end"
        client.read_log(runtime)
        fired = network.fired if stalled is None else stalled.drop_own(network.fired)
        return _Outcome(result, error, client, fired, daemon, socket_path, root)

    return threads.run(main)


def _assert_invariants(outcome: _Outcome, sim: Sim, faults: _Faults) -> None:
    killed = [command for command in outcome.daemon.ledger if command.died is not None]
    context = f"seed {sim.seed}: {faults}; killed {killed}; error {outcome.error!r}; result {outcome.result!r}"
    # A defined end: success, or a failure that names how a dead process ended.
    succeeded = outcome.result is not None and outcome.result.succeeded
    if not killed:
        assert outcome.error is None, context
        assert succeeded, context
    elif not succeeded:
        failure = repr(outcome.error) if outcome.result is None else str(outcome.result.failure)
        named = any(f"exit {-command.died}" in failure for command in killed if command.died)
        # A dead measurement command fails its measurement instead, and that failure must not
        # blame the candidate: a client's death says nothing about the revision it measured.
        measured = any(command.effect[0] in _GATES for command in killed) and any(
            f"measurement {kind.value}" in failure
            for kind in MeasurementFailure
            if kind is not MeasurementFailure.WORKLOAD
        )
        assert named or measured, context
    else:
        # A success is earned: every killed command that never took effect was issued again.
        assert outcome.daemon.unretried() == [], context
    # A measurement command that died is never counted as a passed gate.
    for gate in _GATES:
        commands = [command for command in outcome.daemon.ledger if command.effect[0] == gate]
        passed = [
            event
            for event in outcome.client.events
            if event["type"] == "gate_finished"
            and event["data"]["gate"] == gate
            and event["status"] == "completed"
        ]
        if passed:
            assert any(command.died is None for command in commands), (context, gate)
    # The client's connection met its fault, and the client was still told that end exactly once.
    assert [fired.fault for fired in outcome.link_faults] == [faults.link], context
    terminal = [event["type"] for event in outcome.client.events if event["type"] in _TERMINAL]
    assert len(terminal) == 1, (context, terminal)
    assert (terminal[0] == "run_finished") == succeeded, (context, terminal)
    # Every container the run created is gone, and the run's host lease is free.
    assert outcome.daemon.containers == {}, context
    project = Project.open(outcome.root)
    with project.state.exclusive_run_host(_RUN_ID):
        pass
    assert not outcome.socket_path.exists(), context


def _check(
    tmp_path: Path, sim: Sim, faults: _Faults | None = None, stall: _Stall | None = None
) -> _Outcome:
    faults = _Faults.draw(sim) if faults is None else faults
    outcome = _run(tmp_path, sim, faults, stall)
    _assert_invariants(outcome, sim, faults)
    return outcome


def test_a_whole_run_ends_and_releases_everything_despite_dead_processes_and_a_network_fault(
    sim: Sim, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent container receives the provider credential, so the run needs one to open.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _check(tmp_path, sim)


@pytest.mark.slow
@pytest.mark.parametrize("seed", _NIGHTLY_SEEDS)
def test_the_whole_run_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _check(tmp_path, Sim(seed=seed))


@pytest.mark.parametrize("which", ["first", "last"])
@pytest.mark.parametrize("timing", ["before-effect", "after-effect"])
def test_a_killed_container_removal_leaves_no_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str, timing: str
) -> None:
    """Regression: a ``docker rm -f`` whose client died was not retried, so the container leaked.

    A run with no fault locates the run's container removals (the attempt's first, the
    root's last); the run under test kills one of them, before or after the daemon removed
    the container.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    (tmp_path / "clean").mkdir()
    (tmp_path / "killed").mkdir()
    clean = _check(tmp_path / "clean", Sim(seed=0), _Faults(deaths=(), link=_NO_LINK_FAULT))
    removals = [i for i, command in enumerate(clean.daemon.ledger) if command.argv[1] == "rm"]
    assert len(removals) >= 2, clean.daemon.ledger
    target = removals[0] if which == "first" else removals[-1]
    death = _Death(command=target, signal=Signals.SIGKILL, after_effect=timing == "after-effect")
    outcome = _check(
        tmp_path / "killed", Sim(seed=0), _Faults(deaths=(death,), link=_NO_LINK_FAULT)
    )
    assert outcome.daemon.ledger[target].argv[1] == "rm"


@pytest.mark.parametrize("gate", sorted(_GATES))
@pytest.mark.parametrize("timing", ["before-effect", "after-effect"])
def test_a_killed_measurement_command_is_a_failed_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str, timing: str
) -> None:
    """A ``docker exec`` of the accuracy check or the benchmark whose client died fails its gate.

    The run kills the gate's first command, before or after it ran in the container, and
    reports that gate failed, not passed.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    death = _Death(command=gate, signal=Signals.SIGKILL, after_effect=timing == "after-effect")
    outcome = _check(tmp_path, Sim(seed=0), _Faults(deaths=(death,), link=_NO_LINK_FAULT))
    assert [command.effect[0] for command in outcome.daemon.ledger if command.died] == [gate]
    finished = [
        event["status"]
        for event in outcome.client.events
        if event["type"] == "gate_finished" and event["data"]["gate"] == gate
    ]
    # The baseline measurement's gate failed; the run's final measurement of its winner runs
    # the gate again, in a new container, and may pass.
    assert finished[0] == "failed", finished


@pytest.mark.parametrize("stall", list(_Stall))
def test_a_stalled_subscriber_stalls_neither_the_run_nor_the_other_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stall: _Stall
) -> None:
    """Regression: the server wrote to a subscriber with no bound, so one that stopped draining
    held its subscription for ever, and an attached server never exited.

    The stalled subscriber connects first and the client that follows the run second, over a
    socket buffer the event stream overflows. The run still ends, the following client is
    told the end once, the server hangs up on the stalled one and says so, and exits.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with caplog.at_level(logging.WARNING, logger="server.transport.unix_jsonl"):
        _check(tmp_path, Sim(seed=0), _Faults(deaths=(), link=_NO_LINK_FAULT), stall)
    assert any("disconnecting it" in record.getMessage() for record in caplog.records)
