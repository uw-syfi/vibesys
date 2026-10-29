"""A run that ends any way stops and removes every container it started.

Regression for #1521: stopping a headless run, or a run failing, left its agent
containers running. The run opens a real ``DockerSandbox`` over a fake Docker
daemon, so the assertion is on the daemon's own container list.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import pytest
from tests.support.core_plugins import capability_plugin
from tests.support.docker_environment import fake_docker_environment
from tests.support.real_git import REAL_PROCESSES
from tests.support.skeleton_strategy import (
    ATTEMPT,
    IMPLEMENTER_ROLE,
    OPERATIONS,
    SkeletonConfig,
    SkeletonStrategy,
)
from tests.support.task_project import write_task

from entrypoints.production import production_timing
from entrypoints.run import supervise
from headless import run as render_run
from launch import LaunchSettings, default_runs
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_task
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.plugin_catalog import OrchestrationRegistry
from vibesys.run.contracts import ResumeRef, RunRequest
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_core.api import AttemptBudget, ItemId, WorkspaceMode, WorkspacePlan
from vs_core.api.search import Flow, NotOpened, NoWinner, search
from vs_project.api import OrchestrationDescriptor, Project, ProjectStateError
from vs_sandbox.api import RUN_ID_LABEL, DockerSandbox
from vs_sandbox.api.testing import FakeContainer, FakeDockerEngine, HostExecutedContainerBackend
from vs_sim.api import OsThreads, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import FakePipedProcessLauncher, FakeSignalSource

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vs_sandbox.api import CommandRunner, HostResource, SandboxKind
from vs_sim.api import DISCARD

_PLUGIN_ID = "container-exit"


class _DaemonBackend(HostExecutedContainerBackend):
    """A backend whose Docker sandboxes talk to a :class:`FakeDockerEngine`."""

    engine: FakeDockerEngine

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
        del kind
        return DockerSandbox(
            host_workspace=host_workspace,
            image=container_image or "agent-image",
            resources=resources,
            docker=self.engine,
            run_id=run_id,
            threads=OsThreads(),
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


@dataclass
class _Resumed:
    """A run resumed after its host died, and the host's Docker client."""

    run_id: str
    host_docker: FakeDockerEngine


@dataclass
class _Observed:
    """What the orchestration saw of the daemon while the run was live."""

    run_id: str = ""
    containers: tuple[FakeContainer, ...] = ()


_RUN_DEADLINE = 1000.0


@search(
    "container-exit",
    config=SkeletonConfig,
    operations=OPERATIONS,
    roles=(IMPLEMENTER_ROLE,),
)
async def _probe(ctx: Flow, config: SkeletonConfig) -> NoWinner | None:
    """Open one attempt, so the run owns a container, then act on the exit path once it is live.

    Flows are pure, so the probe's side effects (reading the daemon, delivering the signals)
    reach the test through a module-level hook the test installs for one run.
    """
    del config
    async with ctx.workspaces.open(
        attempt=ATTEMPT.attempt_id,
        item=ItemId(root="item-0"),
        plan=WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=ctx.facts.baseline),
        budget=AttemptBudget(),
    ) as workspace:
        if isinstance(workspace, NotOpened):
            return NoWinner(reason="the attempt workspace did not open")
        if _LIVE_HOOK is None or _LIVE_HOOK() == "failed":
            return NoWinner(reason="scripted")
        # Signals were delivered: wait for the stop they cause; the timer keeps the shell from
        # calling the wait a stall.
        await ctx.first(ctx.stopped, ctx.timer(_RUN_DEADLINE))
    return None


_LIVE_HOOK: Callable[[], Literal["failed", "settle"]] | None = None
_RUN_ID = "container-exit-run"


def _live_hook(
    engine: FakeDockerEngine, observed: _Observed, signals: FakeSignalSource, exit_path: str
) -> Callable[[], Literal["failed", "settle"]]:
    """What happens once the run's container exists: note the daemon, then fail or signal."""

    def live() -> Literal["failed", "settle"]:
        observed.run_id = _RUN_ID
        observed.containers = engine.containers()
        if exit_path == "failure":
            return "failed"
        for name in exit_path.split("+"):
            assert signals.deliver(signal.Signals[name]), f"the run installed no {name} handler"
        return "settle"

    return live


def _execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    exit_path: str,
    ends_with: type[BaseException] | None = None,
    resumed: _Resumed | None = None,
) -> tuple[FakeDockerEngine, _Observed]:
    # The container receives the provider credential, so the run needs one to open.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    root = tmp_path / "project"
    resume = None if resumed is None else ResumeRef(run_id=resumed.run_id)
    host_docker = None if resumed is None else resumed.host_docker
    if resumed is None:
        _project(root)
    state = tmp_path / "engine"
    state.mkdir(exist_ok=True)
    engine = FakeDockerEngine(state, launcher=FakePipedProcessLauncher())
    if exit_path == "start-lost":
        engine.runs_lost_after_creating([False])

    observed = _Observed()
    signals = FakeSignalSource()
    ending_context = nullcontext() if ends_with is None else pytest.raises(ends_with)

    global _LIVE_HOOK  # noqa: PLW0603  # lint-waiver: LW-950003 [PLW0603]; a pure core strategy reaches the test only through this one hook, installed for one run.
    _LIVE_HOOK = None if resumed is not None else _live_hook(engine, observed, signals, exit_path)
    plugin = capability_plugin(
        _PLUGIN_ID,
        strategy=SkeletonStrategy.unmeasured(keeps_candidate=False),
        search=_probe,
    )
    registry = OrchestrationRegistry()
    registry.register_plugin(plugin)

    def backend_factory(
        _name: object,
        *,
        log_dir: Path,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
        clock: object,
        events: object | None = None,
    ) -> _DaemonBackend:
        del clock, events  # the fake backend records no timestamps and has no device to watch
        backend = _DaemonBackend(
            log_dir,
            log=log,
            image=image,
            threads=OsThreads(),
            launcher=FakePipedProcessLauncher(),
        )
        backend.engine = engine
        return backend

    request = RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(id=_PLUGIN_ID, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "claude-sonnet-5"}}),
        input_bundle=load_task(root),
        objective="Improve the queue.",
        exp_name=_PLUGIN_ID,
        run_id=_RUN_ID,
        agent_backend="cli",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
        run_environment=fake_docker_environment(host_docker),
        resume=resume,
    )
    runs = default_runs(
        LaunchSettings(
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            timing=production_timing(),
            processes=REAL_PROCESSES,
            piped_processes=FakePipedProcessLauncher(),
            registry=registry,
            agent_client_factory=lambda **_: FakeAgentClient(
                capabilities=AgentCapabilities(provider_session_resume=True), threads=OsThreads()
            ),
            backend_factory=backend_factory,
        )
    )

    async def execute() -> None:
        handle = runs.start(request) if resume is None else runs.resume(request)
        await supervise(handle, render_run(handle), signals=signals)

    try:
        with ending_context:
            asyncio.run(execute())
    finally:
        _LIVE_HOOK = None
    return engine, observed


# Each exit path ends the run its own typed way: a stop and a failure return normally, a
# terminating signal exits 128 + n. (Which signal maps to which code is `supervise`'s own rule,
# tested where it lives; the operator's stop here shows a signalled exit removes containers.)
# Budget: each case is a whole run over a fake Docker daemon, the point of the regression (#1521).
@pytest.mark.parametrize(
    ("exit_path", "ends_with"),
    [("failure", None), ("start-lost", RuntimeError), ("SIGINT", None)],
)
def test_an_exiting_run_leaves_no_container_and_every_container_carries_the_runs_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_path: str,
    ends_with: type[BaseException] | None,
) -> None:
    engine, observed = _execute(tmp_path, monkeypatch, exit_path=exit_path, ends_with=ends_with)

    assert engine.containers() == ()
    # The daemon saw the run start its container.
    assert any(call[1] == "run" for call in engine.calls)
    if exit_path != "start-lost":
        assert observed.run_id
        assert observed.containers
        assert all(
            container.labels == {RUN_ID_LABEL: observed.run_id} for container in observed.containers
        )


def test_resuming_a_run_ends_only_the_containers_its_dead_host_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose host died ends its containers; a resume refused for a live host recovers
    nothing; a resume that is accepted removes the run's own orphans and leaves other runs'.

    The first run must not end (a lost container start raises out of it before core records
    an end): a run that ended is refused before anything boots, so it recovers nothing.
    """
    engine, _ = _execute(tmp_path, monkeypatch, exit_path="start-lost", ends_with=RuntimeError)
    assert engine.containers() == ()
    host_docker = FakeDockerEngine(launcher=FakePipedProcessLauncher())
    own = _plant_container(host_docker, _RUN_ID)
    other = _plant_container(host_docker, "some-other-run")
    calls_before_resume = tuple(host_docker.calls)
    project = Project.open(tmp_path / "project")

    with project.state.exclusive_run_host(_RUN_ID):
        _execute(
            tmp_path,
            monkeypatch,
            exit_path="SIGINT",
            ends_with=ProjectStateError,
            resumed=_Resumed(_RUN_ID, host_docker),
        )
    assert {c.container_id for c in host_docker.containers()} == {own, other}
    assert tuple(host_docker.calls) == calls_before_resume

    _execute(
        tmp_path,
        monkeypatch,
        exit_path="SIGINT",
        resumed=_Resumed(_RUN_ID, host_docker),
    )

    assert [c.container_id for c in host_docker.containers()] == [other]
    assert ("docker", "rm", "-f", own) in host_docker.calls


def _plant_container(engine: FakeDockerEngine, run_id: str) -> str:
    label = f"{RUN_ID_LABEL}={run_id}"
    result = engine.run(
        ["docker", "run", "-d", "--label", label, "agent-image"], timeout_seconds=1, output=DISCARD
    )
    return result.stdout.strip()
