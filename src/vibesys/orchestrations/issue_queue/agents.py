"""Fixed agent declarations for issue-queue policy."""

from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentTool,
    WorkspaceAccess,
)

SHELL = AgentTool(id="shell")
ISSUE_BOARD = AgentTool(id="issue-board")

IMPLEMENTER = AgentRole(
    id="implementer",
    system_prompt=(
        "You implement exactly one issue in the current workspace. Keep changes "
        "within its acceptance criteria, run a focused self-check, and return only "
        "the requested structured response."
    ),
    tools=(SHELL,),
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
)

JUDGE = AgentRole(
    id="judge",
    system_prompt=(
        "You independently review one issue for functional correctness. Maintain "
        "relevant tests, run them, and return only the requested structured verdict. "
        "File at most one unrelated bug through the issue-board tool."
    ),
    tools=(SHELL, ISSUE_BOARD),
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=frozenset(
        {AgentCapability.MCP_SERVERS, AgentCapability.SESSION_REUSE}
    ),
)

PERF_EVALUATOR = AgentRole(
    id="perf_eval",
    system_prompt=(
        "You benchmark the current implementation, compare it with prior durable "
        "measurements, and file the highest-value follow-up work through the "
        "issue-board tool. Return only the requested structured response."
    ),
    tools=(SHELL, ISSUE_BOARD),
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=frozenset(
        {AgentCapability.MCP_SERVERS, AgentCapability.SESSION_REUSE}
    ),
)

AGENTS = (IMPLEMENTER, JUDGE, PERF_EVALUATOR)

__all__ = ["AGENTS", "IMPLEMENTER", "ISSUE_BOARD", "JUDGE", "PERF_EVALUATOR"]
