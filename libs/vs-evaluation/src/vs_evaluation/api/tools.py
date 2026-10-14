"""Public MCP tool adapter for the evaluation agent service."""

from vs_evaluation.agent_core_mcp import (
    CORE_EVALUATION_TOOLS,
    SUBMIT_TOOL,
    VALIDATE_WAIT_TOOL,
    build_core_evaluation_tools,
    core_evaluation_mcp_descriptor,
)
from vs_evaluation.agent_wire import EvaluationServiceClientError, SocketClient

__all__ = [
    "CORE_EVALUATION_TOOLS",
    "SUBMIT_TOOL",
    "VALIDATE_WAIT_TOOL",
    "EvaluationServiceClientError",
    "SocketClient",
    "build_core_evaluation_tools",
    "core_evaluation_mcp_descriptor",
]
