"""Public MCP tool adapter for the evaluation agent service."""

from vs_evaluation.agent_mcp import (
    EvaluationServiceClientError,
    build_evaluation_tools,
    evaluation_mcp_descriptor,
    evaluation_tool_names,
)

__all__ = [
    "EvaluationServiceClientError",
    "build_evaluation_tools",
    "evaluation_mcp_descriptor",
    "evaluation_tool_names",
]
