"""A profile-guided attribution command is offered only where the run can execute it (#2118).

The strategy runs the manifest's `[profile_guided]` command through the run's command runner,
which is the agent's shell (the editor container under Slurm). That container binds no GPU and
lacks the task's environment, so under Slurm the command exited 127 in every capture. Run start
now asks the run environment, and refuses with an error naming the command and the reason
before any agent work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.task_project import write_task

from launch import validate_run_request
from vibesys.api import Config, ConfigurationError, OrchestrationDescriptor, RunRequest
from vibesys.api.request import RunEnvironmentSpec, load_task

if TYPE_CHECKING:
    from pathlib import Path

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
                "judge_every": 1,
                "official_eval_every": 1,
                "profile_guided": {"command": command},
            },
        ),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        exp_name="attribution-runnable",
        runs_dir=root / "runs",
        run_environment=spec,
    )


@settings(max_examples=25, deadline=None, derandomize=True, database=None)
@given(
    command=_COMMANDS,
    environment=st.sampled_from(_ENVIRONMENTS),
    loop=st.sampled_from(_LOOPS),
)
def test_an_attribution_command_is_runnable_where_offered_or_refused_by_name(
    tmp_path_factory: pytest.TempPathFactory, command: list[str], environment: str, loop: str
) -> None:
    command = ["/bin/true", *command[1:]] if environment == "host" else command
    request = _request(tmp_path_factory.mktemp("attr"), environment, loop, command)
    if environment not in _SLURM_ENVIRONMENTS:
        validate_run_request(request)
        return
    with pytest.raises(ConfigurationError) as error:
        validate_run_request(request)
    diagnostic = error.value.diagnostic
    assert diagnostic.code == "attribution_command_unavailable"
    assert diagnostic.stage == "profiler_validation"
    assert command[0] in diagnostic.message
    assert "editor container" in diagnostic.message
