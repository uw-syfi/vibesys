"""Invalid placement and profiler choices fail before a session can start."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from launch import create_session
from vibesys.api import (
    ComputeBackend,
    Config,
    ConfigurationError,
    OrchestrationDescriptor,
    ProfilerKind,
    RunRequest,
)
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import CoreEvent


def _request(root: Path, domain: str) -> RunRequest:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Improve inference.\n")
    (root / "vibesys.input.toml").write_text(
        f'version = 1\n[agent]\ndomain = "{domain}"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )
    return RunRequest(
        project_root=root,
        input_bundle=load_input_bundle(root),
        orchestration=OrchestrationDescriptor(id="multi-agent", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "test"}}),
        exp_name="fail-fast",
        profiler_kind=ProfilerKind.NONE,
        run_environment=RunEnvironmentSpec("docker"),
    )


@pytest.mark.parametrize(
    ("profiler", "domain", "backend"),
    [
        *[
            (kind, "generic", ComputeBackend.CUDA)
            for kind in (
                ProfilerKind.NSYS,
                ProfilerKind.NCU,
                ProfilerKind.ROCPROF,
                ProfilerKind.OTEL,
                ProfilerKind.TORCH,
                ProfilerKind.NEURON,
                ProfilerKind.HEADROOM,
            )
        ],
        (ProfilerKind.LINUX_CPU, "llm-serving", ComputeBackend.CPU),
        (ProfilerKind.MACOS_CPU, "llm-serving", ComputeBackend.CPU),
        *[
            (ProfilerKind.NCU, "kernel-writing", backend)
            for backend in ComputeBackend
            if backend is not ComputeBackend.CUDA
        ],
    ],
)
def test_incompatible_profiler_is_rejected_at_session_construction(
    tmp_path: Path, profiler: ProfilerKind, domain: str, backend: ComputeBackend
) -> None:
    request = _request(tmp_path / "input", domain).model_copy(
        update={"profiler_kind": profiler, "backend": backend}
    )
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    with pytest.raises(ConfigurationError) as raised:
        create_session(request, sink=record)
    assert raised.value.diagnostic.code == "profiler_incompatible"
    assert "--profiler" in str(raised.value)
    assert profiler.value in str(raised.value)
    assert domain in str(raised.value)
    assert backend.value in str(raised.value)
    assert not events


@pytest.mark.parametrize("git_marker", ["directory", "file"])
@pytest.mark.parametrize("depth", range(4))
def test_collection_in_repository_fails_before_session_start(
    tmp_path: Path, git_marker: str, depth: int
) -> None:
    containing = tmp_path / "repository"
    containing.mkdir()
    run_test_command(["git", "init", "-q", str(containing)], check=True)
    if git_marker == "file":
        run_test_command(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "baseline",
            ],
            cwd=containing,
            check=True,
        )
        worktree = tmp_path / "worktree"
        run_test_command(
            ["git", "worktree", "add", "-q", "--detach", str(worktree)],
            cwd=containing,
            check=True,
        )
        containing = worktree
    runs_dir = containing.joinpath(*[f"part-{number}" for number in range(depth)])
    request = _request(tmp_path / "input", "generic").model_copy(update={"runs_dir": runs_dir})
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    with pytest.raises(ConfigurationError, match="--runs-dir") as raised:
        create_session(request, sink=record)
    assert raised.value.diagnostic.code == "invalid_runs_dir"
    assert str(containing) in str(raised.value)
    assert not events
