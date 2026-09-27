"""Public contract tests for privileged Docker workspace maintenance."""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.docker_workspace import (
    DockerWorkspaceRepairError,
    remove_docker_workspace_child,
    repair_docker_workspace,
)

if TYPE_CHECKING:
    from pathlib import Path


def _fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, exit_code: int = 0) -> Path:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    call_log = tmp_path / "docker-call.json"
    executable = executable_dir / "docker"
    executable.write_text(
        f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(call_log)!r}).write_text(json.dumps(sys.argv[1:]))
print("maintenance failed", file=sys.stderr)
raise SystemExit({exit_code})
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{executable_dir}{os.pathsep}{os.environ['PATH']}")
    return call_log


def test_repair_uses_selected_image_and_host_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    call_log = _fake_docker(tmp_path, monkeypatch)

    repair_docker_workspace(workspace, image="editor@sha256:abc")

    arguments = json.loads(call_log.read_text(encoding="utf-8"))
    assert arguments == [
        "run",
        "--rm",
        "-v",
        f"{workspace}:/workspace",
        "editor@sha256:abc",
        "bash",
        "-c",
        f"chown -R {os.getuid()}:{os.getgid()} /workspace",
    ]


def test_repair_reports_docker_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _fake_docker(tmp_path, monkeypatch, exit_code=17)

    with pytest.raises(
        DockerWorkspaceRepairError,
        match=rf"chown failed for {workspace} \(rc=17\): maintenance failed",
    ):
        repair_docker_workspace(workspace, image="editor")


def test_repair_ignores_absent_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    call_log = _fake_docker(tmp_path, monkeypatch)

    repair_docker_workspace(tmp_path / "absent", image="editor")

    assert not call_log.exists()


def test_remove_child_quotes_shell_metacharacters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    call_log = _fake_docker(tmp_path, monkeypatch)

    removed = remove_docker_workspace_child(
        workspace,
        "semi;touch hacked",
        image="editor",
    )

    assert removed is True
    arguments = json.loads(call_log.read_text(encoding="utf-8"))
    assert arguments[-1] == "rm -rf -- '/workspace/semi;touch hacked'"


def test_remove_child_reports_remaining_target_when_docker_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "child"
    target.mkdir(parents=True)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    assert remove_docker_workspace_child(workspace, "child", image="editor") is False
