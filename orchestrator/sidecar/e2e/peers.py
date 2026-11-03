"""Provider peers that act: agentshim's scripted Claude and Codex, with each turn made of real effects.

agentshim's scripted peers write frames and call nothing. These subclass its worlds so that the
turn for each user message is made when the message arrives: the ``Actor`` runs the role's effects
(real commands, real calls of the orchestrator's tools), and the turn the peer plays back mirrors
them as the tool calls and commands a real agent would show, with their real output. The engine,
the normalizer and everything above see what a real agent produces.

One lock serializes the writes to a peer. The sidecar can write a steer from a server thread while
a turn's effects run on the worker thread; the lock makes the later message wait its turn, so the
n-th message is always answered by the n-th scripted step.
"""

from __future__ import annotations

import json
import shlex
import threading
from typing import TYPE_CHECKING, Any

from agentshim.testing import (
    CallMcp,
    ClaudePeerTurn,
    ClaudeStreamPeers,
    CodexScript,
    RunCommand,
    Say,
)

from e2e.effects import Mcp, Shell

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentshim.execution.process import SpawnRequest
    from agentshim.testing.codex_app_server import CodexStep
    from agentshim.testing.process import FakePeer, PeerOutput

    from e2e.actor import Actor

#: The name the orchestrator's tool server has in the agent's MCP configuration.
SERVER = "vibesys"


class _Recorded:
    """A peer that notes the user messages written to it and lets one writer at a time in."""

    def __init__(
        self, inner: FakePeer, inbox: list[str], text_of: Callable[[dict[str, Any]], str | None]
    ) -> None:
        """Wrap *inner*; append each user message *text_of* finds in a line of stdin to *inbox*."""
        self._inner = inner
        self._inbox = inbox
        self._text_of = text_of
        self._lock = threading.Lock()
        self._pending = ""

    def on_start(self) -> Sequence[PeerOutput]:
        with self._lock:
            return self._inner.on_start()

    def on_stdin(self, data: str) -> Sequence[PeerOutput]:
        with self._lock:
            self._pending += data
            *lines, self._pending = self._pending.split("\n")
            for line in lines:
                text = self._text_of(json.loads(line)) if line.strip() else None
                if text is not None:
                    self._inbox.append(text)
            return self._inner.on_stdin(data)

    def on_stdin_closed(self) -> Sequence[PeerOutput]:
        with self._lock:
            return self._inner.on_stdin_closed()


def _claude_text(frame: dict[str, Any]) -> str | None:
    if frame.get("type") != "user":
        return None
    content = frame.get("message", {}).get("content", "")
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


def _codex_text(frame: dict[str, Any]) -> str | None:
    if frame.get("method") != "turn/start":
        return None
    return "".join(
        item.get("text", "")
        for item in frame.get("params", {}).get("input", [])
        if item.get("type") == "text"
    )


class ActingClaudePeers(ClaudeStreamPeers):
    """Claude's scripted CLI, whose n-th turn is what the actor does for the n-th message."""

    def __init__(self, actor: Actor, on_turn: Callable[[], None]) -> None:
        """Answer each user message by running it through *actor*; call *on_turn* before each."""
        super().__init__()
        self._actor = actor
        self._on_turn = on_turn
        self._inbox: list[str] = []
        self._answered = 0

    def build(self, request: SpawnRequest) -> FakePeer:
        """The far end of a ``claude`` process, recording what it is sent."""
        return _Recorded(super().build(request), self._inbox, _claude_text)

    def next_turn(self) -> ClaudePeerTurn:
        """The turn for the next unanswered message: its effects run now, and it mirrors them."""
        self._on_turn()
        message = self._inbox[self._answered]
        self._answered += 1
        done = self._actor.act(message)
        return ClaudePeerTurn(
            text=done.text,
            tool_calls=tuple(_claude_call(step.effect, step.outcome.text) for step in done.steps),
        )


def _claude_call(effect: Mcp | Shell, output: str) -> tuple[str, dict[str, Any], str]:
    if isinstance(effect, Mcp):
        return (f"mcp__{SERVER}__{effect.tool}", dict(effect.arguments), output)
    return ("Bash", {"command": shlex.join(effect.argv)}, output)


class ActingCodex(CodexScript):
    """Codex's scripted app-server, whose n-th turn is what the actor does for the n-th message."""

    def __init__(self, actor: Actor, on_turn: Callable[[], None]) -> None:
        """Answer each ``turn/start`` by running its message through *actor*; call *on_turn* before each."""
        super().__init__()
        self._actor = actor
        self._on_turn = on_turn
        self._inbox: list[str] = []
        self._answered = 0

    def peer(self, request: SpawnRequest | None = None) -> FakePeer:
        """A fresh app-server process, recording the messages it is sent."""
        return _Recorded(super().peer(request), self._inbox, _codex_text)

    def next_turn(self) -> tuple[CodexStep, ...]:
        """The steps for the next unanswered message: its effects run now, and they are mirrored."""
        self._on_turn()
        message = self._inbox[self._answered]
        self._answered += 1
        done = self._actor.act(message)
        return (
            *(_codex_step(step.effect, step.outcome.text) for step in done.steps),
            Say(done.text),
        )


def _codex_step(effect: Mcp | Shell, output: str) -> RunCommand | CallMcp:
    if isinstance(effect, Mcp):
        return CallMcp(SERVER, effect.tool, dict(effect.arguments), output=output)
    return RunCommand(shlex.join(effect.argv), output=output)
