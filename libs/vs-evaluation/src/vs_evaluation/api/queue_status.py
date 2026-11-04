"""Public queue status contracts: the reader interface, its readings, the wire and the tool.

``QueueStatusReader`` is the read-only interface every run environment implements;
``QueueStatusService`` serves one on the host and ``SocketQueueStatusReader`` reads it
from wherever the agent runs; ``build_queue_status_tools`` and
``queue_status_mcp_descriptor`` are the agent tool server built on ``vs_mcp``.
"""

from vs_evaluation.queue_status import (
    FRESH_FOR_S,
    MAX_REASON_CHARS,
    QUEUE_READING,
    CachedQueueStatusReader,
    LocalQueueStatusReader,
    OpaqueQueueStatusReader,
    QueueContention,
    QueueItem,
    QueueItemKind,
    QueueItemState,
    QueueKind,
    QueueReading,
    QueueStatus,
    QueueStatusReader,
    QueueStatusUnavailable,
)
from vs_evaluation.queue_status_mcp import (
    QUEUE_STATUS_TOOLS,
    READ_QUEUE_STATUS_TOOL,
    ReadQueueStatusArgs,
    build_queue_status_tools,
    queue_status_mcp_descriptor,
)
from vs_evaluation.queue_status_wire import (
    CALL_TIMEOUT_S,
    QueueStatusService,
    SocketQueueStatusReader,
)

__all__ = [
    "CALL_TIMEOUT_S",
    "FRESH_FOR_S",
    "MAX_REASON_CHARS",
    "QUEUE_READING",
    "QUEUE_STATUS_TOOLS",
    "READ_QUEUE_STATUS_TOOL",
    "CachedQueueStatusReader",
    "LocalQueueStatusReader",
    "OpaqueQueueStatusReader",
    "QueueContention",
    "QueueItem",
    "QueueItemKind",
    "QueueItemState",
    "QueueKind",
    "QueueReading",
    "QueueStatus",
    "QueueStatusReader",
    "QueueStatusService",
    "QueueStatusUnavailable",
    "ReadQueueStatusArgs",
    "SocketQueueStatusReader",
    "build_queue_status_tools",
    "queue_status_mcp_descriptor",
]
