"""A process that relaunches a paused run lifts the pause: the operator's resume, taken by the host.

The product host opens an existing run as the operator's resume of it, so a run the operator
paused (and that parked) does not stay parked when it is relaunched. These cases drive whole runs
through the public session, in virtual time over Fakes: a run that asks for nothing until its
clock reads a moment, paused by the operator and relaunched over the same project, as many times as
the case says. Only a relaunch that lifts the pause lets the run reach that moment and end.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tests.support.container_credentials import set_container_cli_credentials
from tests.support.core_plugins import capability_plugin
from tests.support.docker_environment import fake_docker_environment
from tests.support.skeleton_strategy import IMPLEMENTER, SkeletonStrategy
from tests.support.task_project import write_task

from launch import LaunchSettings, create_session
from vibesys.api import (
    ComputeBackend,
    Config,
    CoreEvent,
    OrchestrationDescriptor,
    OrchestrationRegistry,
    ProfilerKind,
    ResumeRef,
    RunRequest,
    RunResult,
    RunStatus,
)
from vibesys.api.request import load_task
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_project.api.testing import FakeGitRepositories
from vs_runtime.api import AgentRole
from vs_runtime.api.core import RunTiming
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    FakeProcesses,
    ProcessScript,
    SimBlockingRunner,
    SimNetwork,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vs_sim.api import ProcessSpec
    from vs_sim.api.testing import Sim

_PLUGIN_ID = "relaunch-lifts-pause"
_RUN_ID = "relaunch-run"
_ENDS_AT = 30.0
"""The run clock reading at which the run, once running, ends by itself."""
_PAUSE_AFTER_S = 2.0
"""How long into each process the operator pauses it: well before ``_ENDS_AT``."""


def _request(root: Path, *, resumed: bool) -> RunRequest:
    return RunRequest(
        project_root=root,
        run_id=_RUN_ID,
        orchestration=OrchestrationDescriptor(id=_PLUGIN_ID, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_task(root),
        exp_name=_RUN_ID,
        run_environment=fake_docker_environment(),
        agent_backend="stub",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
        resume=ResumeRef(run_id=_RUN_ID) if resumed else None,
    )


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "queue.py").write_text("VALUE = 1\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Improve the queue.\n",
    )
    return root


def _process(
    root: Path, sim: Sim, disk: FakeGitRepositories, *, resumed: bool, pause: bool
) -> RunResult:
    """One process over the project: the operator pauses it when ``pause``, else it runs on."""
    registry = OrchestrationRegistry()
    registry.register_plugin(
        capability_plugin(
            _PLUGIN_ID,
            agents=(AgentRole(id=IMPLEMENTER.root, system_prompt="Implement."),),
            strategy=SkeletonStrategy.waiting_until(_ENDS_AT),
            deadline_seconds=1000.0,
        )
    )
    threads = sim.threads()

    def no_origin(spec: ProcessSpec) -> ProcessScript:
        assert spec.argv[-3:] == ("remote", "get-url", "origin")
        return ProcessScript(returncode=128, stderr=b"fatal: no origin")

    events: list[CoreEvent] = []
    session = create_session(
        _request(root, resumed=resumed),
        sink=events.append,
        registry=registry,
        settings=LaunchSettings(
            blocking=SimBlockingRunner(threads),
            network=SimNetwork(threads),
            timing=RunTiming(sim.clock, threads),
            processes=FakeProcesses(no_origin, pass_time=threads.sleep),
            piped_processes=FakePipedProcessLauncher(),
            agent_client_factory=lambda **_kwargs: FakeAgentClient(
                capabilities=AgentCapabilities(provider_session_resume=True), threads=threads
            ),
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(threads=threads),
            git_repository=disk.repository,
        ),
    )

    async def pause_later() -> None:
        await sim.clock.sleep(_PAUSE_AFTER_S)
        session.pause()

    async def exercise() -> RunResult:
        session.start()
        pauser = asyncio.ensure_future(pause_later()) if pause else None
        try:
            return await session.await_result()
        finally:
            if pauser is not None:
                pauser.cancel()
            session.close()

    return sim.run(exercise(), threads=threads)


def test_every_relaunch_of_a_paused_run_lifts_the_pause_and_the_last_one_ends_the_run(
    tmp_path: Path, sim: Sim, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pause, relaunch, pause the relaunch, relaunch again: each relaunch resumes, the last ends.

    Two pauses, not one: a relaunch that names its resume the same in every process is refused
    the second time and leaves the run parked, which one pause does not show.
    """
    set_container_cli_credentials(monkeypatch)
    root = _project(tmp_path)
    disk = FakeGitRepositories()

    first = _process(root, sim, disk, resumed=False, pause=True)
    second = _process(root, sim, disk, resumed=True, pause=True)
    last = _process(root, sim, disk, resumed=True, pause=False)

    assert [first.status, second.status] == [RunStatus.PARKED, RunStatus.PARKED]
    assert last.status is not RunStatus.PARKED
