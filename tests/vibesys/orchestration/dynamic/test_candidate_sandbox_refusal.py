"""The dynamic loop refuses a run that cannot open candidate sandboxes, before any agent turn.

The run reports ``supports_parallel_candidates=False`` through the public run API's
fake, so this asserts the refusal policy and not a property of one real environment.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.vibesys.orchestration.dynamic._support import dynamic_options
from tests.vibesys.orchestration.dynamic.loop._harness import LEGACY_PLUGIN as PLUGIN

from vs_runtime.api import RunFacts
from vs_runtime.api.testing import FakeRun


def test_a_run_without_candidate_sandboxes_is_refused_before_any_agent_turn() -> None:
    run = FakeRun(
        PLUGIN,
        project_root=Path("refusal-workspace"),
        facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
        supports_parallel_candidates=False,
    )

    async def scenario() -> None:
        try:
            with pytest.raises(RuntimeError, match="isolated candidate workspaces"):
                await PLUGIN.orchestrate(run, dynamic_options())
            assert run.agents.sessions == ()
            assert run.workspaces.candidates == ()
        finally:
            await run.close()

    asyncio.run(scenario())
