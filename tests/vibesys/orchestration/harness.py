"""Minimal ``RunContext`` harness for unit-testing ``ctx.agents.turn`` directly,
without a registered strategy or the ``loops/`` orchestration layer.

Reuses the same two hermetic seams as ``tests/vibesys/golden/harness.py``
(the CUDA sandbox factory and ``build_agent_client``) but opens a bare
``RunContext`` with an empty ``RunSetup`` and hands it to a caller-supplied
async body, instead of driving a registered ``Orchestrator``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch  # test-isolation: module seams patched below

from tests.vibesys.golden.harness import _SharedFakeClient, write_minimal_input_bundle

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
    from typing import TypeVar

    from vs_agent.api.testing import FakeAgentClient

    _R = TypeVar("_R")


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
    input_dir = write_minimal_input_bundle(tmp_path)
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
            async with RunContext.open(request, integration, setup=resolved_setup) as ctx:
                return await body(ctx)
        finally:
            integration.close()

    with (
        # test-isolation: the harness patches module constants and the local sandbox factory, which have no injection seam
        patch("vibesys.backends.cuda.make_local_shell_sandbox"),
        # test-isolation: the harness patches module constants and the local sandbox factory, which have no injection seam
        patch(
            "vibesys.orchestration.runtime.build_agent_client",
            side_effect=lambda **_kwargs: _SharedFakeClient(runner),
        ),
        # test-isolation: the harness patches module constants and the local sandbox factory, which have no injection seam
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
    ):
        return asyncio.run(execute())
