"""The evaluation tool server starts in the editor container and offers its tools.

Regression for #1595: the server's packages live under ``libs/*/src`` of the
read-only framework mount, so ``python -m vs_evaluation.agent_core_mcp`` died with
``ModuleNotFoundError`` unless the descriptor carried the import roots. The
descriptor here comes from the run's own bridge, not from a copy of its command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.minimal_container.stdio import StdioJsonProcess, mcp_initialize, mcp_tool_names
from tests.support.skeleton_strategy import ATTEMPT, DIGEST

from vs_core.api import ArtifactId, ArtifactRef, Scope
from vs_evaluation.api import EvaluationAgentRole, EvaluationGrant
from vs_evaluation.api.tools import CORE_EVALUATION_TOOLS, evaluation_mcp_descriptor
from vs_mcp.api import StdioServerDescriptor
from vs_runtime.api import AgentRole, AgentTool
from vs_runtime.api.core import EVALUATION_TOOL_ID, AgentEvaluationBridge, AgentEvaluationPolicy

if TYPE_CHECKING:
    from pathlib import Path

    from tests.minimal_container.editor import Editor

pytestmark = pytest.mark.minimal_container

#: Also run on the stand-in bases: an Ubuntu base has no `python` of its own.
INCLUDE_STAND_INS = True


_POLICY = AgentEvaluationPolicy(
    evaluator_digest=DIGEST,
    workload_digest=DIGEST,
    environment_digest=DIGEST,
    recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest=DIGEST),
    stages=(("accuracy", 10.0), ("benchmark", 10.0)),
    queue_allowance=880.0,
    accuracy_stage="accuracy",
)


class _NoWorkspaces:
    """No workspace is looked up: this tier only asks the bridge which server to start."""

    async def workspace_of(self, scope: Scope) -> None:
        del scope


def test_the_core_evaluation_server_initializes_and_lists_its_tools(
    editor: Editor, tmp_path: Path
) -> None:
    bridge = AgentEvaluationBridge(tmp_path / "evaluation.sock", _POLICY, _NoWorkspaces())
    role = AgentRole(
        id="implementer", system_prompt="implement", extra_tools=(AgentTool(id=EVALUATION_TOOL_ID),)
    )
    (descriptor,) = bridge.servers(role, Scope(owner=ATTEMPT.attempt_id, generation=0))
    assert isinstance(descriptor, StdioServerDescriptor)

    with StdioJsonProcess(editor.server_argv(descriptor)) as server:
        mcp_initialize(server)
        tools = mcp_tool_names(server)

    assert sorted(tools) == sorted(CORE_EVALUATION_TOOLS)


def test_the_per_role_evaluation_server_initializes_and_lists_tools(editor: Editor) -> None:
    grant = EvaluationGrant(
        token="t" * 32,
        principal_id="implementer",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id="work",
    )
    descriptor = evaluation_mcp_descriptor(grant, "/tmp/evaluation.sock")  # noqa: S108  # lint-waiver: LW-960022 [S108]; a socket path this test never connects to.
    assert isinstance(descriptor, StdioServerDescriptor)

    with StdioJsonProcess(editor.server_argv(descriptor)) as server:
        mcp_initialize(server)
        tools = mcp_tool_names(server)

    assert "submit_evaluation" in tools
