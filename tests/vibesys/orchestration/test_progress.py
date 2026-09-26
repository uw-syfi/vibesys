"""``ctx.progress``: the host-owned pending framework-log buffer.

Covers the host mechanism a strategy now uses instead of its own
``self._board_log`` (see ``vibesys.orchestration.progress``): a strategy
declares its progress-board path once (``ctx.progress.declare``) and notes
pure, unwritten Markdown blocks (``ctx.progress.note``); only
``ctx.state.commit`` and ``ctx.gates.run`` -- the host -- drain and write
them, in order. These tests exercise both write points through the public
``ctx`` API with fakes (a synthetic typed state, no projector, a real local
shell for the trusted accuracy command), never monkeypatching the host.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel
from tests.vibesys.orchestration.harness import run_with_context

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.context import RunSetup
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.orchestration.request import RunRequest
from vibesys.orchestration.runtime import RunContext
from vibesys.profilers import ProfilerKind
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import OrchestrationDescriptor

if TYPE_CHECKING:
    from pathlib import Path

_HEADING = re.compile(r"^## Round \d+ — .+$", re.MULTILINE)


class _FakeState(BaseModel):
    """Minimal typed state; only its presence as a committed slot matters here."""

    marker: int = 0


def _write_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _request(project_root: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id="progress-probe", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "progress-probe"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name="progress-probe",
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _setup() -> RunSetup:
    return RunSetup(state_namespace="progress_probe", state_slots={"state.json": _FakeState})


def test_commit_flushes_noted_entries_in_order(tmp_path: Path) -> None:
    """Entries noted before a commit land in `progress.md`, in note order."""
    project_root = tmp_path / "project"
    _write_project(project_root)
    progress_path = tmp_path / "board" / "progress.md"
    integration = LocalRunIntegration()

    async def exercise() -> str:
        async with RunContext.open(_request(project_root), integration, setup=_setup()) as ctx:
            ctx.progress.declare(progress_path)
            ctx.progress.note("## Round 1 — Alpha\n- **info**: first\n")
            ctx.progress.note("## Round 1 — Beta\n- **info**: second\n")

            await ctx.state.commit(
                sequence=1, writes={"state.json": _FakeState(marker=1)}, candidate=False
            )
            # A commit with nothing newly noted flushes nothing further.
            await ctx.state.commit(
                sequence=2, writes={"state.json": _FakeState(marker=1)}, candidate=False
            )

            ctx.progress.note("## Round 2 — Gamma\n- **info**: third\n")
            await ctx.state.commit(
                sequence=3, writes={"state.json": _FakeState(marker=2)}, candidate=False
            )
            return progress_path.read_text(encoding="utf-8")

    try:
        text = asyncio.run(exercise())
    finally:
        integration.close()

    alpha, beta, gamma = (
        text.index(f"Round {n} — {name}") for n, name in ((1, "Alpha"), (1, "Beta"), (2, "Gamma"))
    )
    assert alpha < beta < gamma
    assert len(_HEADING.findall(text)) == 3


def test_gates_run_flushes_pending_entries_before_its_own(tmp_path: Path) -> None:
    """`ctx.gates.run` drains `ctx.progress` first, then records its own outcome."""
    progress_path = tmp_path / "board" / "progress.md"

    async def body(ctx: RunContext) -> str:
        ctx.progress.declare(progress_path)
        ctx.progress.note("## Round 1 — Pending\n- **info**: queued\n")

        await ctx.gates.run(
            round_number=1,
            retry=1,
            commit=None,
            objectives=(),
            agent_backend_name=None,
        )
        # The buffer was drained by `gates.run`; nothing is left pending.
        assert ctx.progress.drain() == []
        return progress_path.read_text(encoding="utf-8")

    text = run_with_context(tmp_path, FakeAgentClient(backend_name="cli"), body)

    pending_index = text.index("Round 1 — Pending")
    accuracy_index = text.index("Round 1 — Framework accuracy gate")
    assert pending_index < accuracy_index
