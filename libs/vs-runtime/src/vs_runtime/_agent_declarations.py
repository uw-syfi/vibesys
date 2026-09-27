"""Shared validation for agent-role requirements resolved by a runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime.contracts import AgentCapability, RuntimeContractError

if TYPE_CHECKING:
    from collections.abc import Collection

    from vs_runtime.contracts import AgentRole

_BUILT_IN_TOOL_IDS = frozenset({"shell"})


def validate_agent_tools(
    role: AgentRole,
    supported_tools: Collection[str],
) -> tuple[str, ...]:
    """Return non-built-in tool IDs after rejecting unsupported declarations."""
    bound_tool_ids = tuple(tool.id for tool in role.tools if tool.id not in _BUILT_IN_TOOL_IDS)
    unknown_tools = sorted(set(bound_tool_ids) - set(supported_tools))
    if unknown_tools:
        message = f"unsupported agent tools: {', '.join(unknown_tools)}"
        raise RuntimeContractError(message)
    return bound_tool_ids


def validate_agent_capabilities(
    role: AgentRole,
    supported_capabilities: Collection[AgentCapability],
    *,
    member_id: str | None,
    has_bound_tools: bool,
) -> None:
    """Reject role and session requirements that the selected driver cannot meet."""
    required = set(role.required_capabilities)
    if member_id is not None:
        required.add(AgentCapability.PROVIDER_SESSION_RESUME)
    if has_bound_tools:
        required.add(AgentCapability.MCP_SERVERS)
    missing = sorted(capability.value for capability in required - set(supported_capabilities))
    if missing:
        message = f"agent driver lacks required capabilities: {', '.join(missing)}"
        raise RuntimeContractError(message)
