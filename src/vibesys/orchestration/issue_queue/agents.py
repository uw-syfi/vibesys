"""Fixed agent declarations for issue-queue policy."""

from vibesys.orchestration.issue_queue.prompts import (
    implementer_system_prompt,
    judge_system_prompt,
    performance_system_prompt,
)
from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentTool,
    WorkspaceAccess,
)

ISSUE_BOARD = AgentTool(id="issue-board")
PROFILER = AgentTool(id="profiler")

IMPLEMENTER_SYSTEM_PROMPT = implementer_system_prompt()

JUDGE_SYSTEM_PROMPT = judge_system_prompt()

PERFORMANCE_SYSTEM_PROMPT = performance_system_prompt()

IMPLEMENTER = AgentRole(
    id="implementer",
    system_prompt=IMPLEMENTER_SYSTEM_PROMPT,
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
)

JUDGE = AgentRole(
    id="judge",
    system_prompt=JUDGE_SYSTEM_PROMPT,
    extra_tools=(ISSUE_BOARD,),
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=frozenset({AgentCapability.MCP_SERVERS, AgentCapability.SESSION_REUSE}),
)

PERF_EVALUATOR = AgentRole(
    id="perf_eval",
    system_prompt=PERFORMANCE_SYSTEM_PROMPT,
    extra_tools=(ISSUE_BOARD, PROFILER),
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=frozenset({AgentCapability.MCP_SERVERS, AgentCapability.SESSION_REUSE}),
)

AGENTS = (IMPLEMENTER, JUDGE, PERF_EVALUATOR)

__all__ = [
    "AGENTS",
    "IMPLEMENTER",
    "IMPLEMENTER_SYSTEM_PROMPT",
    "ISSUE_BOARD",
    "JUDGE",
    "JUDGE_SYSTEM_PROMPT",
    "PERFORMANCE_SYSTEM_PROMPT",
    "PERF_EVALUATOR",
    "PROFILER",
]
