"""Minimal ``RunContext`` harness for unit-testing ``ctx.agents.turn`` directly,
without a registered strategy or orchestration policy package.

Opens a bare ``RunContext`` with an empty ``RunSetup`` and hands it to a
caller-supplied async body, instead of driving a registered plugin.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch  # test-isolation: module seams patched below

from vibesys.api.testing import FakeComputeBackend
from vibesys.config import Config, as_config
from vibesys.context import RunSetup
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.orchestration.request import RunRequest
from vibesys.orchestration.runtime import RunContext
from vibesys.profilers import ProfilerKind
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from vs_agent.api.testing import FakeAgentClient


class _SharedFakeClient:
    """Keep one scripted client usable across all spawned role handles."""

    def __init__(self, scripted: FakeAgentClient) -> None:
        self._scripted = scripted

    def __getattr__(self, name: str) -> object:
        return getattr(self._scripted, name)

    def close(self) -> None:
        """Let the owning test close the shared script after the run."""


def _write_minimal_input_bundle(root: Path) -> Path:
    """Create a small deterministic project for host-capability tests."""
    model_dir = root / "input_model"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "ref.py").write_text("def predict(x):\n    return x * 2\n", encoding="utf-8")
    (model_dir / "OBJECTIVE.md").write_text("Maximize tok/s throughput.\n", encoding="utf-8")
    (model_dir / "vibesys.input.toml").write_text(
        'version = 1\n\n[agent]\ndomain = "llm-serving"\n\n'
        '[accuracy]\ncommand = ["python", "-c", "print(\'ok\')"]\n\n'
        '[benchmark]\ncommand = ["python", "-c", "print(\'ok\')"]\n',
        encoding="utf-8",
    )
    return model_dir


def run_with_context[R](
    tmp_path: Path,
    runner: FakeAgentClient,
    body: Callable[[RunContext], Awaitable[R]],
    *,
    setup: RunSetup | None = None,
) -> R:
    """Open one bare ``RunContext`` against ``tmp_path`` and run ``body(ctx)``.

    No orchestrator, no registered strategy ID, no state slots: just the
    host capabilities (``ctx.agents``, ``ctx.workspaces``, ...) a unit test
    needs to call ``ctx.agents.turn`` directly. *setup* defaults to an empty
    ``RunSetup()``; pass one with ``memory_paths`` set to exercise declared
    agent-memory interactions (e.g. role-isolation reverts inside a memory
    path).
    """
    input_dir = _write_minimal_input_bundle(tmp_path)
    bundle = load_input_bundle(input_dir)
    config = as_config(Config.model_validate({"model": {"name": "agents-turn-test"}}))
    descriptor = OrchestrationDescriptor(id="agents-turn-test", config_version=1, options={})
    request = RunRequest(
        project_root=bundle.root,
        orchestration=descriptor,
        config=config,
        input_bundle=bundle,
        objective=bundle.objective,
        exp_name="agents-turn-test",
        runs_dir=tmp_path / "exp_env",
        profiler_kind=ProfilerKind.NONE,
    )

    resolved_setup = setup if setup is not None else RunSetup()

    async def execute() -> R:
        integration = LocalRunIntegration()
        try:
            async with RunContext.open(
                request,
                integration,
                setup=resolved_setup,
                agent_client_factory=lambda **_kwargs: _SharedFakeClient(runner),
                backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(),
            ) as ctx:
                return await body(ctx)
        finally:
            integration.close()

    # test-isolation: PROJECT_ROOT has no construction seam; this host test needs to stage resources from tmp_path.
    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        return asyncio.run(execute())
