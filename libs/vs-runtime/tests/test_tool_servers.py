"""A role is offered the servers of exactly the stateless tools it declares."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import RunId, Scope
from vs_mcp.api import StdioServerDescriptor
from vs_runtime.api import AgentRole, AgentTool
from vs_runtime.api.core import CombinedToolServers, StatelessToolServers

SCOPE = Scope(owner=RunId(root="run"), generation=0)
_IDS = ("profiler", "linter", "board")


def _servers() -> StatelessToolServers:
    return StatelessToolServers(
        {name: StdioServerDescriptor(name=name, command=name) for name in _IDS[:2]}
    )


@given(
    declared=st.lists(st.sampled_from(_IDS), unique=True, max_size=3),
    stateless=st.sets(st.sampled_from(_IDS)),
)
def test_only_declared_stateless_served_tools_are_offered_in_the_roles_order(
    declared: list[str], stateless: set[str]
) -> None:
    role = AgentRole(
        id="r",
        system_prompt="p",
        extra_tools=tuple(AgentTool(id=name, stateless=name in stateless) for name in declared),
    )
    offered = [item.name for item in _servers().servers(role, SCOPE)]
    assert offered == [name for name in declared if name in stateless and name in _IDS[:2]]


def test_combined_sources_offer_every_sources_servers_in_order() -> None:
    role = AgentRole(
        id="r", system_prompt="p", extra_tools=(AgentTool(id="profiler", stateless=True),)
    )
    combined = CombinedToolServers(_servers(), _servers())
    assert [item.name for item in combined.servers(role, SCOPE)] == ["profiler", "profiler"]
    assert CombinedToolServers().servers(role, SCOPE) == ()
