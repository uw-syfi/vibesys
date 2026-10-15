"""The agent's read-only queue status tool server.

It offers exactly one tool, ``read_queue_status``, which takes no arguments and
returns the run's queue reading as JSON: the run's own evaluation and agent GPU jobs
(state, time waited, jobs ahead and estimated start where the backend can tell) and
anonymous contention counts. It registers no write tool, and the reader it serves has
no write method, so it is read-only by construction.

The server runs where the agent runs and reads through the host's queue status
socket (``queue_status_wire``); the host owns the backend reader.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from vs_evaluation.queue_status import FRESH_FOR_S
from vs_evaluation.queue_status_wire import SocketQueueStatusReader
from vs_mcp.api import StdioServerDescriptor, ToolServerDescriptor, ToolSpec, serve_stdio
from vs_sim.api import MonotonicClock, UnixNetwork

if TYPE_CHECKING:
    from vs_evaluation.queue_status import QueueStatusReader

SERVER_NAME = "vs-queue-status"
READ_QUEUE_STATUS_TOOL = "read_queue_status"
QUEUE_STATUS_TOOLS = (READ_QUEUE_STATUS_TOOL,)
SOCKET_ENV = "VS_QUEUE_STATUS_SOCKET"

_DESCRIPTION = (
    "Read the evaluation and GPU job queue of this run. Returns JSON: `queue` says what "
    "jobs wait in (`shared`: a cluster scheduler shared with other users; `local`: this "
    "run's own machine; `opaque`: a service that reports no queue). `items` lists this "
    "run's own trusted evaluations and agent GPU jobs with their state, seconds waited, "
    "`jobs_ahead` and `estimated_start_in_seconds` when the scheduler can tell (null when "
    "it cannot; `items` itself is null when the backend cannot list them). `contention` "
    "counts jobs waiting and running and GPUs busy and free, anonymously. A reading may be "
    f"up to {FRESH_FOR_S:.0f} seconds old. `status: unavailable` gives the reason the queue could not be "
    "read. A long expected wait means a queued evaluation will land late: keep working "
    "instead of waiting for it. This tool only reads; it cannot change any job."
)


class ReadQueueStatusArgs(BaseModel):
    """The tool takes no arguments; an unknown one is refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def build_queue_status_tools(reader: QueueStatusReader) -> tuple[ToolSpec[ReadQueueStatusArgs]]:
    """The one read-only tool, answering from ``reader``."""

    def handler(args: ReadQueueStatusArgs) -> str:
        del args
        return reader.read().model_dump_json()

    return (ToolSpec(READ_QUEUE_STATUS_TOOL, _DESCRIPTION, ReadQueueStatusArgs, handler),)


def queue_status_mcp_descriptor(socket_path: str) -> ToolServerDescriptor:
    """Describe the tool server that reads the host's queue status socket at ``socket_path``.

    The interpreter and import roots are the launcher's to choose, as for the evaluation
    tool server; the socket path is an endpoint, so it rides the launch-only environment.
    """
    return StdioServerDescriptor(
        name=SERVER_NAME,
        command="python",
        args=("-m", "vs_evaluation.queue_status_mcp"),
        runtime_env=((SOCKET_ENV, socket_path),),
    )


def main() -> None:
    """Serve the tool over stdio, reading through the injected socket."""
    serve_stdio(
        build_queue_status_tools(
            SocketQueueStatusReader(
                Path(os.environ[SOCKET_ENV]), network=UnixNetwork(), clock=MonotonicClock()
            )
        ),
        server_name=SERVER_NAME,
    )


if __name__ == "__main__":
    main()


__all__ = [
    "QUEUE_STATUS_TOOLS",
    "READ_QUEUE_STATUS_TOOL",
    "SERVER_NAME",
    "SOCKET_ENV",
    "ReadQueueStatusArgs",
    "build_queue_status_tools",
    "main",
    "queue_status_mcp_descriptor",
]
