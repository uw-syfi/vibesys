"""The dynamic loop offers the evaluation tool to the roles that measure, and only to them (#1649).

The evaluation tool is the one way a VibeSys agent requests a trusted evaluation. This asks the
run's tool offer, which builds each role's MCP configuration, what a session of each dynamic
role is given.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from tests.support.skeleton_strategy import ATTEMPT, DIGEST
from tests.vibesys.orchestration.dynamic.core_policy._inputs import write_project

from vibesys.dynamic_roles import CORE_ROLES, IMPLEMENTER, JUDGE
from vs_core.api import ArtifactId, ArtifactRef, Scope
from vs_evaluation.api.tools import SUBMIT_TOOL, VALIDATE_WAIT_TOOL, build_core_evaluation_tools
from vs_project.api import Project
from vs_runtime.api.core import (
    AgentEvaluationBridge,
    AgentEvaluationPolicy,
    ReceiptStore,
    ScopeWorkspaces,
    StoreWorkspaceReceipts,
)
from vs_runtime.api.testing import FakeWorkspace, FakeWorkspaces

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api import AgentRole
    from vs_runtime.api.infrastructure import RuntimeWorkspaces


def _bridge(tmp_path: Path) -> AgentEvaluationBridge:
    root = tmp_path / "project"
    write_project(root)
    namespace = Project.open(root).state.state_store_namespace("offer")
    policy = AgentEvaluationPolicy(
        evaluator_digest=DIGEST,
        workload_digest=DIGEST,
        environment_digest=DIGEST,
        recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest=DIGEST),
        stages=(("accuracy", 10.0), ("benchmark", 10.0)),
        queue_allowance=880.0,
    )
    return AgentEvaluationBridge(
        tmp_path / "evaluation.sock",
        policy,
        ScopeWorkspaces(
            cast("RuntimeWorkspaces", FakeWorkspaces(FakeWorkspace())),
            StoreWorkspaceReceipts(ReceiptStore(namespace)),
        ),
    )


@pytest.mark.parametrize("role", CORE_ROLES, ids=lambda role: role.id)
def test_a_dynamic_role_is_offered_the_evaluation_tool_exactly_when_it_measures(
    tmp_path: Path, role: AgentRole
) -> None:
    bridge = _bridge(tmp_path)

    servers = bridge.servers(role, Scope(owner=ATTEMPT.attempt_id, generation=0))

    measures = role in (IMPLEMENTER, JUDGE)
    assert bool(servers) is measures
    if measures:
        (descriptor,) = servers
        env = dict(descriptor.runtime_env)
        tools = build_core_evaluation_tools(
            socket_path=tmp_path / "evaluation.sock", token=env["VS_EVALUATION_TOKEN"]
        )
        assert {tool.name for tool in tools} == {SUBMIT_TOOL, VALIDATE_WAIT_TOOL}
