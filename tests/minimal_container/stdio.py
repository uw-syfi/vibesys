"""A line-oriented JSON client for a process started in the editor container.

Both the Codex app server and every MCP server speak newline-delimited JSON over
stdio. This starts one through the container's ``docker exec`` and reads its
replies on a reader thread, so a server that never answers fails the test at a
hang guard instead of blocking it.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

#: How long a reply may take before the process is declared hung.
HANG_GUARD_SECONDS = 120.0


class ContainerProcessError(RuntimeError):
    """The process exited, or stayed silent, before answering."""


class StdioJsonProcess:
    """A container process driven one JSON line at a time."""

    def __init__(self, argv: Sequence[str]) -> None:
        """Start *argv* (already wrapped in ``docker exec -i``) with piped stdio."""
        self._process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-960021 [S603]; argv is the sandbox's own `docker exec` wrapper.
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        self._readers = [
            threading.Thread(target=self._pump_stdout, daemon=True),
            threading.Thread(target=self._pump_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _pump_stdout(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _pump_stderr(self) -> None:
        assert self._process.stderr is not None
        self._stderr.extend(self._process.stderr)

    def __enter__(self) -> StdioJsonProcess:
        """Return the running process."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close its stdin, end it, and reap it."""
        self.close()

    def send(self, message: dict[str, Any]) -> None:
        """Write one JSON line to the process."""
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def reply_to(self, request_id: int) -> dict[str, Any]:
        """Read lines until the one answering *request_id*; return it."""
        while True:
            try:
                # test-isolation: the deadline only guards a hang; a reply returns at once
                line = self._lines.get(timeout=HANG_GUARD_SECONDS)
            except queue.Empty:
                message = f"no answer to request {request_id} in {HANG_GUARD_SECONDS:g}s"
                raise ContainerProcessError(self._describe(message)) from None
            if line is None:
                raise ContainerProcessError(self._describe("exited before answering"))
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue  # log output on stdout is not a reply
            if isinstance(payload, dict) and payload.get("id") == request_id:
                return payload

    def _describe(self, what: str) -> str:
        return f"{what}; stderr: {''.join(self._stderr)[-2000:]!r}"

    def close(self) -> None:
        """End the process; safe to call twice."""
        if self._process.stdin is not None and not self._process.stdin.closed:
            self._process.stdin.close()
        self._process.terminate()
        self._process.wait(timeout=HANG_GUARD_SECONDS)
        for reader in self._readers:
            reader.join(timeout=HANG_GUARD_SECONDS)
        for stream in (self._process.stdout, self._process.stderr):
            if stream is not None:
                stream.close()


def mcp_initialize(process: StdioJsonProcess) -> dict[str, Any]:
    """Run the MCP initialize handshake; return the server's ``result``."""
    process.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "minimal-container-tier", "version": "0"},
            },
        }
    )
    reply = process.reply_to(1)
    result = reply.get("result")
    if not isinstance(result, dict):
        message = f"initialize was refused: {reply}"
        raise ContainerProcessError(message)
    process.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return result


def mcp_tool_names(process: StdioJsonProcess) -> list[str]:
    """List the tools of an initialized MCP server."""
    process.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    reply = process.reply_to(2)
    tools = reply.get("result", {}).get("tools")
    if not isinstance(tools, list):
        message = f"tools/list was refused: {reply}"
        raise ContainerProcessError(message)
    return [tool["name"] for tool in tools]
