"""Tests for the VibeSys command-line entry point."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict
from unittest.mock import Mock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from entrypoints.cli import (
    _extract_flag,
    _extract_loop_selection,
    _load_metric_space_toml,
    _parse_cli_objective,
    _prepare_experiment_repository,
    _render_configuration_error,
    _run_migrate_run_environment,
    _validate_target_inputs,
    _with_operator_constraints,
    load_config_and_skills,
    parse_cli_invocation,
    run_environment_spec_from_args,
)
from entrypoints.headless import main
from vibesys.config import Config
from vibesys.constants import ComputeBackend, DomainName
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.events import CoreEventType, RunStartedData
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.loops.roles import EXPECTED_AGENT_ROLES
from vibesys.profilers import ProfilerKind
from vibesys.sandbox.run_environment import run_environment_record
from vs_project import (
    RUN_SCHEMA_VERSION,
    AgentRunConfiguration,
    EvolveRunConfiguration,
    PlainRunConfiguration,
    Project,
    RunConfiguration,
    RunEnvironmentRecord,
)


def _patch_loop_runner(loop_name: str, runner: Mock):  # noqa: ANN202
    """Replace one immutable CLI dispatch record's runner."""
    import dataclasses  # noqa: PLC0415

    from entrypoints import cli  # noqa: PLC0415

    command = cli._LOOP_COMMANDS[loop_name]  # noqa: SLF001
    return patch.dict(
        cli._LOOP_COMMANDS,  # noqa: SLF001
        {loop_name: dataclasses.replace(command, run=runner)},
    )


# The lazily-imported loop function `vibesys.api._dispatch` calls for each
# `LoopKind`. Tests that need `create_session`'s own RUN_STARTED/FINISHED/
# FAILED bracket to actually execute patch these directly instead of the CLI
# dispatch record (see `_patch_loop_runner`), which would bypass it.
_LOOP_RUN_TARGETS = {
    "agent": "vibesys.loops.agent.loop.run_agent_loop",
    "plain": "vibesys.loops.plain.loop.run_plain_loop",
    "evolve": "vibesys.loops.evolve.loop.run_evolve_loop",
}


def _write_input_project(parent: Path, name: str = "queue-spsc") -> Path:
    project = parent / name
    (project / "src").mkdir(parents=True)
    (project / "OBJECTIVE.md").write_text("Make the queue faster.\n")
    (project / "vibesys.input.toml").write_text(
        """\
version = 1

[agent]
domain = "generic"

[accuracy]
command = ["uv", "run", "python", "accuracy_checker/checker.py"]

[benchmark]
command = ["uv", "run", "python", "benchmark/benchmark.py"]
"""
    )
    (project / "src" / "queue.py").write_text("class Queue: pass\n")
    return project


def _write_repository_task(project: Path, name: str) -> Path:
    task = project / ".vibesys" / "tasks" / name
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text(f"Optimize {name}.\n")
    (task / "vibesys.input.toml").write_text(
        """\
version = 1

[agent]
domain = "generic"

[accuracy]
command = ["python", "-c", "print('ok')"]

[benchmark]
command = ["python", "-c", "print('1')"]
"""
    )
    return task


def test_run_environment_spec_uses_task_modal_entrypoint(tmp_path: Path) -> None:
    project = _write_input_project(tmp_path)
    entrypoint = project / "deploy" / "service.py"
    entrypoint.parent.mkdir()
    entrypoint.write_text("app = object()\n")
    with (project / "vibesys.input.toml").open("a") as manifest:
        manifest.write('\n[environment.modal]\nentrypoint = "deploy/service.py"\n')
    args = argparse.Namespace(
        input_bundle=load_input_bundle(project),
        docker=False,
        docker_image=None,
        modal=True,
        modal_gpu="H100!",
        modal_model_volume=None,
        modal_app="vibesys",
    )

    spec = run_environment_spec_from_args(args)

    assert spec.name == "modal"
    assert spec.options["entrypoint"] == "deploy/service.py"


def test_cli_selects_documented_skypilot_environment(tmp_path: Path) -> None:
    project = _write_input_project(tmp_path)
    with (project / "vibesys.input.toml").open("a") as manifest:
        manifest.write(
            """
[resources]
nodes = 1
accelerators_per_node = 4
accelerator_backend = "rocm"
"""
        )

    invocation = parse_cli_invocation(
        [
            "--input",
            str(project),
            "--run-environment",
            "skypilot",
            "--cluster-profile",
            "remote-gpu",
        ]
    )
    spec = run_environment_spec_from_args(invocation.args)

    assert spec.name == "skypilot"
    assert spec.options["profile"] == "remote-gpu"
    assert spec.resources is not None
    assert spec.resources.accelerators_per_node == 4


def test_cli_rejects_mixed_generic_and_compatibility_environment_flags(tmp_path: Path) -> None:
    project = _write_input_project(tmp_path)
    with pytest.raises(ValueError, match="cannot be combined"):
        parse_cli_invocation(["--input", str(project), "--run-environment", "local", "--modal"])


class _CommonConfiguration(TypedDict):
    run_environment: RunEnvironmentRecord
    model: str
    agent_backend: str
    agent_driver: str
    cli_provider: str
    cli_timeout: int
    compute_backend: str
    profiler: str
    modality: str
    default_reasoning_effort: str
    outer_model: str
    outer_reasoning_effort: str
    inner_model: str
    inner_reasoning_effort: str


_LOCAL_ENVIRONMENT = RunEnvironmentRecord(name="local")
_MODAL_ENVIRONMENT = RunEnvironmentRecord(
    name="modal",
    gpu="A100-80GB",
    model_volume="weights",
    app="run-app",
)


def _common_configuration(
    run_environment: RunEnvironmentRecord | None = None,
) -> _CommonConfiguration:
    return {
        "run_environment": run_environment or _LOCAL_ENVIRONMENT,
        "model": "gpt-recorded",
        "agent_backend": "cli",
        "agent_driver": "omnigent",
        "cli_provider": "claude",
        "cli_timeout": 321,
        "compute_backend": "cpu",
        "profiler": "none",
        "modality": "kv_store",
        "default_reasoning_effort": "high",
        "outer_model": "gpt-outer",
        "outer_reasoning_effort": "medium",
        "inner_model": "gpt-inner",
        "inner_reasoning_effort": "low",
    }


def _agent_configuration(
    *,
    max_rounds: int = 7,
    run_environment: RunEnvironmentRecord | None = None,
) -> AgentRunConfiguration:
    return AgentRunConfiguration(
        **_common_configuration(run_environment),
        outer_loop="agent",
        inner_loop="single-agent",
        interface="service",
        max_rounds=max_rounds,
        max_retries_per_round=4,
        judge_every=2,
        official_eval_every=5,
        memory_layout="directories",
        operator_constraints=("Preserve the ABI.",),
    )


def _plain_configuration(*, max_rounds: int = 6) -> PlainRunConfiguration:
    return PlainRunConfiguration(
        **_common_configuration(),
        outer_loop="plain",
        max_rounds=max_rounds,
        max_attempts_per_issue=4,
        max_issues_per_perf_eval=2,
    )


def _evolve_configuration(*, max_generations: int = 5) -> EvolveRunConfiguration:
    return EvolveRunConfiguration(
        **_common_configuration(),
        outer_loop="evolve",
        max_generations=max_generations,
        children_per_generation=3,
        k_top_inspirations=4,
        k_random_inspirations=1,
        selection_temperature=0.25,
        seed=17,
        search_policy="openevolve",
        openevolve_population_size=40,
        openevolve_archive_size=20,
        openevolve_num_islands=3,
        openevolve_migration_interval=4,
        openevolve_migration_rate=0.25,
        frontier_bias=0.6,
        bootstrap_max_attempts=7,
        keep_deployments=True,
        max_parallelism=2,
        objectives=("latency:min", "throughput:max"),
    )


def _write_project_run(  # noqa: PLR0913
    project: Path,
    run_id: str,
    *,
    configuration: RunConfiguration,
    created_at: datetime,
    make_current: bool = True,
    task_name: str | None = None,
) -> Project:
    vibesys_project = Project.open(project)
    store = vibesys_project.state
    store.create_project(project.name)
    manifest = store.new_run_manifest(
        project.name,
        run_id=run_id,
        branch=f"vibesys-runs/{run_id}",
        vibesys_version="0.2.0-test",
        configuration=configuration,
        trusted_input_baseline="0" * 40,
        task_name=task_name,
        now=created_at,
    )
    store.create_run(manifest, make_current=make_current)
    return vibesys_project


def _git(project: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=project, check=True)  # noqa: S603, S607


def test_extract_flag_accepts_space_and_equals_forms() -> None:
    value, rest = _extract_flag(["--outer-loop", "agent", "--input", "x"], "--outer-loop")
    assert value == "agent"
    assert rest == ["--input", "x"]

    value, rest = _extract_flag(["--input", "x", "--outer-loop=evolve"], "--outer-loop")
    assert value == "evolve"
    assert rest == ["--input", "x"]


def test_extract_flag_rejects_a_missing_value() -> None:
    with pytest.raises(ConfigurationError) as exc:
        _extract_flag(["--outer-loop"], "--outer-loop")
    assert exc.value.diagnostic.code == "invalid_arguments"


@pytest.mark.parametrize("loop", ["agent", "profile-guided", "plain", "evolve"])
def test_extract_loop_selection(loop: str) -> None:
    selected, rest = _extract_loop_selection(["--outer-loop", loop, "--input", "x"])
    assert selected == loop
    assert rest == ["--input", "x"]


def test_extract_loop_selection_defaults_to_agent_and_rejects_unknown() -> None:
    assert _extract_loop_selection(["--input", "x"]) == ("agent", ["--input", "x"])
    with pytest.raises(ConfigurationError) as exc:
        _extract_loop_selection(["--outer-loop", "unknown"])
    assert exc.value.diagnostic.stage == "argument_parsing"


def test_profile_guided_loop_requires_and_accepts_manifest_capability(tmp_path: Path) -> None:
    project = _write_input_project(tmp_path)
    command = ["--outer-loop", "profile-guided", "--input", str(project)]
    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation(command)
    assert exc.value.diagnostic.code == "missing_profile_guided_input"

    with (project / "vibesys.input.toml").open("a") as manifest:
        manifest.write('\n[profile_guided]\ncommand = ["python", "profile.py"]\n')
    invocation = parse_cli_invocation(command)
    assert invocation.loop_kind == "profile-guided"
    assert invocation.args.input_bundle.manifest.profile_guided is not None


@pytest.mark.parametrize("loop", ["agent", "plain", "evolve"])
def test_all_loops_run_directly_in_a_canonical_project(
    loop: str,
    tmp_path: Path,
) -> None:
    project = _write_input_project(tmp_path)

    invocation = parse_cli_invocation(["--outer-loop", loop, "--input", str(project)])

    assert invocation.loop_kind == loop
    assert invocation.args.runs_dir is None
    assert invocation.args.input == project
    assert invocation.args.input_bundle.root == project.resolve()


def test_current_directory_is_the_default_direct_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _write_input_project(tmp_path)
    monkeypatch.chdir(project)

    invocation = parse_cli_invocation([])

    assert invocation.args.input == project.resolve()
    assert invocation.args.runs_dir is None
    assert invocation.args.input_bundle.root == project.resolve()


def test_repository_native_project_selects_a_named_task(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    selected = _write_repository_task(project, "latency")
    _write_repository_task(project, "throughput")

    invocation = parse_cli_invocation(["--project", str(project), "--task", "latency"])

    assert invocation.args.input == project
    assert invocation.args.task == "latency"
    assert invocation.args.input_bundle.root == project.resolve()
    assert invocation.args.input_bundle.task_root == selected.resolve()


@pytest.mark.parametrize("environment_flag", [[], ["--docker"], ["--run-environment", "docker"]])
def test_task_dockerfile_selects_docker_without_building_during_validation(
    environment_flag: list[str],
    tmp_path: Path,
) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    task = _write_repository_task(project, "latency")
    dockerfile = task / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-bookworm\n")

    with patch("entrypoints.cli.build_task_image") as build:
        invocation = parse_cli_invocation(
            ["--project", str(project), "--task", "latency", *environment_flag]
        )
        spec = run_environment_spec_from_args(invocation.args)

    assert spec.name == "docker"
    assert spec.options["image"] is None
    build.assert_not_called()


def test_task_dockerfile_builds_once_for_a_launch(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    task = _write_repository_task(project, "latency")
    dockerfile = task / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-bookworm\n")
    invocation = parse_cli_invocation(["--project", str(project), "--task", "latency"])
    image_id = f"sha256:{'a' * 64}"

    with patch("entrypoints.cli.build_task_image", return_value=image_id) as build:
        spec = run_environment_spec_from_args(
            invocation.args,
            build_task_docker_image=True,
        )

    assert spec.name == "docker"
    assert spec.options["image"] == image_id
    build.assert_called_once_with(dockerfile.resolve())


@pytest.mark.parametrize(
    ("loop_name", "runner_path"),
    [
        ("agent", "vibesys.loops.agent.loop.run_agent_loop"),
        ("plain", "vibesys.loops.plain.loop.run_plain_loop"),
        ("evolve", "vibesys.loops.evolve.loop.run_evolve_loop"),
    ],
)
def test_each_outer_loop_builds_the_task_image_once(
    loop_name: str,
    runner_path: str,
    tmp_path: Path,
) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = tmp_path / "repository"
    project.mkdir()
    task = _write_repository_task(project, "latency")
    dockerfile = task / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-bookworm\n")
    invocation = parse_cli_invocation(
        ["--outer-loop", loop_name, "--project", str(project), "--task", "latency"]
    )
    image_id = f"sha256:{'a' * 64}"
    run = getattr(cli, f"_run_{loop_name}")
    config = Config.model_validate({"model": {"name": "gpt-5.4"}})
    # `load_config_and_skills`/`_prepare_experiment_repository` normally fill
    # these in as side effects on `args`; both are mocked away here, so set
    # them explicitly to satisfy `RunRequest`'s validation.
    invocation.args.repo_visibility = config.repository.visibility
    invocation.args.exp_name = "test-run"

    with (
        patch("entrypoints.cli.build_task_image", return_value=image_id) as build,
        patch(
            "entrypoints.cli.load_config_and_skills",
            return_value=(config, (), ComputeBackend.CPU),
        ),
        patch("entrypoints.cli._prepare_experiment_repository"),
        patch(runner_path, return_value=True) as loop_runner,
    ):
        run(invocation.args)

    build.assert_called_once_with(dockerfile.resolve())
    assert loop_runner.call_args.kwargs["run_environment"].options["image"] == image_id


@pytest.mark.parametrize(
    "flags",
    [
        ["--run-environment", "local"],
        ["--modal"],
        ["--skypilot"],
        ["--docker-image", "custom:latest"],
    ],
)
def test_fresh_task_dockerfile_rejects_conflicting_environment_flags(
    flags: list[str],
    tmp_path: Path,
) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    task = _write_repository_task(project, "latency")
    (task / "Dockerfile").write_text("FROM python:3.12-bookworm\n")

    with pytest.raises(ValueError, match="task Dockerfile"):
        parse_cli_invocation(["--project", str(project), "--task", "latency", *flags])


def test_repository_native_project_implicitly_selects_its_only_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    _write_repository_task(project, "latency")
    monkeypatch.chdir(project)

    invocation = parse_cli_invocation([])

    assert invocation.args.input == project.resolve()
    assert invocation.args.task == "latency"


def test_repository_native_project_requires_task_when_ambiguous(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    _write_repository_task(project, "latency")
    _write_repository_task(project, "throughput")

    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation(["--project", str(project)])

    assert "latency, throughput" in exc.value.diagnostic.message


def test_repository_native_project_supports_isolated_materialization(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    _write_repository_task(project, "latency")

    invocation = parse_cli_invocation(
        ["--project", str(project), "--runs-dir", str(tmp_path / "runs")]
    )

    assert invocation.args.input_bundle.task_name == "latency"
    assert invocation.args.runs_dir == tmp_path / "runs"


def test_runs_dir_is_an_absolute_project_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _write_input_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    invocation = parse_cli_invocation(["--input", str(project), "--runs-dir", "runs"])

    assert invocation.args.runs_dir == (tmp_path / "runs").resolve()
    assert invocation.args.input_bundle.root == project.resolve()


@pytest.mark.parametrize("value", ["", " \t "])
def test_runs_dir_rejects_empty_values(value: str) -> None:
    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation([f"--runs-dir={value}"])
    assert exc.value.diagnostic.code == "invalid_arguments"


def test_runs_dir_rejects_the_python_installation_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = _write_input_project(tmp_path)
    prefix = tmp_path / ".venv"
    monkeypatch.setattr(cli.sys, "prefix", str(prefix))

    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation(["--input", str(project), "--runs-dir", str(prefix / "runs")])
    assert exc.value.diagnostic.code == "invalid_runs_dir"


def test_direct_project_rejects_materialization_inputs_but_copy_accepts_them(
    tmp_path: Path,
) -> None:
    project = _write_input_project(tmp_path)
    with (project / "vibesys.input.toml").open("a") as manifest:
        manifest.write(
            """
[[workspace.sources]]
name = "library"
repo = "https://example.invalid/library.git"
commit = "0123456"
dest = "library"
"""
        )

    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation(["--input", str(project)])
    assert exc.value.diagnostic.code == "direct_project_materialization_unsupported"

    copied = parse_cli_invocation(["--input", str(project), "--runs-dir", str(tmp_path / "runs")])
    assert [source.name for source in copied.args.input_bundle.workspace_sources] == ["library"]


def test_direct_runs_default_to_local_and_copied_runs_default_to_a_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = _write_input_project(tmp_path)
    github = Mock()
    github.current_user.return_value = "octocat"
    monkeypatch.setattr(cli, "GitHubCLI", Mock(return_value=github))
    config = Config.model_validate({"model": {"name": "gpt-5.4"}})

    direct = parse_cli_invocation(["--input", str(project)])
    _prepare_experiment_repository(direct.args, config)
    assert direct.args.local is True
    assert direct.args.repo is None

    copied = parse_cli_invocation(["--input", str(project), "--runs-dir", str(tmp_path / "runs")])
    _prepare_experiment_repository(copied.args, config)
    assert copied.args.repo == f"octocat/{copied.args.exp_name}"
    github.current_user.assert_called_once_with()


def test_short_repository_name_uses_the_configured_owner(tmp_path: Path) -> None:
    from entrypoints import cli  # noqa: PLC0415

    config_path = tmp_path / "agent.toml"
    config_path.write_text('[model]\nname = "gpt-5.5"\n[repository]\nowner = "my-lab"\n')
    args = cli._build_agent_parser().parse_args(  # noqa: SLF001
        ["--repo", "trial", "--config", str(config_path), "--no-skills"]
    )

    load_config_and_skills(args, domain=DomainName.GENERIC)

    assert args.repo == "my-lab/trial"


def test_local_and_repo_are_mutually_exclusive() -> None:
    from entrypoints import cli  # noqa: PLC0415

    args = cli._build_agent_parser().parse_args(  # noqa: SLF001
        ["--local", "--repo", "owner/trial", "--no-skills"]
    )
    with pytest.raises(ConfigurationError, match="--local cannot be combined"):
        load_config_and_skills(args, domain=DomainName.GENERIC)


@pytest.mark.parametrize(
    ("builder", "validator"),
    [
        ("_build_agent_parser", "_validate_agent"),
        ("_build_plain_parser", "_validate_plain"),
        ("_build_evolve_parser", "_validate_evolve"),
    ],
)
def test_all_loops_accept_modal_without_a_profiler(
    builder: str,
    validator: str,
    tmp_path: Path,
) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = _write_input_project(tmp_path)
    args = getattr(cli, builder)().parse_args(
        ["--input", str(project), "--modal", "--profiler", "none"]
    )

    getattr(cli, validator)(args)
    assert args.profiler is ProfilerKind.NONE


def test_profiler_validation_uses_the_selected_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = _write_input_project(tmp_path)
    args = cli._build_agent_parser().parse_args(  # noqa: SLF001
        ["--input", str(project), "--profiler", "nsys"]
    )
    monkeypatch.setattr(
        cli,
        "supported_profilers",
        Mock(return_value=frozenset({ProfilerKind.TORCH, ProfilerKind.NONE})),
    )

    with pytest.raises(ConfigurationError, match="run environment 'local'"):
        cli._validate_agent(args)  # noqa: SLF001


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--children-per-generation", "0"),
        ("--max-generations", "0"),
        ("--selection-temperature", "0"),
        ("--bootstrap-max-attempts", "0"),
        ("--max-parallelism", "0"),
        ("--openevolve-num-islands", "0"),
        ("--openevolve-migration-rate", "1.1"),
    ],
)
def test_evolve_rejects_invalid_search_settings(
    flag: str,
    value: str,
    tmp_path: Path,
) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = _write_input_project(tmp_path)
    args = cli._build_evolve_parser().parse_args(  # noqa: SLF001
        ["--input", str(project), flag, value]
    )
    with pytest.raises(ConfigurationError):
        cli._validate_evolve(args)  # noqa: SLF001


def test_openevolve_knobs_select_openevolve_for_a_new_run(tmp_path: Path) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = _write_input_project(tmp_path)
    args = cli._build_evolve_parser().parse_args(  # noqa: SLF001
        ["--input", str(project), "--openevolve-num-islands", "3"]
    )

    policy, config = cli._resolve_openevolve_options(args)  # noqa: SLF001

    assert policy == "openevolve"
    assert config is not None
    assert config.num_islands == 3


def test_openevolve_knobs_cannot_be_combined_with_vibesys_policy(tmp_path: Path) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = _write_input_project(tmp_path)
    args = cli._build_evolve_parser().parse_args(  # noqa: SLF001
        [
            "--input",
            str(project),
            "--search-policy",
            "vibesys",
            "--openevolve-num-islands",
            "3",
        ]
    )
    with pytest.raises(ConfigurationError):
        cli._validate_evolve(args)  # noqa: SLF001


def test_target_validation_loads_the_manifest_contract(tmp_path: Path) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = _write_input_project(tmp_path)
    with (project / "vibesys.input.toml").open("a") as manifest:
        manifest.write(
            '\n[benchmark.result]\njson_argument = "--output-json"\nmetric = "ops_per_sec"\n'
        )
    args = cli._build_agent_parser().parse_args(["--input", str(project)])  # noqa: SLF001

    _validate_target_inputs(args)

    assert args.input_bundle.domain is DomainName.GENERIC
    assert args.input_bundle.benchmark_result.metric == "ops_per_sec"
    assert args.input_bundle.benchmark_result.json_argument == "--output-json"


@pytest.mark.parametrize("missing", ["OBJECTIVE.md", "vibesys.input.toml"])
def test_target_validation_reports_missing_required_files(
    missing: str,
    tmp_path: Path,
) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = _write_input_project(tmp_path)
    (project / missing).unlink()
    args = cli._build_agent_parser().parse_args(["--input", str(project)])  # noqa: SLF001

    with pytest.raises(ConfigurationError) as exc:
        _validate_target_inputs(args)
    assert missing in exc.value.diagnostic.message


def test_target_validation_explains_an_invalid_launch_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrypoints import cli  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    args = cli._build_agent_parser().parse_args([])  # noqa: SLF001

    with pytest.raises(ConfigurationError) as exc:
        _validate_target_inputs(args)

    assert "Current directory is not a VibeSys project" in exc.value.diagnostic.message
    assert "Launch VibeSys from the project or pass --project PATH" in (
        exc.value.diagnostic.message
    )


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--max-retries-per-round", "0"), ("--judge-every", "0"), ("--official-eval-every", "0")],
)
def test_agent_rejects_nonpositive_round_settings(
    flag: str,
    value: str,
    tmp_path: Path,
) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = _write_input_project(tmp_path)
    args = cli._build_agent_parser().parse_args(  # noqa: SLF001
        ["--input", str(project), flag, value]
    )
    with pytest.raises(ConfigurationError):
        cli._validate_agent(args)  # noqa: SLF001


def test_operator_constraints_are_repeatable_and_do_not_mutate_the_objective() -> None:
    objective = "Maximize throughput.\n"

    effective = _with_operator_constraints(
        objective,
        ["No quantization.", "  One H100 only.  "],
    )

    assert objective == "Maximize throughput.\n"
    assert effective.endswith("## Operator constraints\n\n- No quantization.\n- One H100 only.\n")


def test_omitted_config_uses_builtin_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = _write_input_project(tmp_path)
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.chdir(launch)
    args = cli._build_agent_parser().parse_args(  # noqa: SLF001
        ["--input", str(project), "--no-skills"]
    )

    config, skills, _ = load_config_and_skills(args, domain=DomainName.GENERIC)

    assert config.model.name == "gpt-5.4"
    assert skills is None


def test_omitted_config_loads_only_the_launch_directory_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrypoints import cli  # noqa: PLC0415

    project = _write_input_project(tmp_path)
    launch = tmp_path / "launch"
    launch.mkdir()
    (tmp_path / "agent.toml").write_text('[model]\nname = "parent-model"\n')
    monkeypatch.chdir(launch)
    args = cli._build_agent_parser().parse_args(["--input", str(project)])  # noqa: SLF001

    config, _, _ = load_config_and_skills(args, domain=DomainName.GENERIC)
    assert config.model.name == "gpt-5.4"

    (launch / "agent.toml").write_text('[model]\nname = "launch-model"\n')
    config, _, _ = load_config_and_skills(args, domain=DomainName.GENERIC)
    assert config.model.name == "launch-model"


def test_missing_explicit_config_is_a_configuration_error(tmp_path: Path) -> None:
    from entrypoints import cli  # noqa: PLC0415

    args = cli._build_agent_parser().parse_args(  # noqa: SLF001
        ["--config", str(tmp_path / "missing.toml")]
    )
    with pytest.raises(ConfigurationError) as exc:
        load_config_and_skills(args, domain=DomainName.GENERIC)
    assert exc.value.diagnostic.code == "config_load_failed"


def test_validate_command_checks_the_current_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _write_input_project(tmp_path)
    monkeypatch.chdir(project)

    with patch.object(sys, "argv", ["vibesys", "validate"]):
        main()

    output = capsys.readouterr().out
    assert "VibeSys validation passed" in output
    assert f"project: {project}" in output


def test_validate_command_reports_an_invalid_project_without_running_a_loop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _write_input_project(tmp_path)
    (project / "OBJECTIVE.md").unlink()
    runner = Mock()

    with (
        patch.object(sys, "argv", ["vibesys", "validate", str(project)]),
        _patch_loop_runner("agent", runner),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == 1
    assert "OBJECTIVE.md not found" in capsys.readouterr().err
    runner.assert_not_called()


def test_direct_resume_selects_an_explicit_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _write_input_project(tmp_path)
    run_id = "20260811-120000-11111111-agent"
    _write_project_run(
        project,
        run_id,
        configuration=_agent_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    monkeypatch.chdir(project)

    invocation = parse_cli_invocation(["--resume", run_id])

    assert invocation.args.resume == run_id
    assert invocation.args.exp_name == run_id
    assert invocation.args.input == project.resolve()
    assert invocation.args.input_bundle.root == project.resolve()


def test_repository_resume_restores_the_recorded_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    selected = _write_repository_task(project, "latency")
    _write_repository_task(project, "throughput")
    run_id = "20260811-120000-11111111-agent"
    _write_project_run(
        project,
        run_id,
        configuration=_agent_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
        task_name="latency",
    )
    monkeypatch.chdir(project)

    invocation = parse_cli_invocation(["--resume", run_id])

    assert invocation.args.task == "latency"
    assert invocation.args.input_bundle.task_root == selected.resolve()


def test_repository_resume_keeps_a_recorded_local_environment_despite_task_dockerfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    task = _write_repository_task(project, "latency")
    (task / "Dockerfile").write_text("FROM python:3.12-bookworm\n")
    run_id = "20260811-120000-11111111-agent"
    _write_project_run(
        project,
        run_id,
        configuration=_agent_configuration(run_environment=_LOCAL_ENVIRONMENT),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
        task_name="latency",
    )
    monkeypatch.chdir(project)

    args = parse_cli_invocation(["--resume", run_id]).args
    with patch("entrypoints.cli.build_task_image") as build:
        spec = run_environment_spec_from_args(args, build_task_docker_image=True)

    assert spec.name == "local"
    build.assert_not_called()


def test_repository_resume_rebuilds_a_recorded_task_docker_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    task = _write_repository_task(project, "latency")
    dockerfile = task / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-bookworm\n")
    recorded_image = f"sha256:{'a' * 64}"
    rebuilt_image = f"sha256:{'b' * 64}"
    run_id = "20260811-120000-11111111-agent"
    _write_project_run(
        project,
        run_id,
        configuration=_agent_configuration(
            run_environment=RunEnvironmentRecord(name="docker", image=recorded_image)
        ),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
        task_name="latency",
    )
    monkeypatch.chdir(project)

    args = parse_cli_invocation(["--resume", run_id]).args
    with patch("entrypoints.cli.build_task_image", return_value=rebuilt_image) as build:
        spec = run_environment_spec_from_args(args, build_task_docker_image=True)

    assert spec.name == "docker"
    assert spec.options["image"] == rebuilt_image
    build.assert_called_once_with(dockerfile.resolve())


def test_repository_resume_rejects_a_different_task(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    _write_repository_task(project, "latency")
    _write_repository_task(project, "throughput")
    run_id = "20260811-120000-11111111-agent"
    _write_project_run(
        project,
        run_id,
        configuration=_agent_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
        task_name="latency",
    )

    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation(
            ["--project", str(project), "--task", "throughput", "--resume", run_id]
        )

    assert exc.value.diagnostic.code == "project_resume_configuration_mismatch"


def test_direct_resume_prefers_current_then_latest_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _write_input_project(tmp_path)
    current = "20260811-120000-11111111-current"
    latest = "20260811-130000-22222222-latest"
    store = _write_project_run(
        project,
        current,
        configuration=_agent_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    store = _write_project_run(
        project,
        latest,
        configuration=_agent_configuration(),
        created_at=datetime(2026, 8, 11, 13, tzinfo=UTC),
        make_current=False,
    )
    monkeypatch.chdir(project)

    assert parse_cli_invocation(["--resume"]).args.resume == current

    store.state.set_current_run(None)
    assert parse_cli_invocation(["--resume", "latest"]).args.resume == latest


def test_collection_resume_selects_the_project_root_and_its_current_run(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "runs"
    project = _write_input_project(collection, "trial")
    current = "20260811-120000-11111111-current"
    _write_project_run(
        project,
        current,
        configuration=_agent_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )

    invocation = parse_cli_invocation(["--runs-dir", str(collection), "--resume", "trial"])

    assert invocation.args.resume == current
    assert invocation.args.input == project.resolve()
    assert invocation.args.input_bundle.root == project.resolve()


def test_remote_resume_selects_run_branch_before_reading_project_state(
    tmp_path: Path,
) -> None:
    source = _write_input_project(tmp_path, "source")
    _git(source, "init", "-q", "-b", "main")
    _git(source, "add", ".")
    _git(
        source,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "initial",
    )
    run_id = "20260811-120000-11111111-agent"
    _git(source, "switch", "-q", "-c", f"vibesys-runs/{run_id}")
    _write_project_run(
        source,
        run_id,
        configuration=_agent_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    _git(source, "add", ".vibesys/state")
    _git(
        source,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "run state",
    )

    remote = tmp_path / "remote.git"
    _git(
        tmp_path,
        "init",
        "--bare",
        "-q",
        "--initial-branch=main",
        str(remote),
    )
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-q", "origin", "main", f"vibesys-runs/{run_id}")

    runs_dir = tmp_path / "runs"
    invocation = parse_cli_invocation(["--runs-dir", str(runs_dir), "--resume", remote.as_uri()])

    project = runs_dir / "remote"
    assert invocation.args.input == project.resolve()
    assert invocation.args.resume == run_id
    branch = subprocess.run(
        ["git", "branch", "--show-current"],  # noqa: S607
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "@{upstream}"],  # noqa: S607
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == f"vibesys-runs/{run_id}"
    assert upstream == f"origin/vibesys-runs/{run_id}"

    Project.open(source).state.update_run_configuration(
        run_id,
        _agent_configuration(max_rounds=8),
    )
    _git(source, "add", ".vibesys/state")
    _git(
        source,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "advance run state",
    )
    _git(source, "push", "-q", "origin", f"vibesys-runs/{run_id}")

    advanced = parse_cli_invocation(["--runs-dir", str(runs_dir), "--resume", remote.as_uri()])

    assert advanced.args.resume == run_id
    assert advanced.args.max_rounds == 8
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == subprocess.run(  # noqa: S603
            ["git", "rev-parse", "origin/vibesys-runs/" + run_id],  # noqa: S607
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )

    Project.open(project).state.set_current_run(run_id)

    newer_run_id = "20260811-130000-22222222-agent"
    _git(source, "switch", "-q", "-c", f"vibesys-runs/{newer_run_id}")
    _write_project_run(
        source,
        newer_run_id,
        configuration=_agent_configuration(),
        created_at=datetime(2026, 8, 11, 13, tzinfo=UTC),
    )
    _git(source, "add", ".vibesys/state")
    _git(
        source,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "newer run state",
    )
    _git(source, "push", "-q", "origin", f"vibesys-runs/{newer_run_id}")

    resumed = parse_cli_invocation(["--runs-dir", str(runs_dir), "--resume", remote.as_uri()])

    assert resumed.args.input == project.resolve()
    assert resumed.args.resume == newer_run_id
    selected = subprocess.run(
        ["git", "branch", "--show-current"],  # noqa: S607
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert selected == f"vibesys-runs/{newer_run_id}"


def test_remote_resume_rejects_non_github_origin_lookalike(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    destination = runs_dir / "project"
    destination.mkdir(parents=True)
    _git(destination, "init", "-q")
    _git(
        destination,
        "remote",
        "add",
        "origin",
        "https://evil.example/example/project.git",
    )

    with pytest.raises(ConfigurationError) as caught:
        parse_cli_invocation(["--runs-dir", str(runs_dir), "--resume", "example/project"])

    assert caught.value.diagnostic.code == "resume_clone_failed"
    assert "different origin" in caught.value.diagnostic.message


def test_remote_resume_rejects_parent_directory_as_clone_name(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"

    with pytest.raises(ConfigurationError) as caught:
        parse_cli_invocation(["--runs-dir", str(runs_dir), "--resume", "file:///tmp/.."])

    assert caught.value.diagnostic.code == "resume_clone_failed"
    assert "safe local directory name" in caught.value.diagnostic.message


def test_collection_latest_considers_only_canonical_projects(tmp_path: Path) -> None:
    collection = tmp_path / "runs"
    unmanaged = collection / "zz-unmanaged"
    (unmanaged / "workspace").mkdir(parents=True)
    (unmanaged / "logs").mkdir()
    project = _write_input_project(collection, "aa-project")
    run_id = "20260811-120000-11111111-current"
    _write_project_run(
        project,
        run_id,
        configuration=_agent_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )

    invocation = parse_cli_invocation(["--runs-dir", str(collection), "--resume", "latest"])

    assert invocation.args.input == project.resolve()
    assert invocation.args.resume == run_id


def test_collection_resume_requires_project_metadata(tmp_path: Path) -> None:
    collection = tmp_path / "runs"
    unmanaged = _write_input_project(collection, "unmanaged")

    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation(["--runs-dir", str(collection), "--resume", unmanaged.name])
    assert exc.value.diagnostic.code == "resume_not_found"
    assert "not a VibeSys project" in exc.value.diagnostic.message


def test_resume_switches_to_the_recorded_run_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _write_input_project(tmp_path)
    _git(project, "init", "-q", "-b", "main")
    _git(project, "add", ".")
    _git(
        project,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "initial",
    )
    run_id = "20260811-120000-11111111-agent"
    _git(project, "switch", "-q", "-c", f"vibesys-runs/{run_id}")
    (project / "OBJECTIVE.md").write_text("Objective on the run branch.\n")
    store = _write_project_run(
        project,
        run_id,
        configuration=_agent_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    state_pathspec = (
        store.state.git_integration(run_id)
        .resolve_snapshot(store.state.initialization_snapshot(run_id))
        .scope_pathspec
    )
    _git(project, "add", "OBJECTIVE.md", state_pathspec)
    _git(
        project,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "run state",
    )
    _git(project, "switch", "-q", "main")
    monkeypatch.chdir(project)

    invocation = parse_cli_invocation(["--resume", run_id])

    assert invocation.args.input_bundle.objective == "Objective on the run branch.\n"
    branch = subprocess.run(
        ["git", "branch", "--show-current"],  # noqa: S607
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == f"vibesys-runs/{run_id}"


def test_agent_resume_restores_its_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _write_input_project(tmp_path)
    run_id = "20260811-120000-11111111-agent"
    _write_project_run(
        project,
        run_id,
        configuration=_agent_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    monkeypatch.chdir(project)

    args = parse_cli_invocation(["--resume", run_id]).args
    config, _, backend = load_config_and_skills(args, domain=DomainName.GENERIC)

    assert args.max_rounds == 7
    assert args.max_retries_per_round == 4
    assert args.judge_every == 2
    assert args.official_eval_every == 5
    assert args.inner_loop == "single-agent"
    assert args.interface == "service"
    assert args.memory_layout == "directories"
    assert args.constraint == ["Preserve the ABI."]
    assert args.profiler is ProfilerKind.NONE
    assert args.cli_provider == "claude"
    assert backend is ComputeBackend.CPU
    assert config.model.name == "gpt-recorded"
    assert config.agent.cli_timeout == 321
    assert config.agent.driver == "omnigent"
    assert config.agent.outer.model == "gpt-outer"
    assert config.agent.inner.model == "gpt-inner"


def test_plain_resume_restores_its_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _write_input_project(tmp_path)
    run_id = "20260811-120000-11111111-plain"
    _write_project_run(
        project,
        run_id,
        configuration=_plain_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    monkeypatch.chdir(project)

    args = parse_cli_invocation(["--outer-loop", "plain", "--resume", run_id]).args

    assert args.max_rounds == 6
    assert args.max_attempts_per_issue == 4
    assert args.max_issues_per_perf_eval == 2
    assert args.agent_backend == "cli"
    assert args.cli_provider == "claude"
    assert args.backend is ComputeBackend.CPU
    assert args.profiler is ProfilerKind.NONE


def test_evolve_resume_restores_its_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _write_input_project(tmp_path)
    run_id = "20260811-120000-11111111-evolve"
    _write_project_run(
        project,
        run_id,
        configuration=_evolve_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    monkeypatch.chdir(project)

    args = parse_cli_invocation(["--outer-loop", "evolve", "--resume", run_id]).args

    assert args.max_generations == 5
    assert args.children_per_generation == 3
    assert args.k_top_inspirations == 4
    assert args.k_random_inspirations == 1
    assert args.selection_temperature == 0.25
    assert args.seed == 17
    assert args.search_policy == "openevolve"
    assert args.openevolve_population_size == 40
    assert args.openevolve_archive_size == 20
    assert args.openevolve_num_islands == 3
    assert args.openevolve_migration_interval == 4
    assert args.openevolve_migration_rate == 0.25
    assert args.frontier_bias == 0.6
    assert args.bootstrap_max_attempts == 7
    assert args.keep_deployments is True
    assert args.max_parallelism == 2
    assert [(item.name, item.direction) for item in args.objective] == [
        ("latency", "min"),
        ("throughput", "max"),
    ]


@pytest.mark.parametrize(
    "case",
    [
        ("agent", _agent_configuration(max_rounds=7), "--max-rounds", 7, 8),
        ("plain", _plain_configuration(max_rounds=6), "--max-rounds", 6, 7),
        ("evolve", _evolve_configuration(max_generations=5), "--max-generations", 5, 6),
    ],
)
def test_resume_budgets_can_only_increase(
    case: tuple[str, RunConfiguration, str, int, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, configuration, budget_flag, recorded, increased = case
    project = _write_input_project(tmp_path)
    run_id = f"20260811-120000-11111111-{loop}"
    _write_project_run(
        project,
        run_id,
        configuration=configuration,
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    monkeypatch.chdir(project)
    base = ["--outer-loop", loop, "--resume", run_id]

    omitted = parse_cli_invocation(base)
    raised = parse_cli_invocation([*base, budget_flag, str(increased)])

    destination = budget_flag.removeprefix("--").replace("-", "_")
    assert getattr(omitted.args, destination) == recorded
    assert getattr(raised.args, destination) == increased
    with pytest.raises(ConfigurationError, match="cannot decrease"):
        parse_cli_invocation([*base, budget_flag, str(recorded - 1)])


def test_resume_rejects_an_outer_loop_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _write_input_project(tmp_path)
    run_id = "20260811-120000-11111111-plain"
    _write_project_run(
        project,
        run_id,
        configuration=_plain_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    monkeypatch.chdir(project)

    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation(["--outer-loop", "agent", "--resume", run_id])
    assert exc.value.diagnostic.code == "project_resume_configuration_mismatch"
    assert "--outer-loop plain" in exc.value.diagnostic.message


def test_resume_rejects_changes_to_immutable_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _write_input_project(tmp_path)
    run_id = "20260811-120000-11111111-agent"
    _write_project_run(
        project,
        run_id,
        configuration=_agent_configuration(),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    monkeypatch.chdir(project)

    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation(["--resume", run_id, "--judge-every", "3"])
    assert exc.value.diagnostic.code == "project_resume_configuration_mismatch"
    assert "judge_every" in exc.value.diagnostic.message


def _write_pre_migration_run(project: Path, run_id: str) -> None:
    """Rewrite a run manifest as a schema version 1 recording."""
    path = project / ".vibesys" / "state" / "runs" / run_id / "run.json"
    raw = json.loads(path.read_text())
    raw["schema_version"] = 1
    del raw["configuration"]["run_environment"]
    path.write_text(json.dumps(raw, indent=2, sort_keys=True))


def _modal_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """Provision a project whose recorded run executes on Modal."""
    project = _write_input_project(tmp_path)
    run_id = "20260811-120000-11111111-agent"
    _write_project_run(
        project,
        run_id,
        configuration=_agent_configuration(run_environment=_MODAL_ENVIRONMENT),
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    monkeypatch.chdir(project)
    return project, run_id


def test_fresh_run_records_the_selected_run_environment(tmp_path: Path) -> None:
    project = _write_input_project(tmp_path)
    args = argparse.Namespace(
        input_bundle=load_input_bundle(project),
        docker=False,
        docker_image=None,
        modal=True,
        modal_gpu="A100-80GB",
        modal_model_volume="weights",
        modal_app="run-app",
    )

    record = run_environment_record(run_environment_spec_from_args(args))

    assert record == _MODAL_ENVIRONMENT


def test_resume_without_run_environment_flags_restores_the_recorded_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_id = _modal_run(tmp_path, monkeypatch)

    args = parse_cli_invocation(["--resume", run_id]).args

    assert args.modal is True
    assert args.docker is False
    assert args.modal_gpu == "A100-80GB"
    assert args.modal_model_volume == "weights"
    assert args.modal_app == "run-app"
    assert run_environment_record(run_environment_spec_from_args(args)) == _MODAL_ENVIRONMENT


def test_resume_with_matching_run_environment_flags_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_id = _modal_run(tmp_path, monkeypatch)

    args = parse_cli_invocation(
        [
            "--resume",
            run_id,
            "--modal",
            "--modal-gpu",
            "A100-80GB",
            "--modal-model-volume",
            "weights",
            "--modal-app",
            "run-app",
        ]
    ).args

    assert run_environment_record(run_environment_spec_from_args(args)) == _MODAL_ENVIRONMENT


@pytest.mark.parametrize(
    ("flags", "field"),
    [
        (["--docker"], "run_environment"),
        (["--modal", "--modal-gpu", "H100!"], "run_environment.gpu"),
    ],
)
def test_resume_rejects_run_environment_flags_that_contradict_the_recording(
    flags: list[str],
    field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_id = _modal_run(tmp_path, monkeypatch)

    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation(["--resume", run_id, *flags])
    assert exc.value.diagnostic.code == "project_resume_configuration_mismatch"
    assert field in exc.value.diagnostic.message


def test_resume_rejects_a_recording_without_a_run_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_id = _modal_run(tmp_path, monkeypatch)
    _write_pre_migration_run(project, run_id)

    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation(["--resume", run_id])

    assert exc.value.diagnostic.code == "project_run_schema_migration_required"
    assert "migrate-run-environment" in exc.value.diagnostic.message
    assert run_id in exc.value.diagnostic.message


def test_migrated_recording_resumes_with_the_supplied_run_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_id = _modal_run(tmp_path, monkeypatch)
    _write_pre_migration_run(project, run_id)

    _run_migrate_run_environment(
        [
            "--project",
            str(project),
            "--run",
            run_id,
            "--run-environment",
            "modal",
            "--modal-gpu",
            "A100-80GB",
            "--modal-model-volume",
            "weights",
            "--modal-app",
            "run-app",
        ]
    )

    args = parse_cli_invocation(["--resume", run_id]).args
    assert args.modal is True
    assert run_environment_record(run_environment_spec_from_args(args)) == _MODAL_ENVIRONMENT
    recorded = Project.open(project).state.load_run(run_id)
    assert recorded.schema_version == RUN_SCHEMA_VERSION
    assert recorded.configuration.run_environment == _MODAL_ENVIRONMENT


def test_migration_rejects_an_already_migrated_recording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_id = _modal_run(tmp_path, monkeypatch)

    with pytest.raises(ConfigurationError) as exc:
        _run_migrate_run_environment(
            ["--project", str(project), "--run", run_id, "--run-environment", "modal"]
        )

    assert exc.value.diagnostic.code == "migration_failed"
    assert "already at run schema version" in exc.value.diagnostic.message


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("latency", "must be 'name:max' or 'name:min'"),
        (":max", "metric name is empty"),
        ("latency:avg", "direction must be 'max' or 'min'"),
    ],
)
def test_parse_cli_objective_rejects_malformed_specs(spec: str, message: str) -> None:
    with pytest.raises(Exception) as exc:  # noqa: PT011
        _parse_cli_objective(spec)
    assert message in str(exc.value)


def test_objective_files_are_optional_and_validated(tmp_path: Path) -> None:
    """The axes and the tolerance are one fact, read together or not at all."""
    assert _load_metric_space_toml(tmp_path) == MetricSpace()

    (tmp_path / "objectives.toml").write_text(
        '[[objective]]\nname = "latency"\ndirection = "min"\n[pareto]\nrelative_noise = 0.03\n'
    )
    assert _load_metric_space_toml(tmp_path) == MetricSpace(
        objectives=(Objective(name="latency", direction="min"),),
        relative_noise=0.03,
    )


@pytest.mark.parametrize("value", ["true", "-0.1", "1.0", '"noisy"'])
def test_pareto_relative_noise_rejects_invalid_values(tmp_path: Path, value: str) -> None:
    (tmp_path / "objectives.toml").write_text(f"[pareto]\nrelative_noise = {value}\n")
    with pytest.raises(ValueError, match=r"pareto\.relative_noise"):
        _load_metric_space_toml(tmp_path)


def test_render_configuration_error_prints_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = ConfigurationError(
        ConfigurationDiagnostic(
            code="invalid_arguments",
            stage="argument_parsing",
            message="bad args",
            usage="usage: vibesys ...",
        )
    )
    with pytest.raises(SystemExit) as exc:
        _render_configuration_error(error)
    assert exc.value.code == 2
    assert "bad args" in capsys.readouterr().err


@pytest.mark.parametrize("loop", ["agent", "plain", "evolve"])
def test_main_routes_to_the_selected_loop(
    loop: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`create_session` dispatches to the loop function selected by `LoopKind`."""
    project = _write_input_project(tmp_path)
    runner = Mock(return_value=True)
    monkeypatch.setattr(_LOOP_RUN_TARGETS[loop], runner)
    argv = ["vibesys", "--outer-loop", loop, "--input", str(project)]

    with patch.object(sys, "argv", argv):
        main()

    runner.assert_called_once()
    assert runner.call_args.kwargs["input_path"] == str(project.resolve())


def test_dispatch_owns_headless_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys  # noqa: PLC0415

    from entrypoints import cli  # noqa: PLC0415

    headless_run_module = sys.modules["headless.execute"]

    project = _write_input_project(tmp_path)
    renderer = Mock()
    monkeypatch.setattr(headless_run_module, "HeadlessRenderer", lambda: renderer)
    monkeypatch.setattr(_LOOP_RUN_TARGETS["agent"], Mock(return_value=True))

    cli.dispatch(["--input", str(project)])

    assert [call.args[0].type for call in renderer.handle.call_args_list] == [
        CoreEventType.RUN_STARTED,
        CoreEventType.RUN_FINISHED,
    ]


def test_dispatch_records_failure_through_the_headless_renderer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys  # noqa: PLC0415

    from entrypoints import cli  # noqa: PLC0415

    headless_run_module = sys.modules["headless.execute"]

    project = _write_input_project(tmp_path)
    renderer = Mock()
    monkeypatch.setattr(headless_run_module, "HeadlessRenderer", lambda: renderer)
    monkeypatch.setattr(_LOOP_RUN_TARGETS["agent"], Mock(side_effect=RuntimeError("runner failed")))

    with pytest.raises(RuntimeError, match="runner failed"):
        cli.dispatch(["--input", str(project)])

    assert [call.args[0].type for call in renderer.handle.call_args_list] == [
        CoreEventType.RUN_STARTED,
        CoreEventType.RUN_FAILED,
    ]


def _write_microservice_project(parent: Path, *, traced: bool, name: str = "hotel") -> Path:
    """Write a microservice bundle that does or does not capture a trace graph."""
    project = parent / name
    (project / "src").mkdir(parents=True)
    (project / "OBJECTIVE.md").write_text("Make the request path faster.\n")
    telemetry = (
        ', "--telemetry-output", "/tmp/otel/telemetry.json"'
        ', "--trace-graph-json", "/tmp/otel/trace-graph.json"'
        if traced
        else ""
    )
    (project / "vibesys.input.toml").write_text(
        f"""\
version = 1

[agent]
domain = "microservices"

[accuracy]
command = ["python", "-c", "print('ok')"]

[benchmark]
command = ["servicebench", "trace", "--candidate-dir", "svc"{telemetry}]

[benchmark.result]
json_argument = "--output-json"
metric = "primary_value"
"""
    )
    return project


def test_instrumented_microservice_task_profiles_with_otel_by_default(tmp_path: Path) -> None:
    """A task that provisions tracing is why ``auto`` can safely mean OTel.

    Without this the default run resolves to ``none`` for microservices, the
    profiler role never executes, and no critical-path evidence is produced.
    """
    from entrypoints import cli  # noqa: PLC0415

    project = _write_microservice_project(tmp_path, traced=True)
    args = cli._build_agent_parser().parse_args(["--input", str(project)])  # noqa: SLF001
    args.input_bundle = load_input_bundle(project)

    assert args.profiler is ProfilerKind.AUTO
    cli._apply_bundle_profiler_default(args)  # noqa: SLF001

    assert args.profiler is ProfilerKind.OTEL


def test_uninstrumented_microservice_task_keeps_auto(tmp_path: Path) -> None:
    """Without a collector there is nothing for the OTel profiler to read."""
    from entrypoints import cli  # noqa: PLC0415

    project = _write_microservice_project(tmp_path, traced=False)
    args = cli._build_agent_parser().parse_args(["--input", str(project)])  # noqa: SLF001
    args.input_bundle = load_input_bundle(project)

    cli._apply_bundle_profiler_default(args)  # noqa: SLF001

    assert args.profiler is ProfilerKind.AUTO


def test_explicit_profiler_flag_overrides_the_task_default(tmp_path: Path) -> None:
    """The bundle only supplies a default; the operator stays in control."""
    from entrypoints import cli  # noqa: PLC0415

    project = _write_microservice_project(tmp_path, traced=True)
    args = cli._build_agent_parser().parse_args(  # noqa: SLF001
        ["--input", str(project), "--profiler", "none"]
    )
    args.input_bundle = load_input_bundle(project)

    cli._apply_bundle_profiler_default(args)  # noqa: SLF001

    assert args.profiler is ProfilerKind.NONE


def test_non_microservice_task_is_unaffected_by_trace_arguments(tmp_path: Path) -> None:
    """OTel is microservices-only; a generic bundle must not be upgraded."""
    from entrypoints import cli  # noqa: PLC0415

    project = _write_microservice_project(tmp_path, traced=True, name="generic-task")
    manifest = project / "vibesys.input.toml"
    manifest.write_text(manifest.read_text().replace('"microservices"', '"generic"'))
    args = cli._build_agent_parser().parse_args(["--input", str(project)])  # noqa: SLF001
    args.input_bundle = load_input_bundle(project)

    cli._apply_bundle_profiler_default(args)  # noqa: SLF001

    assert args.profiler is ProfilerKind.AUTO


def test_expected_role_registry_covers_every_outer_loop() -> None:
    """The advertised contract must name every loop dispatch can select."""
    from entrypoints import cli  # noqa: PLC0415

    assert set(EXPECTED_AGENT_ROLES) == set(cli._OUTER_LOOPS)  # noqa: SLF001
    assert set(EXPECTED_AGENT_ROLES) == set(cli._LOOP_COMMANDS)  # noqa: SLF001
    assert all(EXPECTED_AGENT_ROLES.values())


@pytest.mark.parametrize("loop", ["agent", "plain", "evolve"])
def test_dispatch_advertises_expected_roles_on_run_started(
    loop: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys  # noqa: PLC0415

    from entrypoints import cli  # noqa: PLC0415

    headless_run_module = sys.modules["headless.execute"]

    project = _write_input_project(tmp_path)
    renderer = Mock()
    monkeypatch.setattr(headless_run_module, "HeadlessRenderer", lambda: renderer)
    monkeypatch.setattr(_LOOP_RUN_TARGETS[loop], Mock(return_value=True))

    cli.dispatch(["--outer-loop", loop, "--input", str(project)])

    started = next(
        call.args[0]
        for call in renderer.handle.call_args_list
        if call.args[0].type is CoreEventType.RUN_STARTED
    )
    assert isinstance(started.data, RunStartedData)
    assert started.data.expected_roles == EXPECTED_AGENT_ROLES[loop]
    assert started.data.expected_roles


@pytest.mark.parametrize("loop", ["agent", "evolve"])
def test_dispatch_omits_profiler_role_when_profiler_is_disabled(
    loop: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled profiler never runs, so its placeholder must not be seeded."""
    import sys  # noqa: PLC0415

    from entrypoints import cli  # noqa: PLC0415

    headless_run_module = sys.modules["headless.execute"]

    project = _write_input_project(tmp_path)
    renderer = Mock()
    monkeypatch.setattr(headless_run_module, "HeadlessRenderer", lambda: renderer)
    monkeypatch.setattr(_LOOP_RUN_TARGETS[loop], Mock(return_value=True))

    cli.dispatch(["--outer-loop", loop, "--input", str(project), "--profiler", "none"])

    started = next(
        call.args[0]
        for call in renderer.handle.call_args_list
        if call.args[0].type is CoreEventType.RUN_STARTED
    )
    assert "profiler" not in started.data.expected_roles
    assert started.data.expected_roles == tuple(
        role for role in EXPECTED_AGENT_ROLES[loop] if role != "profiler"
    )
