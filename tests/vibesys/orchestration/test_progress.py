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

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.context import RunSetup
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.orchestration.request import RunRequest
from vibesys.orchestration.runtime import RunContext
from vibesys.profilers import ProfilerKind
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import OrchestrationPlugin, RunHost, RunStatus

if TYPE_CHECKING:
    from pathlib import Path

_HEADING = re.compile(r"^## Round \d+ — .+$", re.MULTILINE)


class _FakeState(BaseModel):
    """Minimal typed state; only its presence as a committed slot matters here."""

    marker: int = 0


class _Options(BaseModel):
    pass


async def _orchestrate(run: RunHost, options: BaseModel) -> RunStatus:
    del run, options
    return RunStatus.SUCCEEDED


_PLUGIN = OrchestrationPlugin(
    id="progress-probe",
    agents=(),
    options=_Options,
    orchestrate=_orchestrate,
    state=_FakeState,
)


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


def test_commit_flushes_noted_entries_in_order(tmp_path: Path) -> None:
    """Entries noted before a commit land in `progress.md`, in note order."""
    project_root = tmp_path / "project"
    _write_project(project_root)
    progress_path = tmp_path / "board" / "progress.md"
    integration = LocalRunIntegration()

    async def exercise() -> str:
        async with RunContext.open(
            _request(project_root), integration, setup=RunSetup(), plugin=_PLUGIN
        ) as ctx:
            ctx.progress.declare(progress_path)
            ctx.progress.note("## Round 1 — Alpha\n- **info**: first\n")
            ctx.progress.note("## Round 1 — Beta\n- **info**: second\n")

            await ctx.state.commit(_FakeState(marker=1))
            # A commit with nothing newly noted flushes nothing further.
            await ctx.state.commit(_FakeState(marker=1))

            ctx.progress.note("## Round 2 — Gamma\n- **info**: third\n")
            await ctx.state.commit(_FakeState(marker=2))
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
