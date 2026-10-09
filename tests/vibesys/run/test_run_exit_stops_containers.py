"""A run that ends any way stops and removes every container it started.

Regression for #1521: stopping a headless run, or a run failing, left its agent
containers running. The run opens a real ``DockerSandbox`` over a fake Docker
daemon, so the assertion is on the daemon's own container list.
"""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from tests.support.docker_environment import fake_docker_environment
from tests.vibesys.orchestration.plugin import EmptyOptions

from entrypoints.run import supervise
from headless import run as render_run
from launch import LaunchSettings, default_runs
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.plugin_catalog import OrchestrationRegistry
from vibesys.run.contracts import RunRequest
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import OrchestrationPlugin, RunStatus
from vs_sandbox.api import RUN_ID_LABEL, DockerSandbox
from vs_sandbox.api.testing import FakeContainer, FakeDockerEngine, HostExecutedContainerBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vs_runtime.api import Run
    from vs_sandbox.api import CommandRunner, HostResource, SandboxKind

_PLUGIN_ID = "container-exit"


class _OrchestrationFailedError(RuntimeError):
    """The orchestration's own failure."""


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
        )


def _project(root: Path) -> None:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n'
        '[benchmark]\ncommand = ["run-benchmark"]\nresult_protocol = 2\n'
    )


@dataclass
class _Observed:
    """What the orchestration saw of the daemon while the run was live."""

    run_id: str = ""
    containers: tuple[FakeContainer, ...] = ()


async def _run_until_exit(
    run: Run, engine: FakeDockerEngine, observed: _Observed, *, exit_path: str
) -> RunStatus:
    """Fail, or raise the signals of *exit_path* and then wait to be unwound."""
    observed.run_id = run.run_id
    observed.containers = engine.containers()
    if exit_path == "failure":
        raise _OrchestrationFailedError
    for name in exit_path.split("+"):
        os.kill(os.getpid(), getattr(signal, name))
    while True:
        await run.control.checkpoint()
        await asyncio.sleep(0)


def _execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    exit_path: str,
    ends_with: type[BaseException] | None = None,
) -> tuple[FakeDockerEngine, _Observed]:
    # The container receives the provider credential, so the run needs one to open.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    root = tmp_path / "project"
    _project(root)
    state = tmp_path / "engine"
    state.mkdir()
    engine = FakeDockerEngine(state)
    if exit_path == "start-lost":
        engine.runs_lost_after_creating([False])

    observed = _Observed()
    ending_context = nullcontext() if ends_with is None else pytest.raises(ends_with)

    async def orchestrate(run: Run, _options: object) -> RunStatus:
        return await _run_until_exit(run, engine, observed, exit_path=exit_path)

    plugin = OrchestrationPlugin(
        id=_PLUGIN_ID, agents=(), options=EmptyOptions, orchestrate=orchestrate
    )
    registry = OrchestrationRegistry()
    registry.register_plugin(plugin)

    def backend_factory(
        _name: object,
        *,
        log_dir: Path,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
    ) -> _DaemonBackend:
        backend = _DaemonBackend(log_dir, log=log, image=image)
        backend.engine = engine
        return backend

    request = RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(id=_PLUGIN_ID, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": _PLUGIN_ID}}),
        input_bundle=load_input_bundle(root),
        objective="Improve the queue.",
        exp_name=_PLUGIN_ID,
        agent_backend="cli",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
        run_environment=fake_docker_environment(),
    )
    runs = default_runs(
        LaunchSettings(
            registry=registry,
            agent_client_factory=lambda **_: None,  # ty: ignore[invalid-argument-type]
            backend_factory=backend_factory,
        )
    )

    async def execute() -> None:
        handle = runs.start(request)
        await supervise(handle, render_run(handle))

    with ending_context:
        asyncio.run(execute())
    return engine, observed


# Each exit path ends the run its own typed way: a stop returns normally, a
# failure raises the orchestration's error, a terminating signal exits 128 + n.
@pytest.mark.parametrize(
    ("exit_path", "ends_with"),
    [
        ("failure", _OrchestrationFailedError),
        ("SIGINT", None),
        ("SIGTERM", SystemExit),
        ("SIGHUP", SystemExit),
        ("SIGINT+SIGTERM", SystemExit),
        ("start-lost", RuntimeError),
    ],
)
def test_an_exiting_run_leaves_no_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_path: str,
    ends_with: type[BaseException] | None,
) -> None:
    engine, _observed = _execute(tmp_path, monkeypatch, exit_path=exit_path, ends_with=ends_with)

    assert engine.containers() == ()
    # The daemon saw the run start its container.
    assert any(call[1] == "run" for call in engine.calls)


def test_every_container_of_a_run_carries_the_runs_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine, observed = _execute(
        tmp_path, monkeypatch, exit_path="failure", ends_with=_OrchestrationFailedError
    )

    assert observed.run_id
    assert [container.labels for container in observed.containers] == [
        {RUN_ID_LABEL: observed.run_id}
    ]
