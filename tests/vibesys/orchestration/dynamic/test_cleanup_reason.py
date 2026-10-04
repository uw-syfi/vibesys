"""Cleanup failures retain a nonempty public observation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
)
from tests.vibesys.orchestration.dynamic.test_scheduler import _FlakyWorkspaces

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, ORCHESTRATOR
from vs_runtime.api import Run
from vs_runtime.api.testing import FakeWorkspace

if TYPE_CHECKING:
    from pathlib import Path


class _CleanupFaultWorkspace(FakeWorkspace):
    async def discard(self) -> None:
        message = "   "
        raise RuntimeError(message)


class _CleanupFaultWorkspaces(_FlakyWorkspaces):
    async def create_candidate(
        self, from_revision: str | None = None, *, member_id: str | None = None
    ) -> _CleanupFaultWorkspace:
        candidate = await super().create_candidate(from_revision, member_id=member_id)
        return _CleanupFaultWorkspace(
            workspace_id=candidate.id,
            path=candidate.path,
            revision=candidate.revision,
            known_revisions={from_revision} if from_revision is not None else set(),
        )


def test_blank_workspace_cleanup_failure_has_a_reason(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = baseline_run(
            tmp_path,
            Script(
                {
                    ORCHESTRATOR.id: [portfolio("held")],
                    IMPLEMENTER.id: [{**implementation("held"), "outcome": "blocked"}],
                }
            ),
        )
        run = Run(
            run_id=fake.run_id,
            facts=fake.facts,
            agents=fake.agents,
            workspaces=_CleanupFaultWorkspaces(fake.workspaces, failures=0),
            evaluation=fake.evaluation,
            state=fake.state,
            control=fake.control,
            commands=fake.commands,
            skills=fake.skills,
            observations=fake.observations,
        )
        await PLUGIN.orchestrate(
            run, dynamic_options(max_rounds=1, max_in_flight=1, max_retries_per_round=1)
        )
        assert "dynamic workstream held workspace cleanup failed: RuntimeError" in [
            call.message for call in fake.observations.calls
        ]

    asyncio.run(scenario())
