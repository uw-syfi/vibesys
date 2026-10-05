"""Public MCP tool adapter for the evaluation agent service."""

from vs_evaluation.agent_core_mcp import (
    CORE_EVALUATION_TOOLS,
    build_core_evaluation_tools,
    core_evaluation_mcp_descriptor,
)
from vs_evaluation.agent_mcp import (
    build_evaluation_tools,
    evaluation_mcp_descriptor,
    evaluation_tool_names,
)
from vs_evaluation.agent_wire import EvaluationServiceClientError

__all__ = [
    "CORE_EVALUATION_TOOLS",
    "EvaluationServiceClientError",
    "build_core_evaluation_tools",
    "build_evaluation_tools",
    "core_evaluation_mcp_descriptor",
    "evaluation_mcp_descriptor",
    "evaluation_tool_names",
]
