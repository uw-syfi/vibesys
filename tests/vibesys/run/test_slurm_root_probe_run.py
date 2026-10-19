"""A run on a local Slurm root holds its Slurm work for the root's probe, and ends on its failure.

The root session starts the workspace-root probe beside the run (a 1-CPU Slurm job proving
the compute nodes share the run's root). These tests drive whole runs through the public
session, on a Fake cluster and in virtual time:

- while the probe job waits in the queue, the run's measurement reaches no scheduler: the
  run environment hands the probe to the cluster facts, and the host hands it to the
  evaluation executor;
- a probe that failed for good (its job failed) ends the run at once, as a failure that
  names the probe, instead of letting the run spend its agents' turns on work the cluster
  will refuse.
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
from functools import partial
from typing import TYPE_CHECKING

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


def _probe_operation(run_id: str) -> str:
    # test-isolation: the probe's scheduler identity, which the Fake scripts by operation id.
    return "workspace-root-probe-" + hashlib.sha256(run_id.encode()).hexdigest()[:32]


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


def _request(tmp_path: Path) -> RunRequest:
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
    config.write_text(_SLURM_LOCAL.format(remote=remote), encoding="utf-8")
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


def _run(
    tmp_path: Path, sim: Sim, connector: FakeConnector, strategy: SkeletonStrategy
) -> tuple[RunResult, list[CoreEvent]]:
    """Run the skeleton ``strategy`` to its end on the local Slurm root behind ``connector``."""
    request = _request(tmp_path)
    registry = OrchestrationRegistry()
    registry.register_plugin(
        capability_plugin(
            _PLUGIN_ID,
            agents=(AgentRole(id=IMPLEMENTER.root, system_prompt="Implement."),),
            strategy=strategy,
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
            timing=RunTiming(sim.clock, 60.0, threads),
            processes=FakeProcesses(no_origin, pass_time=threads.sleep),
            piped_processes=FakePipedProcessLauncher(),
            slurm_processes=connector,
            agent_client_factory=lambda **_kwargs: FakeAgentClient(
                capabilities=AgentCapabilities(provider_session_resume=True), threads=threads
            ),
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(threads=threads),
            git_repository=_ProductGit().repository,
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
