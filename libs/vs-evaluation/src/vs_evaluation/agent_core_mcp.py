"""The agent's in-turn evaluation tools on the core path: submit, then validate the wait.

A core run decides admission, budget and identity itself, so this server is narrow: it
offers exactly two tools and no status, wait, cancel, availability or evidence reader.
An agent submits a measurement, gets an opaque handle, validates the handles it will
wait on, and ends its turn with its waiting reply. Core resumes the same conversation
once, when the measurement ends; there is nothing to poll. The wire is the one in
``agent_wire``; the host-side service that answers it lives in ``vs_runtime``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vs_evaluation.agent_models import EvidenceKindsArgs, SubmitCall, WaitArgs, WaitCall
from vs_evaluation.agent_wire import Offer, SocketClient
from vs_mcp.api import StdioServerDescriptor, ToolServerDescriptor, ToolSpec, serve_stdio

if TYPE_CHECKING:
    from collections.abc import Sequence

SERVER_NAME = "vs-evaluation"
SUBMIT_TOOL = "submit_evaluation"
VALIDATE_WAIT_TOOL = "validate_evaluation_wait"
CORE_EVALUATION_TOOLS = (SUBMIT_TOOL, VALIDATE_WAIT_TOOL)


CORE_MCP_PACKAGES = ("vs-evaluation", "vs-evaluator-protocol", "vs-mcp")
"""The framework packages the MCP process imports, by source directory name under ``libs``."""


def core_evaluation_mcp_descriptor(
    token: str, socket_path: str, *, import_roots: Sequence[str] = ()
) -> ToolServerDescriptor:
    """Describe the thin MCP process for one core scope's host-issued token.

    *import_roots* are the directories, as the process sees them, that hold
    :data:`CORE_MCP_PACKAGES`. A process that does not already import the
    framework (an agent container) needs them on its ``PYTHONPATH``, and a
    provider may start it with a scrubbed environment, so they travel on the
    server itself.
    """
    return StdioServerDescriptor(
        name=SERVER_NAME,
        command="python",
        args=("-m", "vs_evaluation.agent_core_mcp"),
        env=(("PYTHONPATH", os.pathsep.join(import_roots)),) if import_roots else (),
        runtime_env=(("VS_EVALUATION_SOCKET", socket_path), ("VS_EVALUATION_TOKEN", token)),
    )


def build_core_evaluation_tools(*, socket_path: Path, token: str) -> tuple[ToolSpec[Any], ...]:
    """The two core-path tools; every other option is absent from the schema."""
    offer = Offer(SocketClient(socket_path), token)
    return (
        offer.tool(
            SUBMIT_TOOL,
            "Submit this workspace's current candidate for evaluation without blocking. "
            "Returns an opaque handle, or an error naming why the run refused it (for "
            "example, the submission budget for this exact candidate is spent). Then end "
            "the turn with waiting_for_evaluation: the run resumes you once when the "
            "evaluation ends. There is nothing to poll.",
            EvidenceKindsArgs,
            SubmitCall,
        ),
        offer.tool(
            VALIDATE_WAIT_TOOL,
            "Validate the evaluation handles you will wait on before ending with "
            "waiting_for_evaluation. Only handles this agent submitted in this turn are valid.",
            WaitArgs,
            WaitCall,
        ),
    )


def main() -> None:
    """Serve the tools over stdio from the injected socket and token."""
    serve_stdio(
        build_core_evaluation_tools(
            socket_path=Path(os.environ["VS_EVALUATION_SOCKET"]),
            token=os.environ["VS_EVALUATION_TOKEN"],
        ),
        server_name=SERVER_NAME,
    )


if __name__ == "__main__":
    main()


__all__ = [
    "CORE_EVALUATION_TOOLS",
    "CORE_MCP_PACKAGES",
    "SUBMIT_TOOL",
    "VALIDATE_WAIT_TOOL",
    "build_core_evaluation_tools",
    "core_evaluation_mcp_descriptor",
    "main",
]
