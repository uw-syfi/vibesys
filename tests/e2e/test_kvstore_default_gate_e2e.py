"""The public kvstore-default accuracy gate is self-contained after materialization."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from tests.support import run_test_command

from vibesys.api.request import load_project_task
from vs_project.api import Project


@pytest.mark.native_build
def test_kvstore_default_accuracy_gate_runs_with_an_empty_offline_go_cache(
    tmp_path: Path,
) -> None:
    """A cold evaluator host can execute the configured trusted gate without a proxy."""
    if shutil.which("go") is None:
        pytest.skip("Go is required by the trusted kvstore-default accuracy gate")
    if shutil.which("uv") is None:
        pytest.skip("uv is required by the configured kvstore-default task command")

    repository = Path(__file__).parents[2]
    source = repository / "examples" / "data-structures" / "kvstore-default"
    project_root = tmp_path / "kvstore-default"
    shutil.copytree(source, project_root)

    project = Project.open(project_root)
    task = project.select_task("default")
    bundle = load_project_task(project, task)
    command = [
        *bundle.accuracy_command,
        "--use-reference",
        "--clients",
        "1",
        "--ops",
        "4",
        "--key-space",
        "1",
    ]
    go_module_cache = tmp_path / "empty-go-module-cache"
    go_module_cache.mkdir()
    environment = {
        **os.environ,
        "GOCACHE": str(tmp_path / "go-build-cache"),
        "GOMODCACHE": str(go_module_cache),
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "UV_OFFLINE": "1",
    }

    result = run_test_command(
        command,
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS - history is linearizable" in result.stdout
    assert not any(go_module_cache.iterdir())
