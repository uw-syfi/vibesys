"""The run options the desktop app sets reach the run's configuration, and ``vibesys preflight``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.task_project import write_task

from entrypoints import cli
from entrypoints.cli.config import load_config_and_skills
from entrypoints.cli.constants import (
    MAX_RUN_SECONDS_FLAG,
    ROLE_MODEL_FLAG,
    ROLE_PROVIDER_FLAG,
    ROLE_REASONING_EFFORT_FLAG,
)
from vibesys.api import DomainName
from vibesys.api.request import run_environment_uses_docker
from vibesys.errors import ConfigurationError
from vibesys.inputs import InputBundle, load_task
from vs_project.api.testing import FakeGitRepositories
from vs_runtime.api.infrastructure import RunEnvironmentSpec
from vs_sim.api.testing import ScriptedProbe

if TYPE_CHECKING:
    from argparse import Namespace

    from vs_sim.api import ProbeResult


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--outer-loop",
        "agent",
        "--runs-dir",
        str(tmp_path / "runs"),
        "--input-objective",
        "Serve fast.",
        "--input-domain",
        "llm-serving",
        "--input-accuracy-command",
        "python checker.py",
        "--input-benchmark-command",
        "python benchmark.py",
        "--no-skills",
        *extra,
    ]


_ROLE = st.from_regex(r"[a-z][a-z0-9_-]{0,8}", fullmatch=True)


@given(
    seconds=st.integers(1, 10**6),
    models=st.dictionaries(_ROLE, st.from_regex(r"[a-z0-9.-]{1,12}", fullmatch=True), max_size=3),
    efforts=st.dictionaries(_ROLE, st.sampled_from(["low", "medium", "high"]), max_size=3),
)
def test_time_limit_and_role_flags_override_the_configuration(
    tmp_path_factory: pytest.TempPathFactory,
    seconds: int,
    models: dict[str, str],
    efforts: dict[str, str],
) -> None:
    tmp_path = tmp_path_factory.mktemp("run")
    flags = [MAX_RUN_SECONDS_FLAG, str(seconds)]
    for role, model in models.items():
        flags += [ROLE_MODEL_FLAG, f"{role}={model}"]
    for role, effort in efforts.items():
        flags += [ROLE_REASONING_EFFORT_FLAG, f"{role}={effort}"]
    args = cli.parse_cli_invocation(_argv(tmp_path, *flags)).args

    config, _skills, _backend = load_config_and_skills(args, domain=DomainName.LLM_SERVING)

    assert config.run.max_run_seconds == seconds
    assert {role: cfg.model for role, cfg in config.agent.roles.items() if cfg.model} == models
    assert {
        role: cfg.reasoning_effort
        for role, cfg in config.agent.roles.items()
        if cfg.reasoning_effort
    } == efforts


@pytest.mark.parametrize(
    "flags",
    [
        [MAX_RUN_SECONDS_FLAG, "0"],
        [MAX_RUN_SECONDS_FLAG, "soon"],
        [ROLE_MODEL_FLAG, "implementer"],
        [ROLE_REASONING_EFFORT_FLAG, "=high"],
    ],
)
def test_a_malformed_option_is_a_configuration_error(tmp_path: Path, flags: list[str]) -> None:
    with pytest.raises(ConfigurationError):
        cli.parse_cli_invocation(_argv(tmp_path, *flags))


def test_a_role_name_the_configuration_cannot_hold_is_refused_naming_the_key(
    tmp_path: Path,
) -> None:
    args = cli.parse_cli_invocation(_argv(tmp_path, ROLE_MODEL_FLAG, "Bad Role=x")).args

    with pytest.raises(ConfigurationError, match=r"agent\.roles"):
        load_config_and_skills(args, domain=DomainName.LLM_SERVING)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (RunEnvironmentSpec("docker"), True),
        (RunEnvironmentSpec("modal", {"gpu": "A10G"}), True),
        (RunEnvironmentSpec("host"), False),
    ],
    ids=["docker", "modal", "host"],
)
def test_whether_a_run_needs_docker_comes_from_its_environment(
    *, spec: RunEnvironmentSpec, expected: bool
) -> None:
    assert run_environment_uses_docker(spec) is expected


_GPU_ROW = "0, GPU-1, NVIDIA A100, 10, 40960, 3\n"


def _nvidia_smi(answer: ProbeResult | None) -> ScriptedProbe:
    """A host whose ``nvidia-smi`` answers ``answer`` and that runs nothing else."""
    return ScriptedProbe(lambda argv: answer if argv[0] == "nvidia-smi" else None)


def _parse(argv: list[str]) -> Namespace:
    return cli.parse_cli_invocation(argv).args


_DOCKER_INFO = ("docker", "info", "--format", "{{json .Runtimes}}")
_DOCKER_IN_DOCKER_TASK = """\
version = 1

[agent]
domain = "generic"

[environment]
docker_in_docker = true

[accuracy]
command = ["python", "-c", "print('ok')"]

[benchmark]
command = ["python", "-c", "print('1')"]
"""


def _docker_in_docker_project(root: Path) -> list[str]:
    """A project whose task declares ``docker_in_docker``; the flags that select it."""
    project = root / "project"
    task = project / ".vibesys" / "tasks" / "latency"
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Optimize latency.\n")
    (task / "vibesys.input.toml").write_text(_DOCKER_IN_DOCKER_TASK)
    return ["--project", str(project), "--task", "latency"]


def _docker_runtimes(answer: ProbeResult | None) -> ScriptedProbe:
    """A host whose ``docker info`` answers ``answer``; every other command is unavailable."""
    return ScriptedProbe(lambda argv: answer if argv == _DOCKER_INFO else None)


_QUEUE_RS = Path(__file__).parents[2] / "examples/data-structures/repositories/queue-rs"


def _queue_task(tmp_path: Path, *, build: bool) -> InputBundle:
    """queue-rs's mpmc task in a project of its own, with or without its declared build."""
    task = tmp_path / ".vibesys" / "tasks" / "mpmc"
    task.mkdir(parents=True)
    source = _QUEUE_RS / ".vibesys" / "tasks" / "mpmc"
    (task / "OBJECTIVE.md").write_text((source / "OBJECTIVE.md").read_text())
    manifest = (source / "vibesys.input.toml").read_text()
    if not build:
        start = manifest.index("[build]")
        manifest = manifest[:start] + manifest[manifest.index("[check]") :]
    (task / "vibesys.input.toml").write_text(manifest)
    return load_task(tmp_path, "mpmc")


def _project_declaring(tmp_path: Path, backends: str) -> Path:
    """A project whose one task declares ``backends`` (a TOML array body) and nothing else."""
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    write_task(
        project,
        f"""\
version = 1
backends = [{backends}]

[agent]
domain = "generic"

[accuracy]
command = ["python", "-c", "print('ok')"]

[benchmark]
command = ["python", "-c", "print('1')"]
""",
    )
    return project


def _slurm_flags(tmp_path: Path) -> list[str]:
    """The flags ``run.preflight`` adds for a run measured on a cluster, over a minimal operator file."""
    config = tmp_path / "operator-slurm.toml"
    config.write_text(
        '[slurm]\nname = "cluster-m"\nremote_workspace_root = "/remote/runs"\n'
        '[slurm.transport]\nkind = "ssh"\nhost = "host-a"\n',
        encoding="utf-8",
    )
    return ["--run-environment", "slurm", "--slurm-config", str(config)]


def _committed_project(tmp_path: Path) -> tuple[Path, FakeGitRepositories]:
    """A project that is its own repository with everything committed."""
    project = _project_declaring(tmp_path, '"cpu"')
    (project / ".git").mkdir()
    disk = FakeGitRepositories()
    repository = disk.repository(project)
    repository.initialize(initial_branch="main")
    repository.stage_all(["."])
    repository.commit("baseline")
    return project, disk


def test_a_role_provider_flag_reaches_the_roles_configuration(tmp_path: Path) -> None:
    flags = [
        ROLE_PROVIDER_FLAG,
        "implementer=claude",
        ROLE_MODEL_FLAG,
        "implementer=claude-opus-5",
    ]
    args = cli.parse_cli_invocation(_argv(tmp_path, *flags)).args

    config, _skills, _backend = load_config_and_skills(args, domain=DomainName.LLM_SERVING)

    assert config.agent.roles["implementer"].provider == "claude"
    assert config.agent.roles["implementer"].model == "claude-opus-5"
