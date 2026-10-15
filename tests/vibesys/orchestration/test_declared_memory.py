"""Artifact directories declared by a plugin's core policy are preserved by the run.

A strategy declares the workspace paths its artifacts live in once, as the core policy's
``artifact_directories``, and the run merges them into every ``adopt``/``restore`` call
automatically, whether or not the caller names them explicitly. This covers a bare
``workspace.restore()``, which is what a rollback boils down to.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.core_host_world import open_core_host
from tests.support.docker_environment import fake_docker_environment
from tests.support.real_git import NO_GITHUB, REAL_GIT
from tests.support.task_project import write_task
from tests.vibesys.orchestration.plugin import capability_plugin

from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationDescriptor,
    ProfilerKind,
    RunRequest,
)
from vibesys.api.request import load_task
from vibesys.run.integration import LocalRunIntegration
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api import OsThreads
from vs_sim.api.testing import ManualClock, examples

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_PLUGIN = capability_plugin("memory-preserving", artifact_directories=("progress.md",))


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "code.py").write_text("VALUE = 1\n")
    (root / "progress.md").write_text("round 1: baseline\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Improve the queue.\n",
    )


def _request(project_root: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id="memory-preserving", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_task(project_root),
        objective="Improve the queue.",
        exp_name="team-demo",
        run_environment=fake_docker_environment(),
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _run_restore(request: RunRequest, code: str, memory: str) -> None:
    """Exercise declared-memory restore through a real product-composed run."""
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )

    async def exercise() -> None:
        async with open_core_host(request, integration, plugin=_PLUGIN) as run:
            root = run.workspaces.root
            (root.path / "code.py").write_text("VALUE = 1\n")
            (root.path / "progress.md").write_text("round 1: baseline\n")
            baseline = await root.snapshot("baseline")

            (root.path / "code.py").write_text(code)
            (root.path / "progress.md").write_text(memory)
            await root.snapshot("round-2")
            await root.restore(baseline, clean=True)

            assert (root.path / "code.py").read_text() == "VALUE = 1\n"
            assert (root.path / "progress.md").read_text() == memory

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


def test_rollback_style_restore_preserves_declared_memory(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    request = _request(project_root)
    _run_restore(request, "VALUE = 2\n", "round 2: tried a thing\n")


# ---------------------------------------------------------------------------
# Property test: declared memory survives any rollback style/content.
# ---------------------------------------------------------------------------

_ascii_text = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=8
)


@given(
    round2_code=_ascii_text,
    round2_memory=_ascii_text,
)
@settings(max_examples=examples(pr=2, nightly=6), deadline=None)
def test_declared_memory_survives_any_rollback(
    tmp_path_factory: pytest.TempPathFactory,
    round2_code: str,
    round2_memory: str,
) -> None:
    """Declared memory survives restore for arbitrary round-2 content."""
    tmp_path = tmp_path_factory.mktemp("declared-memory-property")
    project_root = tmp_path / "project"
    _write_project(project_root)
    request = _request(project_root)
    _run_restore(request, round2_code, round2_memory)
