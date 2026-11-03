"""The real ``Machine``: scripted effects run as processes on this host.

This is the e2e tier's one use of real processes. ``Shell`` runs in the agent's worktree with the
agent's environment, and ``Mcp`` starts the orchestrator's MCP server exactly as the sidecar's
``mcp.json`` names it, with the session identity, and speaks its stdio protocol for one call.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

from e2e.effects import Mcp, Outcome, Shell

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from agentshim import StdioMcpServer

#: Guards a hung command; a run that passes never comes near it.
BOUND_SECONDS = 120.0


class HostMachine:
    """Carries out effects as the agent would: shell commands and calls of the orchestrator's tools."""

    def __init__(self, *, cwd: Path, env: Mapping[str, str], server: StdioMcpServer) -> None:
        """Work in *cwd* with *env*; call tools through *server*, given the identity in its own env."""
        self._cwd = cwd
        self._env = dict(env)
        self._server = server

    def run(self, effect: Shell | Mcp) -> Outcome:
        """See ``Machine.run``."""
        if isinstance(effect, Shell):
            return self._shell(effect)
        return self._tool(effect)

    def _shell(self, effect: Shell) -> Outcome:
        done = subprocess.run(  # noqa: S603 - the scripted agent's own command, from a script in this directory
            effect.argv,
            cwd=self._cwd,
            env=self._env,
            capture_output=True,
            text=True,
            timeout=BOUND_SECONDS,
            check=False,
        )
        return Outcome(
            done.returncode == 0, done.stdout if done.returncode == 0 else done.stdout + done.stderr
        )

    def _tool(self, effect: Mcp) -> Outcome:
        requests: list[dict[str, Any]] = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "scripted", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": effect.tool, "arguments": dict(effect.arguments)},
            },
        ]
        done = subprocess.run(  # noqa: S603 - the server the daemon wrote into mcp.json
            [self._server.command, *self._server.args],
            input="".join(json.dumps(r) + "\n" for r in requests),
            env={**self._env, **self._server.env},
            cwd=self._cwd,
            capture_output=True,
            text=True,
            timeout=BOUND_SECONDS,
            check=False,
        )
        answers = {
            a.get("id"): a
            for a in (json.loads(line) for line in done.stdout.splitlines() if line.strip())
        }
        call = answers.get(2)
        if call is None:
            return Outcome(
                ok=False,
                text=f"the MCP server answered no tools/call (exit {done.returncode}): {done.stdout}{done.stderr}",
            )
        if "error" in call:
            return Outcome(ok=False, text=json.dumps(call["error"]))
        result = call["result"]
        text = "".join(block.get("text", "") for block in result.get("content", []))
        return Outcome(ok=not result.get("isError", False), text=text)
