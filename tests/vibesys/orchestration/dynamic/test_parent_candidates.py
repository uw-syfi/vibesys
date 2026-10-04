"""A new workstream builds on content that passed accuracy only if that content is reproducible.

An agent-submitted evaluation records the digest of the content it checked.
The framework offers that revision as a parent only while the revision still
exports to the same digest; a plan naming one that does not is corrected with
the field named, never given the base revision instead.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._support import Script, dynamic_options, portfolio

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, ORCHESTRATOR
from vs_runtime.api import (
    AgentCapability,
    AgentEvaluation,
    AgentEvaluationStage,
    AgentEvaluationStageOutcome,
    AgentEvaluationStatus,
    RunFacts,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


def _blocked(identifier: str) -> dict[str, object]:
    return {"summary": f"Stopped {identifier}.", "outcome": "blocked", "evidence": []}


def _child(parent: str) -> dict[str, object]:
    plan = portfolio("b")
    workstreams = plan["workstreams"]
    assert isinstance(workstreams, list)
    (child,) = workstreams
    return {**plan, "workstreams": [{**child, "parent_hypothesis_id": parent}]}


def _accuracy_passed(revision: str, digest: str) -> AgentEvaluation:
    return AgentEvaluation(
        revision=revision,
        content_digest=digest,
        kinds=("accuracy", "benchmark"),
        status=AgentEvaluationStatus.FAILED,
        failure="benchmark too slow",
        stages=(
            AgentEvaluationStage(kind="accuracy", outcome=AgentEvaluationStageOutcome.PASSED),
            AgentEvaluationStage(kind="benchmark", outcome=AgentEvaluationStageOutcome.FAILED),
        ),
    )


def _run(tmp_path: Path, script: Script, *, digest_matches: bool) -> tuple[FakeRun, str]:
    """Run ``a`` (its turn submits an accuracy pass of its start revision), then the plans."""
    holder: list[FakeRun] = []
    evaluated: list[str] = []

    def respond(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        if role.id == IMPLEMENTER.id and not evaluated:
            run = holder[0]
            workspace = run.workspaces.candidates[-1]
            revision = workspace.revision
            assert revision is not None
            evaluated.append(revision)
            content = f"patch for {revision}" if digest_matches else "other content"
            digest = hashlib.sha256(content.encode()).hexdigest()
            run.evaluation.record_agent_evaluation(workspace, _accuracy_passed(revision, digest))
        return script.respond(role, history, message, response)

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", accuracy_configured=True),
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        holder.append(run)
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_rounds=2))
        return run

    run = asyncio.run(scenario())
    return run, evaluated[0]


def _messages(script: Script, role_id: str) -> list[str]:
    return [message for role, _, message in script.calls if role == role_id]


@pytest.mark.parametrize("content", ["reproduces", "changed"])
def test_an_agent_verified_candidate_is_a_parent_only_while_its_content_reproduces(
    tmp_path: Path, content: str
) -> None:
    digest_matches = content == "reproduces"
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("a"), _child("a"), portfolio("b")],
            IMPLEMENTER.id: [_blocked("a"), _blocked("b")],
        }
    )

    run, evaluated = _run(tmp_path, script, digest_matches=digest_matches)

    planner = _messages(script, ORCHESTRATOR.id)
    implementer = _messages(script, IMPLEMENTER.id)
    if digest_matches:
        assert len(planner) == 2
        assert f'"hypothesis_id":"a","title":"Investigate a","revision":"{evaluated}"' in planner[1]
        assert f"Parent revision: `{evaluated}`" in implementer[1]
    else:
        assert len(planner) == 3
        assert "Buildable candidates" not in planner[1]
        assert (
            "workstreams[0].parent_hypothesis_id: 'a' cannot be built on (its revision no "
            "longer holds the content that passed accuracy)"
        ) in planner[2]
        assert f"Parent revision: `{evaluated}`" not in implementer[1]
        assert any(
            "buildable candidate a withheld" in call.message for call in run.observations.calls
        )
