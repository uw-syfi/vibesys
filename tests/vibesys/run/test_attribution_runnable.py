"""A profile-guided attribution command is refused at start when its site lacks the executable (#2118).

The strategy runs the manifest's `[profile_guided]` command through the run's command runner.
Run start asks the run environment whether the executable exists at that site. An environment
that can inspect the site (the host) refuses an absent executable, naming the command. One that
cannot (Docker, Slurm's editor container) assumes it: v14's passing run (pg5, `python3
attribute.py`, stdlib only) must still start, and v14's pg1 (`bash py.sh`, whose script called an
interpreter missing from the container) cannot be told apart at start, so it fails on its first
capture, which names the command and where it ran (see the strategies' tests).
"""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.docker_environment import host_container_backend
from tests.support.real_git import REAL_GIT, REAL_PROCESSES
from tests.support.task_project import write_task

from entrypoints.production import production_timing
from launch import LaunchSettings, create_session, validate_run_request
from vibesys.api import Config, ConfigurationError, OrchestrationDescriptor, RunRequest
from vibesys.api.request import RunEnvironmentSpec, load_task
from vs_sim.api import ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import FakePipedProcessLauncher, examples

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import CoreEvent

_SLURM_LOCAL = """[slurm]
name = "test-cluster"
remote_workspace_root = "/shared/vibesys"

[slurm.transport]
kind = "local"
shell_command = ["bash", "-c"]
"""
_SLURM_SSH = """[slurm]
name = "test-cluster"
remote_workspace_root = "/remote/vibesys"

[slurm.transport]
kind = "ssh"
host = "test-cluster"

[vibesys]
remote_python = "/remote/venv/bin/python"
"""
_AGENT_GPU = """
[vibesys.agent_gpu]
partitions = ["main"]
max_gpus = 4
max_time_minutes = 90
"""
_OPERATOR = {
    "slurm-local": _SLURM_LOCAL,
    "slurm-ssh": _SLURM_SSH,
    "slurm-agent-gpu": _SLURM_LOCAL + _AGENT_GPU,
}
_SLURM_ENVIRONMENTS = frozenset(_OPERATOR)
_ENVIRONMENTS = ("host", "docker", *_OPERATOR)
_LOOPS = ("profile-guided-single-agent", "profile-guided-multi-agent")
_COMMANDS = st.lists(
    st.text("abcdefghijklmnopqrstuvwxyz-_./", min_size=1, max_size=12), min_size=1, max_size=4
)


def _request(root: Path, environment: str, loop: str, command: list[str]) -> RunRequest:
    project = root / "project"
    project.mkdir(exist_ok=True)
    write_task(
        project,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
    )
    spec = RunEnvironmentSpec(environment)
    if environment in _OPERATOR:
        config = root / f"{environment}.toml"
        config.write_text(_OPERATOR[environment], encoding="utf-8")
        spec = RunEnvironmentSpec("slurm", {"config_path": str(config)})
    return RunRequest(
        project_root=project,
        input_bundle=load_task(project),
        orchestration=OrchestrationDescriptor(
            id=loop,
            config_version=1,
            options={
                "interface": "inprocess",
                "max_rounds": 1,
                "max_retries_per_round": 1,
                # Only the multi search has a judge to ask every so often.
                **({"judge_every": 1} if "multi" in loop else {}),
                "profile_guided": {"command": command},
            },
        ),
        config=Config.model_validate({"model": {"name": "gpt-5.5"}}),
        exp_name="attribution-runnable",
        runs_dir=root / "runs",
        run_environment=spec,
    )


@settings(max_examples=examples(pr=10, nightly=25), deadline=None, derandomize=True, database=None)
@given(
    command=_COMMANDS,
    environment=st.sampled_from(_ENVIRONMENTS),
    loop=st.sampled_from(_LOOPS),
)
def test_an_attribution_command_is_accepted_or_refused_by_name_at_start(
    tmp_path_factory: pytest.TempPathFactory, command: list[str], environment: str, loop: str
) -> None:
    request = _request(tmp_path_factory.mktemp("attr"), environment, loop, command)
    inspectable = environment == "host" and os.sep not in command[0]
    present = inspectable and shutil.which(command[0]) is not None
    if not inspectable or present:
        validate_run_request(request, git=REAL_GIT)
        return
    with pytest.raises(ConfigurationError) as error:
        validate_run_request(request, git=REAL_GIT)
    diagnostic = error.value.diagnostic
    assert diagnostic.code == "attribution_command_unavailable"
    assert diagnostic.stage == "profiler_validation"
    assert command[0] in diagnostic.message


@pytest.mark.parametrize("environment", sorted(_SLURM_ENVIRONMENTS))
def test_a_stdlib_script_command_starts_under_every_slurm_mode(
    tmp_path: Path, environment: str
) -> None:
    """v14 pg5: `python3 attribute.py` ran in the editor container and must stay accepted."""
    request = _request(
        tmp_path, environment, "profile-guided-single-agent", ["python3", "attribute.py"]
    )
    validate_run_request(request, git=REAL_GIT)


def test_a_host_run_refuses_a_command_whose_interpreter_is_missing(tmp_path: Path) -> None:
    """v14 pg1's shape on the host, where the site can be inspected: refused before any agent."""
    request = _request(
        tmp_path, "host", "profile-guided-single-agent", ["no-such-interpreter", "attribute.py"]
    )
    with pytest.raises(ConfigurationError, match="no-such-interpreter"):
        validate_run_request(request, git=REAL_GIT)


def test_session_construction_refuses_a_host_command_whose_interpreter_is_missing(
    tmp_path: Path,
) -> None:
    """The session path, which parses options before this check, refuses it just as start does."""
    request = _request(
        tmp_path, "host", "profile-guided-single-agent", ["no-such-interpreter", "attribute.py"]
    )
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    with pytest.raises(ConfigurationError) as error:
        create_session(
            request,
            sink=record,
            settings=LaunchSettings(
                timing=production_timing(),
                processes=REAL_PROCESSES,
                piped_processes=FakePipedProcessLauncher(),
                backend_factory=host_container_backend,
                blocking=ThreadBlockingRunner(),
                network=UnixNetwork(),
            ),
        )
    assert error.value.diagnostic.code == "attribution_command_unavailable"
    assert not events
