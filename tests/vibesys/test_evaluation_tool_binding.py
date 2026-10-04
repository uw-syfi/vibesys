"""Product composition offers suspension tools only to declared continuation roles."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys.composition import AGENT_TOOL_BINDINGS, AgentToolContext
from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, PROFILER
from vibesys.orchestration.single.agents import IMPLEMENTER as SINGLE_IMPLEMENTER
from vibesys.run.evaluation_backend import SemanticEvaluationBackend, SemanticEvaluationIdentity
from vs_evaluation.api import ContentDigest, EvaluationAgentRole, EvaluationAgentService
from vs_evaluation.api.testing import InMemoryEvaluationNamespace
from vs_evaluation.api.tools import build_evaluation_tools
from vs_runtime.api import AgentToolBindingContext
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api import AgentRole


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "suspended"),
    [(IMPLEMENTER, True), (JUDGE, True), (PROFILER, False), (SINGLE_IMPLEMENTER, False)],
)
async def test_product_binding_propagates_suspension_to_tool_schema(
    tmp_path: Path,
    role: AgentRole,
    *,
    suspended: bool,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    workspace = await run.workspaces.create_candidate(member_id="work")
    namespace = InMemoryEvaluationNamespace()
    digest = ContentDigest.sha256(b"identity")
    backend = SemanticEvaluationBackend(
        run.evaluation,
        run.workspaces,
        namespace,
        SemanticEvaluationIdentity(evaluator=digest, workload=digest, environment=digest),
    )
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
    context = AgentToolContext(profiler_id="none")
    context.install_evaluation(service, backend)
    (descriptor,) = AGENT_TOOL_BINDINGS["evaluation"](
        context,
        AgentToolBindingContext(role, workspace, "work", str),
    )
    env = dict(descriptor.env)
    assert env["VS_EVALUATION_SUSPENSION"] == ("1" if suspended else "0")
    names = {
        tool.name
        for tool in build_evaluation_tools(
            socket_path=service.socket_path,
            token=env["VS_EVALUATION_TOKEN"],
            role=EvaluationAgentRole(env["VS_EVALUATION_ROLE"]),
            evaluation_suspension=env["VS_EVALUATION_SUSPENSION"] == "1",
        )
    }
    if role is IMPLEMENTER:
        assert "submit_evaluation" in names
        assert "await_evaluation" not in names
    if role in (PROFILER, SINGLE_IMPLEMENTER):
        assert "await_evaluation" in names
    await backend.close()
