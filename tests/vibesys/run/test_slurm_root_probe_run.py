"""A run on a local Slurm root holds its Slurm work for the root's probe, and ends on its failure.

The root session starts the workspace-root probe beside the run (a 1-CPU Slurm job proving
the compute nodes share the run's root). These tests drive whole runs through the public
session, on a Fake cluster and in virtual time:

- while the probe job waits in the queue, the run's measurement reaches no scheduler: the
  run environment hands the probe to the cluster facts, and the host hands it to the
  evaluation executor;
- a probe that failed for good (its job failed) ends the run at once, as a failure that
  names the probe, instead of letting the run spend its agents' turns on work the cluster
  will refuse;
- a probe the queue never schedules, for hours past any probe budget, never ends the run:
  only the run's own deadline bounds the wait, and once the queue frees the probe proves
  the root and the held measurements go ahead.
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

import pytest
from tests.support.core_plugins import capability_plugin
from tests.support.skeleton_strategy import IMPLEMENTER, SkeletonStrategy
from tests.support.task_project import write_task

from launch import LaunchSettings, create_session
from vibesys.api import (
    ComputeBackend,
    Config,
    CoreEvent,
    CoreEventType,
    OrchestrationDescriptor,
    OrchestrationRegistry,
    ProfilerKind,
    RunFailureKind,
    RunRequest,
    RunResult,
)
from vibesys.api.request import RunEnvironmentSpec, load_task
from vs_agent.api import AgentCapabilities, reap_orphaned_agents
from vs_agent.api.testing import FakeAgentClient, FakeConfinement, FakeDockerBuildRunner
from vs_core.api.testing import sample_limits
from vs_project.api import FakeStateStores
from vs_project.api.testing import FakeGitRepositories
from vs_runtime.api import AgentRole
from vs_runtime.api.core import RunTiming
from vs_sandbox.api.testing import FakeComputeBackend, FakeDockerEngine
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    FakeProcesses,
    ProcessScript,
    SimBlockingRunner,
    SimNetwork,
)
from vs_slurm.api import FakeConnector, SlurmJobStatus

# test-isolation: the Fake connector's request log is an executable test double outside the API.
from vs_slurm.fake_connector import recorded_commands

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_core.api import Limits
    from vs_project.api import GitFaultSink, GitRepository
    from vs_sim.api import ProcessSpec
    from vs_sim.api.testing import Sim

_PLUGIN_ID = "slurm-probe-run"
_SLURM_LOCAL = """[slurm]
name = "test-cluster"
remote_workspace_root = "{remote}"
poll_interval_seconds = 60

[slurm.transport]
kind = "local"
shell_command = ["fake-connector", "shell"]
rsync_command = ["fake-connector", "rsync"]
"""


def _probe_operation(run_id: str, generation: int = 0) -> str:
    # test-isolation: the probe's scheduler identity, which the Fake scripts by operation id;
    # a retry runs under the next generation's.
    key = run_id if generation == 0 else f"{run_id}\0retry-{generation}"
    return "workspace-root-probe-" + hashlib.sha256(key.encode()).hexdigest()[:32]


class _ProductGit:
    """Product-shaped Fake Git whose linked worktrees expose their metadata marker."""

    def __init__(self) -> None:
        self.disk = FakeGitRepositories()

    def repository(self, root: Path, faults: GitFaultSink) -> GitRepository:
        """Open one Fake repository and materialize the marker real linked trees expose."""
        repository = self.disk.repository(root, faults)
        if repository.is_inside_work_tree() and not (root / ".git").exists():
            (root / ".git").mkdir()
        return repository


def _request(tmp_path: Path, *, slurm_extra: str = "") -> RunRequest:
    root = tmp_path / "project"
    root.mkdir()
    (root / "queue.py").write_text("VALUE = 1\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Improve the queue.\n",
    )
    remote = tmp_path / "remote"
    remote.mkdir()
    config = tmp_path / "slurm.toml"
    config.write_text(
        _SLURM_LOCAL.format(remote=remote).replace(
            "poll_interval_seconds = 60\n", f"poll_interval_seconds = 60\n{slurm_extra}"
        ),
        encoding="utf-8",
    )
    return RunRequest(
        project_root=root,
        run_id="slurm-probe-run",
        orchestration=OrchestrationDescriptor(id=_PLUGIN_ID, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_task(root),
        exp_name="slurm-probe-run",
        run_environment=RunEnvironmentSpec(
            "slurm",
            {
                "config_path": str(config),
                "build_runner": FakeDockerBuildRunner(),
                "docker": FakeDockerEngine(launcher=FakePipedProcessLauncher()),
                "orphan_reaper": partial(
                    reap_orphaned_agents, confinement_for=lambda _id: FakeConfinement()
                ),
            },
        ),
        agent_backend="stub",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


@dataclass(frozen=True)
class _Shape:
    """What a run sets beyond the defaults: Slurm settings, its deadline, its core limits."""

    slurm_extra: str = ""
    deadline_seconds: float = 1000.0
    limits: Limits | None = None
    lease_seconds: float = 60.0
    control_poll_seconds: float = 1.0


def _run(
    tmp_path: Path,
    sim: Sim,
    connector: FakeConnector,
    strategy: SkeletonStrategy,
    shape: _Shape | None = None,
) -> tuple[RunResult, list[CoreEvent]]:
    """Run the skeleton ``strategy`` to its end on the local Slurm root behind ``connector``."""
    shape = shape or _Shape()
    request = _request(tmp_path, slurm_extra=shape.slurm_extra)
    registry = OrchestrationRegistry()
    registry.register_plugin(
        capability_plugin(
            _PLUGIN_ID,
            agents=(AgentRole(id=IMPLEMENTER.root, system_prompt="Implement."),),
            strategy=strategy,
            deadline_seconds=shape.deadline_seconds,
            limits=shape.limits,
        )
    )
    threads = sim.threads()

    def no_origin(spec: ProcessSpec) -> ProcessScript:
        assert spec.argv[-3:] == ("remote", "get-url", "origin")
        return ProcessScript(returncode=128, stderr=b"fatal: no origin")

    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(
        request,
        sink=record,
        registry=registry,
        settings=LaunchSettings(
            blocking=SimBlockingRunner(threads),
            network=SimNetwork(threads),
            timing=RunTiming(
                sim.clock,
                shape.lease_seconds,
                threads,
                control_poll_seconds=shape.control_poll_seconds,
            ),
            processes=FakeProcesses(no_origin, pass_time=threads.sleep),
            piped_processes=FakePipedProcessLauncher(),
            slurm_processes=connector,
            agent_client_factory=lambda **_kwargs: FakeAgentClient(
                capabilities=AgentCapabilities(provider_session_resume=True), threads=threads
            ),
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(threads=threads),
            git_repository=_ProductGit().repository,
            # Run records in memory: hours of a run's virtual time take many commits.
            state_stores=FakeStateStores(),
        ),
    )

    async def exercise() -> RunResult:
        session.start()
        try:
            return await session.await_result()
        finally:
            session.close()

    return sim.run(exercise(), threads=threads), events


def _sbatch_count(connector: FakeConnector) -> int:
    return sum("sbatch" in shlex.split(command) for command in recorded_commands(connector.state))


def test_while_the_probe_job_waits_in_the_queue_no_measurement_reaches_the_scheduler(
    tmp_path: Path, sim: Sim
) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    connector.script(
        _probe_operation("slurm-probe-run"),
        states=(SlurmJobStatus.PENDING,),
        pending_reason="Resources",
    )

    result, events = _run(tmp_path, sim, connector, SkeletonStrategy.baseline_probe())

    # The run's deadline ended it; until then its baseline measurement waited for the root.
    assert result.status.value in {"failed", "stopped"}, result
    assert _sbatch_count(connector) == 1, recorded_commands(connector.state)
    assert any(event.type is CoreEventType.WORKSPACE_ROOT_PROBE for event in events)


def test_a_probe_that_failed_for_good_ends_the_run_as_a_failure_naming_the_probe(
    tmp_path: Path, sim: Sim
) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    remote = tmp_path / "remote"

    def hide_staging_from_the_node(
        request: dict[str, object], answer: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        # The probe job fails on its node: the staged workspace is gone when it runs.
        if request["operation"] == "exec" and " sbatch " in f" {request.get('command', '')} ":
            for workspace in remote.glob("*/*/workspace"):
                if workspace.is_symlink():
                    workspace.unlink()
                else:
                    shutil.rmtree(workspace)
        return answer(request)

    connector.intercept(hide_staging_from_the_node)

    result, events = _run(tmp_path, sim, connector, SkeletonStrategy.baseline_probe())

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind is RunFailureKind("run_environment_unavailable")
    assert "workspace_root` startup probe failed" in result.failure.reason
    failed = [event for event in events if event.type is CoreEventType.RUN_FAILED]
    assert len(failed) == 1
    # The probe's own job is all the run ever sent to the scheduler.
    assert _sbatch_count(connector) == 1


# The queue holds each probe attempt this long before the attempt gives up on it.
_QUEUE_SECONDS = 1800
# The probe deadline this PR used to have: a run whose probe was never scheduled for this
# long ended for good, though nothing was wrong with its root.
_OLD_PROBE_DEADLINE_SECONDS = 6 * 3600
# Probe attempts the full queue never schedules: with the backoff between them they span
# about 8 h, well past the old deadline. The next attempt is scheduled and proves the root.
_UNSCHEDULED_ATTEMPTS = 10
# The run's own deadline, the only bound on its wait for the probe: past all of the above.
_RUN_DEADLINE_SECONDS = 24 * 3600.0


@dataclass
class _Submissions:
    """Every ``sbatch`` the run sent, with the virtual time it was sent at."""

    clock: Callable[[], float]
    sent: list[tuple[float, str]] = field(default_factory=list)

    def watch(
        self, request: dict[str, object], answer: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        command = str(request.get("command", ""))
        if request["operation"] == "exec" and " sbatch " in f" {command} ":
            self.sent.append((self.clock(), command))
        return answer(request)

    def probes(self) -> list[float]:
        return [at for at, command in self.sent if "workspace-root-probe" in command]

    def work(self) -> list[float]:
        return [at for at, command in self.sent if "workspace-root-probe" not in command]


@pytest.mark.xfail(
    strict=True,
    reason="a probe the queue never scheduled ends the run at its 6 h probe deadline (#3243 N2)",
)
def test_a_queue_full_far_past_the_old_probe_deadline_holds_slurm_work_and_never_ends_the_run(
    tmp_path: Path, sim: Sim
) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    for generation in range(_UNSCHEDULED_ATTEMPTS):
        connector.script(
            _probe_operation("slurm-probe-run", generation),
            states=(SlurmJobStatus.PENDING,),
            pending_reason="Resources",
        )
    started = sim.clock.now()
    submissions = _Submissions(clock=lambda: sim.clock.now() - started)
    connector.intercept(submissions.watch)

    result, events = _run(
        tmp_path,
        sim,
        connector,
        SkeletonStrategy(),
        _Shape(
            slurm_extra=f"queue_timeout_seconds = {_QUEUE_SECONDS}\n",
            deadline_seconds=_RUN_DEADLINE_SECONDS,
            # Core looks at the held measurement every few minutes, not every 10 s: the hours
            # of virtual time then cost few durable steps. A look still credits its gap as
            # queue time, so the measurement's own limit never ends the wait.
            limits=sample_limits(
                observe_interval=600.0, observe_backoff_cap=1800.0, queue_notice_interval=3600.0
            ),
            # The loop renews its lease at a third of it and reads controls every poll: both
            # rarely, so hours of an idle run cost few loop steps.
            lease_seconds=3600.0,
            control_poll_seconds=300.0,
        ),
    )

    probes = submissions.probes()
    assert len(probes) == _UNSCHEDULED_ATTEMPTS + 1, submissions.sent
    freed_at = probes[-1]
    assert freed_at > _OLD_PROBE_DEADLINE_SECONDS + _QUEUE_SECONDS
    # Until the queue freed, the run sent the scheduler nothing but its probe.
    work = submissions.work()
    assert work, submissions.sent
    assert min(work) > freed_at
    # The run neither failed nor stopped while it waited: the proof released its held
    # measurements, and the run measured and ended on its own.
    assert not [event for event in events if event.type is CoreEventType.RUN_FAILED]
    assert result.succeeded, result
