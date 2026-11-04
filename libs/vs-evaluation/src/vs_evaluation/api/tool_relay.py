"""Public tool relay: serve tools on the host, forward them from an agent's container.

``ToolRelayService`` serves a fixed set of ``vs_mcp`` tools on a unix socket in the run's
private socket directory, on the host side of the trust boundary. ``relay_mcp_descriptor``
describes the agent-side tool server that forwards to it, which needs only the container
interpreter's packages. ``relay_check_descriptor`` describes its startup check.
"""

from vs_evaluation.tool_relay_mcp import (
    SOCKET_ENV,
    TOKEN_ENV,
    build_relay_server,
    relay_check_descriptor,
    relay_mcp_descriptor,
)
from vs_evaluation.tool_relay_wire import (
    CALL_TIMEOUT_S,
    RELAY_REPLY,
    RelayCall,
    RelayedTool,
    RelayPong,
    RelayRefusal,
    RelayRefusedError,
    RelayResult,
    RelayToolList,
    RelayUnavailableError,
    ToolRelayClient,
    ToolRelayService,
)

__all__ = [
    "CALL_TIMEOUT_S",
    "RELAY_REPLY",
    "SOCKET_ENV",
    "TOKEN_ENV",
    "RelayCall",
    "RelayPong",
    "RelayRefusal",
    "RelayRefusedError",
    "RelayResult",
    "RelayToolList",
    "RelayUnavailableError",
    "RelayedTool",
    "ToolRelayClient",
    "ToolRelayService",
    "build_relay_server",
    "relay_check_descriptor",
    "relay_mcp_descriptor",
]
