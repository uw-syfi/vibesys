"""CLI serving profile validation precedes repository and environment effects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from entrypoints.cli import build_run_request, parse_cli_invocation
from entrypoints.headless import main
from vibesys.api import ConfigurationError, ProfilerKind

if TYPE_CHECKING:
    from pathlib import Path


def _arguments(root: Path, profiler: ProfilerKind) -> list[str]:
    project = root / "project"
    project.mkdir()
    (project / "OBJECTIVE.md").write_text("Reduce latency.\n", encoding="utf-8")
    (project / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "llm-serving"\n'
        '[accuracy]\ncommand = ["python", "check.py"]\n'
        '[benchmark]\ncommand = ["python", "benchmark.py"]\n',
        encoding="utf-8",
    )
    operator = root / "slurm.toml"
    operator.write_text(
        '[slurm]\nname = "cluster"\nremote_workspace_root = "/remote/runs"\n'
        '[slurm.transport]\nkind = "ssh"\nhost = "cluster"\n'
        '[vibesys.service]\ncommand = ["python", "serve.py", "--port", '
        '"VIBESYS_DYNAMIC_PORT"]\n'
        'readiness_url = "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/health"\n'
        "startup_timeout_seconds = 700\n",
        encoding="utf-8",
    )
    return [
        "--input",
        str(project),
        "--backend",
        "rocm",
        "--slurm-config",
        str(operator),
        "--profiler",
        profiler.value,
        "--no-skills",
        "--local",
    ]


@pytest.mark.parametrize("profiler", [ProfilerKind.AUTO, ProfilerKind.ROCPROF])
def test_cli_rejects_missing_profile_before_repository_preparation(
    tmp_path: Path, profiler: ProfilerKind
) -> None:
    invocation = parse_cli_invocation(_arguments(tmp_path, profiler))
    before = set(tmp_path.rglob("*"))
    assert invocation.args.exp_name is None
    with pytest.raises(ConfigurationError, match=r"profile\.command") as error:
        build_run_request(invocation)
    assert error.value.diagnostic.code == "profile_workload_invalid"
    assert invocation.args.exp_name is None
    assert invocation.args.repo is None
    assert set(tmp_path.rglob("*")) == before


def test_headless_renders_missing_profile_as_configuration_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = _arguments(tmp_path, ProfilerKind.ROCPROF)
    before = set(tmp_path.rglob("*"))
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2
    assert "profile.command" in capsys.readouterr().err
    assert set(tmp_path.rglob("*")) == before
