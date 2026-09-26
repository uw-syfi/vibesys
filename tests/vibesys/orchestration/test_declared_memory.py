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

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.loops.legacy_runner import LegacyOrchestrator, run_orchestration

from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationDescriptor,
    ProfilerKind,
    RunRequest,
)
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle
from vibesys.context import RunSetup
from vibesys.run.integration import LocalRunIntegration

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

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


def _run_legacy(request: RunRequest, policy: LegacyOrchestrator) -> bool:
    """Exercise host-owned memory behavior without cataloging old policies."""
    integration = LocalRunIntegration()
    try:
        return asyncio.run(run_orchestration(request, integration, policy))
    finally:
        integration.close()


def test_rollback_style_restore_preserves_declared_memory(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    request = _request(project_root)
    assert _run_legacy(request, _RestorePolicy(request.orchestration))


def test_transaction_restore_preserves_declared_memory(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    request = _request(project_root)
    assert _run_legacy(request, _TransactionPolicy(request.orchestration))


# ---------------------------------------------------------------------------
# Property test: declared memory survives any rollback style/content.
# ---------------------------------------------------------------------------

_ascii_text = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=8
)


class _RollbackPolicy:
    """Parametrized rollback shape: plain ``restore()`` or a transaction,
    with arbitrary round-2 content for both the declared-memory file and a
    plain candidate file.
    """

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        assert descriptor.id == "memory-preserving"
        options = descriptor.options
        self.style = str(options["style"])
        self.round2_code = str(options["round2_code"])
        self.round2_memory = str(options["round2_memory"])
        self.setup = RunSetup(memory_paths=("progress.md",))

    async def run(self, ctx: RunContext) -> bool:
        root = ctx.workspaces.root
        (root.path / "code.py").write_text("VALUE = 1\n")
        (root.path / "progress.md").write_text("round 1: baseline\n")
        baseline = await root.snapshot("baseline")

        if self.style == "restore":
            (root.path / "code.py").write_text(self.round2_code)
            (root.path / "progress.md").write_text(self.round2_memory)
            await root.snapshot("round-2")
            await root.restore(baseline, clean=True)
        else:
            async with root.transaction() as tx:
                (root.path / "code.py").write_text(self.round2_code)
                (root.path / "progress.md").write_text(self.round2_memory)
                del tx  # not committed: exit restores, but keeps declared memory

        assert (root.path / "code.py").read_text() == "VALUE = 1\n"
        assert (root.path / "progress.md").read_text() == self.round2_memory
        return True


@given(
    style=st.sampled_from(["restore", "transaction"]),
    round2_code=_ascii_text,
    round2_memory=_ascii_text,
)
@settings(max_examples=6, deadline=None)
def test_declared_memory_survives_any_rollback(
    tmp_path_factory: pytest.TempPathFactory,
    style: str,
    round2_code: str,
    round2_memory: str,
) -> None:
    """Declared memory (``progress.md``) always survives a rollback, whatever
    the round-2 content and whichever rollback shape (bare restore or an
    uncommitted transaction) a strategy uses.
    """
    tmp_path = tmp_path_factory.mktemp("declared-memory-property")
    project_root = tmp_path / "project"
    _write_project(project_root)
    request = _request(project_root)
    request = request.model_copy(
        update={
            "orchestration": OrchestrationDescriptor(
                id="memory-preserving",
                config_version=1,
                options={
                    "style": style,
                    "round2_code": round2_code,
                    "round2_memory": round2_memory,
                },
            )
        }
    )
    assert _run_legacy(request, _RollbackPolicy(request.orchestration))
