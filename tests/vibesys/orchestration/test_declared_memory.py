"""Plugin-declared memory paths are preserved automatically by the run.

A strategy used to pass ``preserve_paths=self._memory_paths()`` on every
``workspace.restore()`` call (rollback, isolation revert, final-candidate
selection). It now declares its memory paths once on its plugin, and the
run (``_Workspaces._with_declared_memory``) merges them into every
``adopt``/``restore`` call automatically, whether or not the
caller names them explicitly.

Covers the two call paths agent strategies use: a bare ``workspace.restore()``
(what a rollback -- and, after lane A's turns.py migrates, a role-isolation
revert boils down to).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.plugin import capability_plugin

from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationDescriptor,
    ProfilerKind,
    RunRequest,
)
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_PLUGIN = capability_plugin("memory-preserving", memory_paths=("progress.md",))


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "code.py").write_text("VALUE = 1\n")
    (root / "progress.md").write_text("round 1: baseline\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _request(project_root: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id="memory-preserving", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name="team-demo",
        run_environment=RunEnvironmentSpec("local"),
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _run_restore(request: RunRequest, code: str, memory: str) -> None:
    """Exercise declared-memory restore through a real product-composed run."""
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with open_product_run_host(
            request,
            integration,
            plugin=_PLUGIN,
        ) as run:
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
@settings(max_examples=6, deadline=None)
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
