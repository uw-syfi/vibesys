"""Byte-exact agent-facing text owned by the multi plugin's prompt templates.

Complements the normalized trajectory goldens: these fixtures keep trailing
newlines and spacing, so moving wording between Python and templates cannot
change what an agent reads. Regenerate with ``UPDATE_PROMPT_SNAPSHOTS=1``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.golden.helpers import assert_exact_text

from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.multi import PLUGIN
from vibesys.orchestration.multi.contracts import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
)
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.review import Verdict
from vs_runtime.api import AgentCapability, RunFacts, RunStatus
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole

_FIXTURES = Path(__file__).parent / "fixtures" / "exact_prompts"
_CAPABILITIES = frozenset(
    {
        AgentCapability.PROVIDER_SESSION_RESUME,
        AgentCapability.SESSION_REUSE,
        AgentCapability.MCP_SERVERS,
    }
)


def _plan(hypothesis_id: str, **changes: object) -> OrchestratorPlan:
    return OrchestratorPlan.model_validate(
        {
            "hypothesis_id": hypothesis_id,
            "hypothesis": "Batching removes per-request overhead.",
            "title": "Batch prefill",
            "task": "Batch prefill requests.",
            "pass_criteria": "Throughput improves without an accuracy regression.",
            "reasoning": "The trace shows repeated launch overhead.",
            **changes,
        }
    )


class _Script:
    def __init__(self, *replies: object) -> None:
        self.replies = deque(replies)
        self.calls: list[tuple[str, str]] = []

    def respond(
        self,
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        self.calls.append((role.id, message))
        return self.replies.popleft()


def _orchestrate(path: Path, script: _Script, facts: RunFacts | None = None) -> None:
    async def scenario() -> None:
        run = FakeRun(
            PLUGIN,
            project_root=path,
            facts=facts,
            responder=script.respond,
            supported_agent_capabilities=_CAPABILITIES,
            supported_extra_tools=("profiler",),
        )
        try:
            options = PLUGIN.options.model_validate(
                {
                    "interface": "service",
                    "max_rounds": 1,
                    "max_retries_per_round": 2,
                    "judge_every": 1,
                    "official_eval_every": 10,
                }
            )
            assert await PLUGIN.orchestrate(run, options) is RunStatus.SUCCEEDED
        finally:
            await run.close()

    asyncio.run(scenario())


def _tail() -> tuple[object, ...]:
    return (
        ImplementerResponse(
            summary="Implemented batching.",
            expected_behavior="Fewer launches.",
            hypothesis_outcome="nominated",
            evidence="The local smoke check passed.",
        ),
        JudgeResponse(analysis="Sound.", feedback="", verdict=Verdict.PASS),
    )


@pytest.mark.parametrize("agent", PLUGIN.agents, ids=lambda agent: agent.id)
def test_system_prompt_text_is_stable(agent: AgentRole) -> None:
    assert_exact_text(_FIXTURES / f"system-{agent.id}.txt", agent.system_prompt)


def test_plan_correction_message_text_is_stable(tmp_path: Path) -> None:
    updates = [
        {
            "hypothesis_id": "H-01",
            "disposition": "abandoned",
            "reason": "Self-reference is invalid.",
        },
        {
            "hypothesis_id": "H-00",
            "disposition": "parked",
            "reason": "Superseded.",
        },
    ]
    rejected = _plan("H-01", hypothesis_updates=updates)
    script = _Script(
        PreRoundDecision(need_profile=False, profile_focus="", reasoning="Enough evidence."),
        rejected,
        _plan("H-02"),
    )
    script.replies.extend(_tail())

    _orchestrate(tmp_path, script)

    correction = [message for role, message in script.calls if role == "orchestrator"][2]
    assert_exact_text(_FIXTURES / "plan-correction.txt", correction)


@pytest.mark.parametrize(
    ("domain", "profiler"), [("generic", "linux_cpu"), ("llm-serving", "torch")]
)
def test_profiler_prompt_text_with_campaign_context_is_stable(
    tmp_path: Path, domain: str, profiler: str
) -> None:
    script = _Script(
        PreRoundDecision(need_profile=True, profile_focus="CPU dispatch", reasoning="Profile."),
        ProfilerSummary(analysis="A.", bottlenecks="B.", suggestions="S."),
        _plan("H-01"),
    )
    script.replies.extend(_tail())
    facts = RunFacts(domain_id=domain, objective="Improve the candidate.", profiler_id=profiler)

    _orchestrate(tmp_path, script, facts)

    prompt = next(message for role, message in script.calls if role == "profiler")
    assert_exact_text(
        _FIXTURES / f"profiler-{profiler}.txt", prompt.replace(str(tmp_path), "<ROOT>")
    )
