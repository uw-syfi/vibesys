"""Public run-control metadata writers never publish partial documents."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from tests.support.file_effects import file_size_limit

from vs_sandbox.api import DockerSandbox
from vs_sandbox.api.slurm import (
    SlurmCapturePlan,
    SlurmEvaluationPlan,
    read_slurm_capture_plan,
    read_slurm_evaluation_plan,
    write_slurm_capture_plan,
    write_slurm_evaluation_plan,
)


def test_capture_plan_interrupted_at_every_byte_remains_readable(tmp_path: Path) -> None:
    path = tmp_path / "capture.json"
    old = SlurmCapturePlan(profile_command=("true",))
    new = SlurmCapturePlan(profile_command=("echo", "new command"))
    for interruption in range(len(new.model_dump_json().encode()) + 1):
        write_slurm_capture_plan(path, old)
        with file_size_limit(interruption), pytest.raises(OSError, match="File too large"):
            write_slurm_capture_plan(path, new)
        assert read_slurm_capture_plan(path) == old
        assert not list(path.parent.glob(".*.tmp"))


def test_evaluation_plan_interrupted_at_every_byte_remains_readable(tmp_path: Path) -> None:
    path = tmp_path / "evaluation.json"
    old = SlurmEvaluationPlan(config_path=Path("config.toml"), accuracy_command=("true",))
    new = old.model_copy(update={"benchmark_command": ("echo", "new command")})
    for interruption in range(len(new.model_dump_json().encode()) + 1):
        write_slurm_evaluation_plan(path, old)
        with file_size_limit(interruption), pytest.raises(OSError, match="File too large"):
            write_slurm_evaluation_plan(path, new)
        assert read_slurm_evaluation_plan(path) == old
        assert not list(path.parent.glob(".*.tmp"))


def test_docker_metadata_interrupted_at_every_byte_preserves_last_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "bin" / "docker"
    executable.parent.mkdir()
    executable.write_text(
        f"""#!{sys.executable}
import sys
from pathlib import Path
state = Path({str(tmp_path / "container-state")!r})
args = sys.argv[1:]
container = "test-container"
if args and args[0] == "run" and args[-2:] == ["sleep", "infinity"]:
    if state.exists():
        sys.exit(64)
    state.write_bytes(b"running")
    print(container)
elif args == ["exec", container, "sh", "-c", "id -u agent && id -g agent"]:
    if not state.exists() or state.read_bytes() != b"running":
        sys.exit(64)
    print({os.getuid()})
    print({os.getgid()})
elif args == ["stop", container] and state.exists():
    state.write_bytes(b"stopped")
elif args == ["rm", "-f", container] and state.exists():
    state.unlink()
else:
    sys.exit(64)
"""
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{executable.parent}:{os.environ['PATH']}")
    sandbox = DockerSandbox(str(tmp_path), image="test-image")
    path = tmp_path / ".docker_metadata.json"
    sandbox.start()
    try:
        sandbox.save_symlink_commands(["old"])
        old = path.read_bytes()
        for interruption in range(len(old) + 1):
            with file_size_limit(interruption):
                sandbox.save_symlink_commands(["new-command"])
            assert path.read_bytes() == old
            assert not list(path.parent.glob(".*.tmp"))
    finally:
        sandbox.stop()
