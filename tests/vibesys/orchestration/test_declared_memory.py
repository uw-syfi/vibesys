"""``RunSetup.memory_paths`` is preserved automatically by the host.

A strategy used to pass ``preserve_paths=self._memory_paths()`` on every
``workspace.restore()`` call (rollback, isolation revert, final-candidate
selection). It now declares its memory paths once on ``RunSetup``, and the
host (``_Workspaces._with_declared_memory``) merges them into every
``adopt``/``restore``/``transaction`` call automatically, whether or not the
caller names them explicitly.

Covers the two call paths agent strategies use: a bare ``workspace.restore()``
(what a rollback -- and, after lane A's turns.py migrates, a role-isolation
revert -- both boil down to) and a ``workspace.transaction()``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationDescriptor,
    OrchestrationRegistry,
    ProfilerKind,
    RunRequest,
    create_session,
)
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle
from vibesys.context import RunSetup

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.orchestration.runtime import RunContext


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


class _RestorePolicy:
    """Rollback shape: a bare ``workspace.restore()`` with no explicit preserve_paths."""

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        assert descriptor.id == "memory-preserving"
        self.setup = RunSetup(memory_paths=("progress.md",))

    async def run(self, ctx: RunContext) -> bool:
        root = ctx.workspaces.root
        (root.path / "code.py").write_text("VALUE = 1\n")
        (root.path / "progress.md").write_text("round 1: baseline\n")
        baseline = await root.snapshot("baseline")

        (root.path / "code.py").write_text("VALUE = 2\n")
        (root.path / "progress.md").write_text("round 2: tried a thing\n")
        await root.snapshot("round-2")

        # Roll back to baseline without naming progress.md explicitly: the
        # host must preserve it because RunSetup declared it.
        await root.restore(baseline, clean=True)

        assert (root.path / "code.py").read_text() == "VALUE = 1\n"
        assert (root.path / "progress.md").read_text() == "round 2: tried a thing\n"
        return True


class _TransactionPolicy:
    """Isolation-revert shape: a failed/undone transaction body."""

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        assert descriptor.id == "memory-preserving"
        self.setup = RunSetup(memory_paths=("progress.md",))

    async def run(self, ctx: RunContext) -> bool:
        root = ctx.workspaces.root
        (root.path / "code.py").write_text("VALUE = 1\n")
        (root.path / "progress.md").write_text("round 1: baseline\n")

        async with root.transaction() as tx:
            (root.path / "code.py").write_text("VALUE = 2\n")
            (root.path / "progress.md").write_text("round 2: tried a thing\n")
            del tx  # not committed: exit restores, but must keep declared memory

        assert (root.path / "code.py").read_text() == "VALUE = 1\n"
        assert (root.path / "progress.md").read_text() == "round 2: tried a thing\n"
        return True


def _discard_event(event: object) -> None:
    del event


def test_rollback_style_restore_preserves_declared_memory(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    registry = OrchestrationRegistry()
    registry.register("memory-preserving", _RestorePolicy)
    session = create_session(_request(project_root), sink=_discard_event, registry=registry)
    session.start()
    result = asyncio.run(session.await_result())
    assert result.succeeded


def test_transaction_restore_preserves_declared_memory(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    registry = OrchestrationRegistry()
    registry.register("memory-preserving", _TransactionPolicy)
    session = create_session(_request(project_root), sink=_discard_event, registry=registry)
    session.start()
    result = asyncio.run(session.await_result())
    assert result.succeeded
