"""Built-in tool bindings selected by the VibeSys launch catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vs_agent.api import StdioServerDescriptor, ToolServerDescriptor, expose_as_tools
from vs_evaluation.api import EvaluationAgentRole
from vs_evaluation.api.tools import evaluation_mcp_descriptor

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.api.wiring import AgentToolContext
    from vs_runtime.api import AgentToolBindingContext


def _profiler_tool(
    context: object, _binding: AgentToolBindingContext
) -> tuple[ToolServerDescriptor, ...]:
    """Bind the selected profiler's analysis server to one agent session."""
    resolved = cast("AgentToolContext", context)
    if resolved.profiler_id == "none":
        return ()
    support_name = f"{resolved.profiler_id}_profiler"
    return (
        StdioServerDescriptor(
            name=f"vibesys-{resolved.profiler_id.replace('_', '-')}-profiler",
            command="python",
            args=(f"{support_name}/server.py",),
            env=resolved.profiler_env,
        ),
    )


def _issue_board_tool(
    _host: object, _binding: AgentToolBindingContext
) -> tuple[ToolServerDescriptor, ...]:
    """Bind the fixed issue-board server to workspace-relative policy artifacts."""
    return (
        expose_as_tools(
            name="vibesys-issue-board",
            entrypoint_module="vibesys.orchestration.issue_queue.tool_server",
            entrypoint_args=(
                "issues.json",
                ".vibesys/issue-tool-policy.json",
                ".vibesys/issue-tracker.json",
            ),
        ),
    )


def _evaluation_tool(
    context: object, binding: AgentToolBindingContext
) -> tuple[ToolServerDescriptor, ...]:
    """Issue one role and logical-member scoped evaluation capability."""
    resolved = cast("AgentToolContext", context)
    service = resolved.evaluation_service
    backend = resolved.evaluation_backend
    if service is None or backend is None:
        message = "evaluation agent service is not installed"
        raise RuntimeError(message)
    role = _EVALUATION_ROLES.get(binding.role.id)
    if role is None:
        message = f"agent role {binding.role.id!r} has no evaluation capability profile"
        raise RuntimeError(message)
    backend.bind(binding)
    scope_id = binding.workspace.id
    principal_member = binding.member_id or scope_id or "root"
    grant = service.grant(
        principal_id=f"{role.value}:{principal_member}",
        role=role,
        scope_id=scope_id,
        run_observer=role is EvaluationAgentRole.RUN_OBSERVER,
    )
    return (evaluation_mcp_descriptor(grant, binding.agent_path(service.socket_path)),)


AGENT_TOOL_BINDINGS: Mapping[
    str, Callable[[object, AgentToolBindingContext], tuple[ToolServerDescriptor, ...]]
] = {
    "evaluation": _evaluation_tool,
    "issue-board": _issue_board_tool,
    "profiler": _profiler_tool,
}
"""Built-in agent tools bound by product composition, not orchestration policy."""


_EVALUATION_ROLES: Mapping[str, EvaluationAgentRole] = {
    "dynamic-implementer": EvaluationAgentRole.IMPLEMENTER,
    "dynamic-judge": EvaluationAgentRole.JUDGE,
    "dynamic-orchestrator": EvaluationAgentRole.RUN_OBSERVER,
    "dynamic-profiler": EvaluationAgentRole.PROFILER,
    "implementer": EvaluationAgentRole.IMPLEMENTER,
    "judge": EvaluationAgentRole.JUDGE,
    "orchestrator": EvaluationAgentRole.ORCHESTRATOR,
    "portfolio_dispatch": EvaluationAgentRole.PORTFOLIO_DISPATCH,
    "profiler": EvaluationAgentRole.PROFILER,
}
