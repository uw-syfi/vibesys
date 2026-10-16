"""The run options the desktop app sets reach the run's configuration, and ``vibesys preflight``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from entrypoints import cli
from entrypoints.cli.config import load_config_and_skills
from entrypoints.cli.preflight import (
    ACCELERATOR_UNAVAILABLE,
    AGENT_IMAGES_COMMAND,
    TASK_IMAGE_UNSUPPORTED,
    agent_image_check,
    preflight_report,
    valid_report,
)
from server.host_api import MAX_RUN_SECONDS_FLAG, ROLE_MODEL_FLAG, ROLE_REASONING_EFFORT_FLAG
from server.host_preflight import CheckId, CheckState, SettingsInvalid
from vibesys.api import DomainName
from vibesys.api.request import TaskImageError, run_environment_uses_docker
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vs_runtime.api.infrastructure import RunEnvironmentSpec
from vs_sim.api import ProbeResult
from vs_sim.api.testing import ScriptedProbe

if TYPE_CHECKING:
    from argparse import Namespace


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


def test_an_invalid_configuration_is_reported_and_nothing_else_is_checked() -> None:
    diagnostic = ConfigurationDiagnostic(
        code="agent_role_configuration_invalid",
        stage="agent_configuration_validation",
        message="agent configuration names roles not declared: agent.roles.bogus",
    )

    def parse(_argv: list[str]) -> Namespace:
        raise ConfigurationError(diagnostic)

    probe = ScriptedProbe(lambda _argv: None)
    report = preflight_report(["--role-model", "bogus=x"], parse, probe)

    assert report.settings == SettingsInvalid(
        code=diagnostic.code, stage=diagnostic.stage, message=diagnostic.message
    )
    assert report.agent_image.state is CheckState.WARN
    assert report.evaluator.state is CheckState.WARN
    assert probe.calls == []


@pytest.mark.parametrize(
    ("answer", "state"),
    [
        (ProbeResult(0, "sha256:abc\n"), CheckState.OK),
        (ProbeResult(0, ""), CheckState.WARN),
        (ProbeResult(1, "", "Cannot connect"), CheckState.WARN),
        (None, CheckState.WARN),
    ],
    ids=["cached", "never-built", "docker-down", "docker-missing"],
)
def test_the_agent_image_is_ok_once_built_here_and_a_warning_before(
    answer: ProbeResult | None, state: CheckState
) -> None:
    check = agent_image_check(
        ScriptedProbe(lambda argv: answer if argv == AGENT_IMAGES_COMMAND else None)
    )

    assert check.id is CheckId.AGENT_IMAGE
    assert check.state is state


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


def test_a_run_that_needs_no_docker_needs_no_agent_image(tmp_path: Path) -> None:
    """Regression: the agent image was checked, through Docker, for a run that starts no container."""
    args = cli.parse_cli_invocation(_argv(tmp_path)).args
    probe = ScriptedProbe(lambda _argv: None)

    report = valid_report(uses_docker=False, bundle=args.input_bundle, probe=probe)

    assert (report.uses_docker, report.agent_image.state) == (False, CheckState.OK)
    assert report.agent_image.detail.startswith("not needed")
    assert probe.calls == []


_GPU_ROW = "0, GPU-1, NVIDIA A100, 10, 40960, 3\n"


def _nvidia_smi(answer: ProbeResult | None) -> ScriptedProbe:
    """A host whose ``nvidia-smi`` answers ``answer`` and that runs nothing else."""
    return ScriptedProbe(lambda argv: answer if argv[0] == "nvidia-smi" else None)


def _parse(argv: list[str]) -> Namespace:
    return cli.parse_cli_invocation(argv).args


@pytest.mark.parametrize(
    "answer",
    [None, ProbeResult(0, ""), ProbeResult(9, "", "NVIDIA-SMI has failed"), ProbeResult(0, "junk")],
    ids=["no-nvidia-smi", "no-gpu", "driver-error", "unreadable"],
)
def test_a_cuda_run_on_a_host_without_a_gpu_fails_preflight_naming_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: ProbeResult | None
) -> None:
    """Regression: preflight passed, then the run stopped at launch with no GPU for ``cuda``."""
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    report = preflight_report(_argv(tmp_path, "--backend", "cuda"), _parse, _nvidia_smi(answer))

    assert isinstance(report.settings, SettingsInvalid)
    assert report.settings.code == ACCELERATOR_UNAVAILABLE
    assert "--backend cpu" in report.settings.message
    assert "\n" not in report.settings.message


@pytest.mark.parametrize(
    ("flags", "answer"),
    [
        (["--backend", "cuda"], ProbeResult(0, _GPU_ROW)),
        (["--backend", "cpu", "--profiler", "none"], None),
    ],
    ids=["cuda-with-gpu", "cpu"],
)
def test_a_run_whose_accelerator_is_usable_passes_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str], answer: ProbeResult | None
) -> None:
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    report = preflight_report(_argv(tmp_path, *flags), _parse, _nvidia_smi(answer))

    assert report.settings.valid is True


def test_a_pinned_cuda_device_is_trusted_as_the_run_trusts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")

    report = preflight_report(_argv(tmp_path, "--backend", "cuda"), _parse, _nvidia_smi(None))

    assert report.settings.valid is True


def test_a_task_image_the_run_refuses_is_reported_as_invalid_settings() -> None:
    """Regression: a refused task Dockerfile (a ValueError) crashed ``vibesys preflight``."""
    refusal = TaskImageError.operator_image(Path("/task/Dockerfile"), "custom:1")

    def parse(_argv: list[str]) -> Namespace:
        raise refusal

    probe = ScriptedProbe(lambda _argv: None)
    report = preflight_report(["--docker-image", "custom:1"], parse, probe)

    assert report.settings == SettingsInvalid(
        code=TASK_IMAGE_UNSUPPORTED, stage="run_environment", message=str(refusal)
    )
    assert probe.calls == []


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


@pytest.mark.parametrize(
    "answer",
    [
        ProbeResult(0, '{"runc": {}}'),
        ProbeResult(1, "", "Cannot connect to the Docker daemon"),
        ProbeResult(0, "junk"),
        None,
    ],
    ids=["no-sysbox", "daemon-down", "unreadable", "no-docker"],
)
def test_a_docker_in_docker_run_on_a_host_without_sysbox_fails_preflight(
    tmp_path: Path, answer: ProbeResult | None
) -> None:
    """Regression: preflight passed, then the run stopped seconds after Start on Sysbox."""
    argv = _docker_in_docker_project(tmp_path)

    report = preflight_report(argv, _parse, _docker_runtimes(answer))

    assert isinstance(report.settings, SettingsInvalid)
    assert report.settings.code == ACCELERATOR_UNAVAILABLE
    assert "sysbox-runc" in report.settings.message
    assert "\n" not in report.settings.message


def test_a_docker_in_docker_run_on_a_host_with_sysbox_passes_preflight(tmp_path: Path) -> None:
    argv = _docker_in_docker_project(tmp_path)

    report = preflight_report(
        argv, _parse, _docker_runtimes(ProbeResult(0, '{"runc": {}, "sysbox-runc": {}}'))
    )

    assert report.settings.valid is True
