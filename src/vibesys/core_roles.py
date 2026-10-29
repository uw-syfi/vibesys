"""Agent roles of a strategy on the core lifecycle.

A role is host wiring: its capabilities and workspace access name `vs_runtime` values and its
system prompt is read from a template file, neither of which a pure strategy may do. Every
strategy's roles share the capability sets below and the one factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_runtime.api import AgentCapability, AgentRole, AgentTool, WorkspaceAccess
from vs_runtime.api.core import EVALUATION_TOOL_ID

if TYPE_CHECKING:
    from vs_prompts.api import TemplateRenderer

__all__ = [
    "CONTINUATION",
    "CONTINUING_READER",
    "EVALUATION_TOOL",
    "MEASURING",
    "MEASURING_READER",
    "MEASURING_WRITER",
    "QUEUE_STATUS_TOOL",
    "READER",
    "WRITER",
    "RoleKind",
    "core_role",
    "reading_queue_status",
]

# A turn that ends in a reply the host may re-dispatch needs the provider to resume it.
CONTINUATION = frozenset(
    {
        AgentCapability.SESSION_REUSE,
        AgentCapability.PROVIDER_SESSION_RESUME,
        AgentCapability.DURABLE_TURN_CONTINUATION,
    }
)
# A role that may also submit a measurement of its own workspace through the bridged evaluation
# tool; core decides admission and budget, so the tool has no status or wait. Reaching the tool
# server needs MCP.
MEASURING = CONTINUATION | {AgentCapability.MCP_SERVERS}
EVALUATION_TOOL = AgentTool(id=EVALUATION_TOOL_ID)


@dataclass(frozen=True)
class RoleKind:
    """What a role may do: the workspace access, capabilities and tools it is declared with."""

    access: WorkspaceAccess
    capabilities: frozenset[AgentCapability]
    tools: tuple[AgentTool, ...] = ()


# A role that only reads and answers once.
READER = RoleKind(WorkspaceAccess.READ_ONLY, frozenset())
# A reader whose turn may end in a reply the host re-dispatches.
CONTINUING_READER = RoleKind(WorkspaceAccess.READ_ONLY, CONTINUATION)
# A reader that may also submit a measurement of the workspace it reads.
MEASURING_READER = RoleKind(WorkspaceAccess.READ_ONLY, MEASURING, (EVALUATION_TOOL,))
# A writer that answers once and commits what it wrote, measuring nothing itself.
WRITER = RoleKind(WorkspaceAccess.READ_WRITE, frozenset())
# A writer that may also submit a measurement of the workspace it writes.
MEASURING_WRITER = RoleKind(WorkspaceAccess.READ_WRITE, MEASURING, (EVALUATION_TOOL,))

# Reads the run's evaluation and agent GPU job queue. Its server holds no run state, so core
# admits it beside its own writes; reaching the server needs MCP.
QUEUE_STATUS_TOOL = AgentTool(id="queue_status", stateless=True)


def reading_queue_status(kind: RoleKind) -> RoleKind:
    """``kind``, also given the read-only queue status tool and the MCP capability it needs."""
    return RoleKind(
        kind.access,
        kind.capabilities | {AgentCapability.MCP_SERVERS},
        (*kind.tools, QUEUE_STATUS_TOOL),
    )


def core_role(
    renderer: TemplateRenderer, *, role_id: str, system_template: str, kind: RoleKind
) -> AgentRole:
    """One agent role: its identity, rendered system prompt, tools, access and capabilities."""
    return AgentRole(
        id=role_id,
        system_prompt=renderer.render_template(system_template),
        extra_tools=kind.tools,
        workspace_access=kind.access,
        required_capabilities=kind.capabilities,
    )
