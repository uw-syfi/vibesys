"""Configuration failures render a flag diagnostic before headless execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from entrypoints.cli import parse_cli_invocation
from entrypoints.headless import main
from vibesys.api import ConfigurationError

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
    assert "Traceback" not in error
    assert not (tmp_path / "runs").exists()
