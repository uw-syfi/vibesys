"""A finished run's reader resources: assembled from its saved state, changing nothing of it."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from tests.support.docker_environment import fake_docker_environment
from tests.support.host_environment import unconfined_host_spec
from tests.support.real_git import NO_GIT, NO_GITHUB
from tests.support.slurm_environment import slurm_environment
from tests.support.task_project import write_task

from vibesys.api.request import RunEnvironmentSpec, load_task
from vibesys.composition import _vibesys_runtime_host_resources
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import ResumeRef, RunRequest
from vibesys.run.integration import LocalRunIntegration
from vibesys.run.reader import NotAResumeError, open_reader_resources
from vs_project.api import OrchestrationDescriptor, Project, ProjectStateError, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
from vs_runtime.api.testing import idle_environment_system
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

RUN_ID = "saved-run"
_KINDS: dict[str, tuple[Callable[[Path], RunEnvironmentSpec], bool]] = {
    # name: (spec, whether the environment holds the agent in a container)
    "host": (lambda _directory: unconfined_host_spec(), False),
    "docker": (lambda _directory: fake_docker_environment(), True),
    "slurm": (slurm_environment, True),
}


def _backend(*_args: object, **_kwargs: object) -> FakeComputeBackend:
    return FakeComputeBackend(threads=SimThreads())


def _saved_run(root: Path, kind: str) -> RunRequest:
    """A project holding one finished run, and the request that resuming it restores."""
    root.mkdir()
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Make it faster.\n",
    )
    project = Project.open(root)
    when = datetime(2026, 1, 1, tzinfo=UTC)
    project.state.create_project("saved", now=when)
    project.state.create_run(
        project.state.new_run_manifest(
            "saved",
            run_id=RUN_ID,
            branch=f"vibesys-runs/{RUN_ID}",
            vibesys_version="test",
            run_environment=RunEnvironmentRecord(name="docker" if kind != "slurm" else "slurm"),
            execution=run_execution_record(),
            orchestration=OrchestrationDescriptor(id="stub", config_version=1, options={}),
            trusted_input_baseline="0" * 40,
            now=when,
        )
    )
    spec_of, _ = _KINDS[kind]
    return RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(id="stub", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "gpt-test"}, "agent": {"backend": "stub"}}),
        input_bundle=load_task(root),
        resume=ResumeRef(run_id=RUN_ID),
        run_environment=spec_of(root.parent),
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
        agent_backend="cli",
    )


def _integration() -> LocalRunIntegration:
    return LocalRunIntegration(
        clock=ManualClock(),
        git=NO_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )


def _digest(root: Path) -> dict[str, str]:
    """Every file under ``root`` (the project and its git history) by content."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("kind", list(_KINDS))
def test_an_environment_kind_assembles_reader_resources_and_leaves_the_run_as_it_was(
    tmp_path: Path, kind: str
) -> None:
    request = _saved_run(tmp_path / "project", kind)
    project = tmp_path / "project"
    before = _digest(project)

    resources = open_reader_resources(
        request, _integration(), agent_specs={}, backend_factory=_backend
    )

    assert resources.run_id == RUN_ID
    assert resources.environment_request.workspace == project
    assert resources.environment_request.run_id == RUN_ID
    assert resources.environment_request.agent_backend == "cli"
    # An agent in a container brings its own resources; one on the host needs the framework's.
    _, in_container = _KINDS[kind]
    assert resources.host_resources == (() if in_container else _vibesys_runtime_host_resources())
    # The saved run, its journal and its git history are byte for byte what they were.
    assert _digest(project) == before
    # Nothing holds the run: a run may resume (it takes the write lease) while the reader lives.
    with resources.project.state.exclusive_run_host(RUN_ID):
        pass


def test_the_reader_of_a_container_run_mounts_the_private_socket_directory_of_its_tools(
    tmp_path: Path,
) -> None:
    request = _saved_run(tmp_path / "project", "docker")

    resources = open_reader_resources(
        request, _integration(), agent_specs={}, backend_factory=_backend
    )

    [mount] = resources.environment_request.environment_bind_mounts
    assert mount.read_only is False
    assert mount.host_path.name  # the run's own directory, named by project and run


def test_a_request_that_resumes_no_run_has_no_reader(tmp_path: Path) -> None:
    request = _saved_run(tmp_path / "project", "host").model_copy(update={"resume": None})

    with pytest.raises(NotAResumeError):
        open_reader_resources(request, _integration(), agent_specs={}, backend_factory=_backend)


def test_a_run_the_project_does_not_record_has_no_reader(tmp_path: Path) -> None:
    request = _saved_run(tmp_path / "project", "host").model_copy(
        update={"resume": ResumeRef(run_id="never-ran")}
    )

    with pytest.raises(ProjectStateError):
        open_reader_resources(request, _integration(), agent_specs={}, backend_factory=_backend)
