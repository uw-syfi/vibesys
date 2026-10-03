"""Configuration failures render a flag diagnostic before headless execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from entrypoints.cli import build_run_request, parse_cli_invocation
from entrypoints.headless import main
from vibesys.api import ConfigurationError, ProfilerKind

if TYPE_CHECKING:
    from pathlib import Path


def _input_project(root: Path) -> Path:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Improve inference.\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )
    return root


@pytest.mark.parametrize("depth", range(4))
def test_cli_rejects_runs_collection_inside_git_repository(tmp_path: Path, depth: int) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    run_test_command(["git", "init", "-q", str(repository)], check=True)
    runs_dir = repository.joinpath(*[f"part-{number}" for number in range(depth)])
    with pytest.raises(ConfigurationError, match="--runs-dir") as raised:
        parse_cli_invocation(
            [
                "--input",
                str(_input_project(tmp_path / "input")),
                "--runs-dir",
                str(runs_dir),
            ]
        )
    assert raised.value.diagnostic.code == "invalid_runs_dir"
    assert str(repository) in str(raised.value)


def test_headless_invalid_profiler_prints_flag_diagnostic_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _input_project(tmp_path / "input")
    with pytest.raises(SystemExit) as raised:
        main(["--input", str(project), "--profiler", "nsys", "--runs-dir", str(tmp_path / "runs")])
    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert "--profiler" in error
    assert "generic" in error
    assert "GitHub CLI" not in error
    assert "Traceback" not in error
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("loop", ["agent", "plain", "evolve"])
@pytest.mark.parametrize("repository", [[], ["--repo", "trial"]], ids=["generated", "short-name"])
@pytest.mark.parametrize(
    "profiler",
    [
        ProfilerKind.NSYS,
        ProfilerKind.NCU,
        ProfilerKind.ROCPROF,
        ProfilerKind.OTEL,
        ProfilerKind.TORCH,
        ProfilerKind.NEURON,
        ProfilerKind.HEADROOM,
    ],
)
def test_invalid_profiler_precedes_unauthenticated_repository_setup(
    tmp_path: Path, loop: str, repository: list[str], profiler: ProfilerKind
) -> None:
    """Both entrypoints' shared builder diagnoses requests before GitHub auth."""
    invocation = parse_cli_invocation(
        [
            "--outer-loop",
            loop,
            "--input",
            str(_input_project(tmp_path / "input")),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--profiler",
            profiler.value,
            *repository,
        ]
    )
    with pytest.raises(ConfigurationError) as raised:
        build_run_request(invocation)
    assert raised.value.diagnostic.code == "profiler_incompatible"
    assert "--profiler" in str(raised.value)
    assert profiler.value in str(raised.value)
    assert not (tmp_path / "runs").exists()


def test_valid_request_still_requires_github_authentication(tmp_path: Path) -> None:
    invocation = parse_cli_invocation(
        [
            "--input",
            str(_input_project(tmp_path / "input")),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--profiler",
            "none",
        ]
    )
    with pytest.raises(ConfigurationError) as raised:
        build_run_request(invocation)
    assert raised.value.diagnostic.code == "repository_setup_failed"
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("loop", ["agent", "plain", "evolve"])
@pytest.mark.parametrize("repository", [[], ["--repo", "trial"]], ids=["generated", "short-name"])
def test_unknown_agent_role_precedes_unauthenticated_repository_setup(
    tmp_path: Path, loop: str, repository: list[str]
) -> None:
    config = tmp_path / "agent.toml"
    config.write_text('[model]\nname = "test"\n[agent.roles.unknown_role]\nmodel = "test"\n')
    invocation = parse_cli_invocation(
        [
            "--outer-loop",
            loop,
            "--input",
            str(_input_project(tmp_path / "input")),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--config",
            str(config),
            *repository,
        ]
    )
    with pytest.raises(ConfigurationError, match="unknown_role") as raised:
        build_run_request(invocation)
    assert raised.value.diagnostic.code == "agent_role_configuration_invalid"
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("loop", ["agent", "plain", "evolve"])
@pytest.mark.parametrize("remote", ["owner/project", "https://github.com/owner/project.git"])
def test_remote_resume_rejects_unknown_local_config_before_clone(
    tmp_path: Path, loop: str, remote: str
) -> None:
    config = tmp_path / "agent.toml"
    config.write_text('[model]\nname = "test"\nunknown_option = true\n')
    with pytest.raises(ConfigurationError, match="unknown_option") as raised:
        parse_cli_invocation(
            [
                "--outer-loop",
                loop,
                "--resume",
                remote,
                "--runs-dir",
                str(tmp_path / "runs"),
                "--config",
                str(config),
            ]
        )
    assert raised.value.diagnostic.code == "config_load_failed"
    assert not (tmp_path / "runs").exists()
