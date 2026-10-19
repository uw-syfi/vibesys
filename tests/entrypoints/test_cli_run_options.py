"""The run options the desktop app sets reach the run's configuration, and ``vibesys preflight``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.fake_github import signed_in_github
from tests.support.task_project import DEFAULT_TASK, task_path, write_task

from entrypoints import cli
from entrypoints.cli.config import load_config_and_skills
from entrypoints.cli.preflight import (
    ACCELERATOR_UNAVAILABLE,
    AGENT_IMAGES_COMMAND,
    agent_image_check,
    evaluator_check,
    valid_report,
)
from entrypoints.cli.preflight import preflight_report as _preflight_report
from launch import built_in_orchestrations
from server.host_api import (
    MAX_RUN_SECONDS_FLAG,
    ROLE_MODEL_FLAG,
    ROLE_PROVIDER_FLAG,
    ROLE_REASONING_EFFORT_FLAG,
)
from server.host_preflight import (
    Backend,
    BackendUnavailable,
    CheckId,
    CheckState,
    ConfigInvalid,
    DeclareBuild,
    PreflightReport,
    SettingsInvalid,
    SettingsValid,
    SysboxMissing,
    TaskImageUnsupported,
)
from vibesys.api import ComputeBackend, DomainName
from vibesys.api.request import run_environment_uses_docker
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.inputs import InputBundle, load_task
from vs_project.api.testing import FakeGitRepositories
from vs_runtime.api.infrastructure import RunEnvironmentSpec
from vs_sim.api import ProbeResult
from vs_sim.api.testing import ScriptedProbe

if TYPE_CHECKING:
    from argparse import Namespace
    from collections.abc import Callable

    from vs_project.api import GitRepository


def preflight_report(
    argv: list[str],
    parse: Callable[[list[str]], Namespace],
    probe: ScriptedProbe,
    repository: Callable[[Path], GitRepository] | None = None,
) -> PreflightReport:
    """The report for a host whose GitHub CLI is signed in, so no test starts a real ``gh``."""
    return _preflight_report(argv, parse, probe, repository, github=signed_in_github().cli)


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

    report = valid_report(
        uses_docker=False, bundle=args.input_bundle, probe=probe, roles=(), backend=Backend.CPU
    )

    assert (report.uses_docker, report.agent_image.state) == (False, CheckState.OK)
    assert report.agent_image.detail.startswith("not needed")
    assert probe.calls == []


@pytest.mark.parametrize(
    ("flags", "plugin_id"),
    [
        (["--inner-loop", "single-agent"], "single-agent"),
        (["--inner-loop", "multi-agent"], "multi-agent"),
        (["--outer-loop", "dynamic"], "dynamic"),
        (["--outer-loop", "plain"], "plain"),
        (["--outer-loop", "evolve"], "evolve"),
    ],
    ids=["single", "multi", "dynamic", "plain", "evolve"],
)
def test_preflight_reports_the_roles_the_orchestration_declares(
    tmp_path: Path, flags: list[str], plugin_id: str
) -> None:
    """The roles are the plugin's declaration: the ids ``--role-model`` is checked against."""
    report = preflight_report(
        _argv(tmp_path, "--backend", "cpu", "--profiler", "none", *flags),
        _parse,
        ScriptedProbe(lambda _argv: None),
    )

    declared = tuple(
        agent.id for agent in built_in_orchestrations().resolve(plugin_id).plugin.agents
    )
    assert isinstance(report.settings, SettingsValid)
    assert declared
    assert report.roles == declared


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
    assert report.settings.cause == BackendUnavailable(
        task="default", backend=Backend.CUDA, declared=()
    )
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


def test_a_task_image_the_run_refuses_is_reported_as_invalid_settings(tmp_path: Path) -> None:
    """Regression: a refused task Dockerfile (a ValueError) crashed ``vibesys preflight``."""
    project = _project_declaring(tmp_path, '"cpu"')
    (task_path(project, "default") / "Dockerfile").write_text("FROM scratch\n")
    probe = ScriptedProbe(lambda _argv: None)

    report = _preflight_of(tmp_path, project, probe, "--docker-image", "custom:1")

    assert isinstance(report.settings, SettingsInvalid)
    assert (report.settings.code, report.settings.stage) == (
        "task_image_unsupported",
        "run_environment",
    )
    assert report.settings.cause == TaskImageUnsupported(task=None, problem=report.settings.message)
    assert "--docker-image" in report.settings.message
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
    assert isinstance(report.settings.cause, SysboxMissing)
    assert "sysbox-runc" in report.settings.cause.problem
    assert "\n" not in report.settings.message


def test_a_docker_in_docker_run_on_a_host_with_sysbox_passes_preflight(tmp_path: Path) -> None:
    argv = _docker_in_docker_project(tmp_path)

    report = preflight_report(
        argv, _parse, _docker_runtimes(ProbeResult(0, '{"runc": {}, "sysbox-runc": {}}'))
    )

    assert report.settings.valid is True


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


def test_a_task_whose_evaluator_requires_a_build_it_does_not_declare_fails_preflight(
    tmp_path: Path,
) -> None:
    """Regression: such a task started, and every measurement, the baseline's too, failed."""
    check = evaluator_check(_queue_task(tmp_path, build=False))

    assert check.state is CheckState.FAIL
    assert "queue-candidate.so" in check.detail
    assert "[build]" in check.detail
    assert isinstance(check.fix, DeclareBuild)
    assert check.fix.outputs == ("queue-candidate.so",)
    assert check.fix.manifest.endswith("vibesys.input.toml")


def test_a_task_that_declares_the_build_its_evaluator_requires_passes_preflight(
    tmp_path: Path,
) -> None:
    check = evaluator_check(_queue_task(tmp_path, build=True))

    assert (check.state, check.fix) == (CheckState.OK, None)


def test_the_evaluator_check_names_the_project_the_benchmark_command_runs_on(
    tmp_path: Path,
) -> None:
    """Regression: the check showed the run's unexpanded ``${PROJECT_ROOT}`` placeholder."""
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    write_task(
        project,
        """\
version = 1
backends = ["cpu"]

[agent]
domain = "generic"

[accuracy]
command = ["python", "-c", "print('ok')"]

[benchmark]
command = ["python", "-c", "print('1')", "--workspace", "${PROJECT_ROOT}"]
""",
    )

    check = evaluator_check(load_task(project, DEFAULT_TASK))

    assert check.state is CheckState.OK
    assert f"--workspace '{project.resolve()}'" in check.detail
    assert "${" not in check.detail


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


def _preflight_of(
    tmp_path: Path,
    project: Path,
    probe: ScriptedProbe,
    *flags: str,
    repository: Callable[[Path], GitRepository] | None = None,
) -> PreflightReport:
    argv = [
        "--outer-loop",
        "agent",
        "--runs-dir",
        str(tmp_path / "runs"),
        "--input",
        str(project),
        "--no-skills",
        "--profiler",
        "none",
        *flags,
    ]
    return preflight_report(argv, _parse, probe, repository)


def test_a_cpu_task_on_a_host_without_a_gpu_passes_preflight_with_no_user_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the CLI's cuda default failed a CPU task on a CPU-only host."""
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    report = _preflight_of(tmp_path, _project_declaring(tmp_path, '"cpu"'), _nvidia_smi(None))

    assert isinstance(report.settings, SettingsValid)
    assert report.backend is Backend.CPU


def test_a_task_whose_backend_the_host_cannot_run_fails_preflight_with_a_typed_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    report = _preflight_of(tmp_path, _project_declaring(tmp_path, '"cuda"'), _nvidia_smi(None))

    assert isinstance(report.settings, SettingsInvalid)
    assert report.settings.cause == BackendUnavailable(
        task="default", backend=Backend.CUDA, declared=(Backend.CUDA,)
    )
    assert report.backend is Backend.CUDA


def _slurm_flags(tmp_path: Path) -> list[str]:
    """The flags ``run.preflight`` adds for a run measured on a cluster, over a minimal operator file."""
    config = tmp_path / "operator-slurm.toml"
    config.write_text(
        '[slurm]\nname = "cluster-m"\nremote_workspace_root = "/remote/runs"\n'
        '[slurm.transport]\nkind = "ssh"\nhost = "host-a"\n',
        encoding="utf-8",
    )
    return ["--run-environment", "slurm", "--slurm-config", str(config)]


@pytest.mark.parametrize("backend", ["cuda", "rocm", "metal"])
@pytest.mark.parametrize("environment", ["slurm", "modal"])
def test_a_gpu_task_measured_on_a_cluster_or_cloud_needs_no_gpu_on_the_agents_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, environment: str
) -> None:
    """Regression: the agent's machine is not where results are measured, so it needs no GPU."""
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    flags = _slurm_flags(tmp_path) if environment == "slurm" else ["--run-environment", "modal"]

    report = _preflight_of(
        tmp_path, _project_declaring(tmp_path, f'"{backend}"'), _nvidia_smi(None), *flags
    )

    assert isinstance(report.settings, SettingsValid)
    assert report.backend is Backend(backend)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [(None, Backend.CPU), (ProbeResult(0, _GPU_ROW), Backend.CUDA)],
    ids=["no-gpu", "gpu"],
)
def test_among_several_declared_backends_the_run_takes_the_first_the_host_provides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: ProbeResult | None,
    expected: Backend,
) -> None:
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    report = _preflight_of(
        tmp_path, _project_declaring(tmp_path, '"cuda", "cpu"'), _nvidia_smi(answer)
    )

    assert isinstance(report.settings, SettingsValid)
    assert report.backend is expected


def test_a_backend_flag_the_task_does_not_declare_is_refused_early(tmp_path: Path) -> None:
    report = _preflight_of(
        tmp_path,
        _project_declaring(tmp_path, '"cpu"'),
        ScriptedProbe(lambda _argv: None),
        "--backend",
        "cuda",
    )

    assert isinstance(report.settings, SettingsInvalid)
    assert report.settings.code == "backend_not_declared"
    assert "cpu" in report.settings.message
    assert report.backend is None


def test_a_backend_flag_the_task_declares_overrides_its_preference(tmp_path: Path) -> None:
    report = _preflight_of(
        tmp_path,
        _project_declaring(tmp_path, '"cuda", "cpu"'),
        ScriptedProbe(lambda _argv: None),
        "--backend",
        "cpu",
    )

    assert report.backend is Backend.CPU


def test_the_backend_enum_names_exactly_the_compute_backends() -> None:
    assert {b.value for b in Backend} == {b.value for b in ComputeBackend}


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


def test_a_clean_project_repository_reports_no_changes(tmp_path: Path) -> None:
    project, disk = _committed_project(tmp_path)

    report = _preflight_of(tmp_path, project, ScriptedProbe(lambda _argv: None), repository=disk)

    assert report.project_changes == ()


def test_uncommitted_changes_in_the_project_repository_are_reported_by_path(
    tmp_path: Path,
) -> None:
    """Regression: every check passed, then Start failed on ``must be clean``."""
    project, disk = _committed_project(tmp_path)
    (project / "src" / "queue.py").write_text("class Queue: pass\n")
    (project / "notes.txt").write_text("draft\n")

    report = _preflight_of(tmp_path, project, ScriptedProbe(lambda _argv: None), repository=disk)

    assert report.project_changes == ("notes.txt", "src/queue.py")


def test_a_project_that_is_no_repository_yet_reports_no_changes(tmp_path: Path) -> None:
    project = _project_declaring(tmp_path, '"cpu"')

    report = _preflight_of(tmp_path, project, ScriptedProbe(lambda _argv: None))

    assert report.project_changes == ()


def _first_declared_role(tmp_path: Path) -> str:
    """A role the run's orchestration declares: the first its own preflight reports."""
    report = preflight_report(
        _argv(tmp_path / "discover", "--backend", "cpu", "--profiler", "none"),
        _parse,
        ScriptedProbe(lambda _argv: None),
    )
    return report.roles[0]


@pytest.mark.parametrize("environment", ["docker", None], ids=["docker", "default"])
def test_roles_on_two_providers_are_refused_where_containers_carry_one_login(
    tmp_path: Path, environment: str | None
) -> None:
    """A container carries the run's provider's login only, so another provider's role cannot start."""
    role = _first_declared_role(tmp_path)
    flags = [ROLE_PROVIDER_FLAG, f"{role}=claude", "--cli-provider", "codex"]
    if environment is not None:
        flags += ["--run-environment", environment]

    report = preflight_report(
        _argv(tmp_path, "--backend", "cpu", "--profiler", "none", *flags),
        _parse,
        ScriptedProbe(lambda _argv: None),
    )

    assert isinstance(report.settings, SettingsInvalid)
    assert "claude, codex" in report.settings.message
    assert isinstance(report.settings.cause, ConfigInvalid)


def test_roles_on_the_runs_own_provider_are_accepted_whatever_the_environment(
    tmp_path: Path,
) -> None:
    role = _first_declared_role(tmp_path)
    flags = [ROLE_PROVIDER_FLAG, f"{role}=codex", "--cli-provider", "codex"]

    report = preflight_report(
        _argv(tmp_path, "--backend", "cpu", "--profiler", "none", *flags),
        _parse,
        ScriptedProbe(lambda _argv: None),
    )

    assert isinstance(report.settings, SettingsValid)
    assert report.roles


def test_a_role_provider_flag_reaches_the_roles_configuration(tmp_path: Path) -> None:
    flags = [ROLE_PROVIDER_FLAG, "implementer=claude", ROLE_MODEL_FLAG, "implementer=claude-opus-5"]
    args = cli.parse_cli_invocation(_argv(tmp_path, *flags)).args

    config, _skills, _backend = load_config_and_skills(args, domain=DomainName.LLM_SERVING)

    assert config.agent.roles["implementer"].provider == "claude"
    assert config.agent.roles["implementer"].model == "claude-opus-5"
