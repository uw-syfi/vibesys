"""The roles a core dynamic run declares are exactly the roles its strategy names."""

from __future__ import annotations

from vibesys.dynamic_core import dynamic_core_registration
from vibesys.dynamic_roles import CORE_ROLES
from vibesys.orchestration.dynamic.strategy.api import Role, role_id
from vs_runtime.api import AgentCapability, AgentTool
from vs_runtime.api.core import EVALUATION_TOOL_ID


def test_every_strategy_role_is_declared_once_and_the_plugin_serves_them() -> None:
    declared = [role.id for role in CORE_ROLES]

    assert sorted(declared) == sorted(role_id(role.value).root for role in Role)
    assert tuple(dynamic_core_registration().plugin.agents) == CORE_ROLES


def test_only_the_roles_that_measure_carry_the_evaluation_tool_and_its_mcp_server() -> None:
    measuring = {role_id(Role.IMPLEMENTER.value).root, role_id(Role.JUDGE.value).root}
    for role in CORE_ROLES:
        assert str(role.system_prompt).strip()
        carries_tool = role.extra_tools == (AgentTool(id=EVALUATION_TOOL_ID),)
        assert carries_tool == (role.id in measuring)
        assert (AgentCapability.MCP_SERVERS in role.required_capabilities) == carries_tool
        assert carries_tool or role.extra_tools == ()
