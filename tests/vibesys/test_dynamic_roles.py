"""The roles a core dynamic run declares are exactly the roles its search names."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.core_roles import QUEUE_STATUS_TOOL
from vibesys.dynamic_core import dynamic_core_registration
from vibesys.dynamic_roles import CORE_ROLES
from vibesys.orchestration.dynamic.launch import DynamicLaunch
from vibesys.orchestration.dynamic.search.api import IMPLEMENTER, JUDGE, PLANNER, ROLES, dynamic
from vs_runtime.api import AgentCapability, AgentTool
from vs_runtime.api.core import EVALUATION_TOOL_ID
from vs_sim.api.testing import examples


def test_every_search_role_is_declared_once_and_the_plugin_serves_them() -> None:
    declared = [role.id for role in CORE_ROLES]

    assert sorted(declared) == sorted(dynamic.ids.role_id(role.name).root for role in ROLES)
    assert tuple(dynamic_core_registration().plugin.agents) == CORE_ROLES


def test_only_the_roles_that_measure_carry_the_evaluation_tool() -> None:
    measuring = {dynamic.ids.role_id(role.name).root for role in (IMPLEMENTER, JUDGE)}
    for role in CORE_ROLES:
        assert str(role.system_prompt).strip()
        carries_tool = AgentTool(id=EVALUATION_TOOL_ID) in role.extra_tools
        assert carries_tool == (role.id in measuring)


def test_the_orchestrator_and_implementer_read_the_queue_and_are_told_so() -> None:
    """The roles that choose and pace measurements carry the read-only queue status tool."""
    reading = {dynamic.ids.role_id(role.name).root for role in (PLANNER, IMPLEMENTER)}
    for role in CORE_ROLES:
        carries_tool = QUEUE_STATUS_TOOL in role.extra_tools
        assert carries_tool == (role.id in reading)
        assert ("read_queue_status" in str(role.system_prompt)) == carries_tool


def test_a_role_needs_mcp_exactly_when_it_carries_a_tool() -> None:
    for role in CORE_ROLES:
        needs_mcp = AgentCapability.MCP_SERVERS in role.required_capabilities
        assert needs_mcp == bool(role.extra_tools)


def test_the_roles_that_wait_for_evaluations_are_the_roles_that_carry_the_tool() -> None:
    """A role that may wait must be able to submit what it waits for, and no other can."""
    waiting = {
        dynamic.ids.role_id(role.name).root for role in ROLES if role.may_wait_for_evaluations
    }
    carrying = {
        role.id for role in CORE_ROLES if AgentTool(id=EVALUATION_TOOL_ID) in role.extra_tools
    }

    assert waiting == carrying


@settings(max_examples=examples(pr=8, nightly=100))
@given(max_rounds=st.integers(1, 50), max_in_flight=st.integers(1, 32))
def test_run_started_reports_the_max_rounds_the_operator_passed(
    max_rounds: int, max_in_flight: int
) -> None:
    """``--max-rounds 3 --max-in-flight 2`` starts a run that says 3, not 3 x 2."""
    options = DynamicLaunch.model_validate(
        {"interface": "service", "max_rounds": max_rounds, "max_in_flight": max_in_flight}
    )
    project = dynamic_core_registration().project_max_rounds

    assert project is not None
    assert project(options) == max_rounds
