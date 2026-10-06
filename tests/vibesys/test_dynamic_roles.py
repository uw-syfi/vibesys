"""The roles a core dynamic run declares are exactly the roles its strategy names."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._support import dynamic_options

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


@given(max_rounds=st.integers(1, 50), max_in_flight=st.integers(1, 32))
def test_run_started_reports_the_max_rounds_the_operator_passed(
    max_rounds: int, max_in_flight: int
) -> None:
    """``--max-rounds 3 --max-in-flight 2`` starts a run that says 3, not 3 x 2."""
    options = dynamic_options(max_rounds=max_rounds, max_in_flight=max_in_flight)
    project = dynamic_core_registration().project_max_rounds

    assert project is not None
    assert project(options) == max_rounds
