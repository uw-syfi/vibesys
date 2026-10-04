"""Byte-exact golden for the brief a profiler conversation turn sends.

Regenerate with ``UPDATE_PROMPT_SNAPSHOTS=1`` and review the fixture diff as a
prompt diff.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from vibesys.orchestration.profiler_agent import RuntimeProfilerTurnProvision
from vs_evaluation.api import ProfilerAgentResult, ProfilerResultOutcome
from vs_runtime.api import AgentCapability, AgentRole
from vs_runtime.api.testing import FakeWorkspace, FakeWorkspaceAgentSessions, FakeWorkspaces

_SNAPSHOT = Path(__file__).with_name("fixtures") / "profiler_prompts" / "turn.txt"


def _first_prompt() -> str:
    role = AgentRole(id="profiler", system_prompt="Investigate performance.")
    result = ProfilerAgentResult(
        outcome=ProfilerResultOutcome.OBSERVED, narrative="Observed.", evidence_ids=("a" * 64,)
    )
    agents = FakeWorkspaceAgentSessions(
        (role,),
        responder=lambda *_: result.model_dump(),
        supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
    )
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=Path("/project"), revision="snapshot-a"),
        supports_parallel_candidates=True,
        sessions=agents,
    )

    async def scenario() -> str:
        provision = RuntimeProfilerTurnProvision(role, agents, workspaces)
        await provision.run_turn(
            session_id="conversation-1",
            operation_id="operation-1",
            request="Find the dominant launch overhead.",
            scope_id=None,
            candidate_snapshot_id="snapshot-a",
        )
        await provision.close()
        return agents.sessions[0].history[0]

    return asyncio.run(scenario())


def test_profiler_turn_prompt_matches_golden() -> None:
    text = _first_prompt()
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        _SNAPSHOT.write_text(text, encoding="utf-8")
        return
    assert text == _SNAPSHOT.read_text(encoding="utf-8")
