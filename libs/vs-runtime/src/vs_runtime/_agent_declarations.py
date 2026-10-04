"""Shared validation for agent-role requirements resolved by a runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_agent.api import AgentSessionKey, SessionScope
from vs_runtime.contracts import AgentCapability, RuntimeContractError

if TYPE_CHECKING:
    from collections.abc import Collection

    from vs_runtime.contracts import AgentRole


def validate_extra_tools(
    role: AgentRole,
    supported_tools: Collection[str],
) -> tuple[str, ...]:
    """Return registered extra tool IDs after rejecting unsupported declarations."""
    bound_tool_ids = tuple(tool.id for tool in role.extra_tools)
    unknown_tools = sorted(set(bound_tool_ids) - set(supported_tools))
    if unknown_tools:
        message = f"unsupported extra agent tools: {', '.join(unknown_tools)}"
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


def agent_session_key(
    role_id: str, member_id: str | None, generation: int | None, session_id: str
) -> AgentSessionKey:
    """Validate optional generation and derive an unambiguous durable identity."""
    if member_id is not None:
        try:
            return AgentSessionKey.for_member(role_id, member_id, generation=generation)
        except ValueError as error:
            raise RuntimeContractError(str(error)) from error
    if generation is not None:
        detail = "session generation requires member_id"
        raise RuntimeContractError(detail)
    return AgentSessionKey(SessionScope.ROLE, f"session:{session_id}")
