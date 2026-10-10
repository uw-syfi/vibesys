"""The far end of a provider's long-lived process, for any stream provider (a test fake).

``stream_peers`` returns a Claude or Codex fake that answers each turn with the
next text, plus the means to make it forget what its container held, so a test
runs one scenario over every provider agentshim lists in
``stream_provider_names()`` without naming one in the scenario.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentshim.providers.codex.app_server.protocol import TurnStartParams, UserInputText
from agentshim.testing import (
    ClaudePeerTurn,
    ClaudeStreamPeers,
    CodexScript,
    CodexStep,
    Hang,
    Say,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentshim.execution.process import SpawnRequest
    from agentshim.testing import FakePeer


@dataclass(frozen=True)
class StreamPeers:
    """A provider's scripted processes and the container state they share."""

    build: Callable[[SpawnRequest], FakePeer]
    forget_conversations: Callable[[], None]
    """Drop every conversation the provider kept, as replacing its container does."""
    prompts: Callable[[], list[str]]
    """The text of every user message the provider's processes received, in order."""


def stream_peers(provider: str, *texts: str, hang_last: bool = False) -> StreamPeers:
    """Fake ``provider`` answering one turn per text; the last turn hangs with ``hang_last``."""
    if provider == "claude":
        turns = [ClaudePeerTurn(text=text) for text in texts]
        if hang_last:
            turns[-1] = ClaudePeerTurn(stall=True)
        claude = ClaudeStreamPeers(turns)
        return StreamPeers(
            claude.build,
            claude.known_sessions.clear,
            lambda: [prompt for peer in claude.peers for prompt in peer.prompts],
        )
    codex = CodexScript()
    for index, text in enumerate(texts):
        last = index == len(texts) - 1
        codex.turn(Hang() if hang_last and last else Say(text))

    def forget() -> None:
        for thread in codex.thread_ids():
            codex.forget(thread)

    def prompts() -> list[str]:
        return _codex_prompts(codex)

    return StreamPeers(codex.peer, forget, prompts)


def answering_with(provider: str, answer: Callable[[], str]) -> StreamPeers:
    """Fake ``provider`` whose every turn answers with ``answer()``, evaluated as the turn starts.

    ``answer`` runs on the turn's own thread at the moment the provider takes the prompt, so
    it can do the agent's work (write a file, make a commit) and name it in the reply.
    """
    if provider == "claude":
        claude = _WorkingClaude(answer)
        return StreamPeers(
            claude.build,
            claude.known_sessions.clear,
            lambda: [prompt for peer in claude.peers for prompt in peer.prompts],
        )
    codex = _WorkingCodex(answer)

    def forget() -> None:
        for thread in codex.thread_ids():
            codex.forget(thread)

    def prompts() -> list[str]:
        return _codex_prompts(codex)

    return StreamPeers(codex.peer, forget, prompts)


class _WorkingClaude(ClaudeStreamPeers):
    def __init__(self, answer: Callable[[], str]) -> None:
        super().__init__()
        self._answer = answer

    def next_turn(self) -> ClaudePeerTurn:
        return ClaudePeerTurn(text=self._answer())


class _WorkingCodex(CodexScript):
    def __init__(self, answer: Callable[[], str]) -> None:
        super().__init__()
        self._answer = answer

    def next_turn(self) -> tuple[CodexStep, ...]:
        return (Say(self._answer()),)


def _codex_prompts(codex: CodexScript) -> list[str]:
    """Every prompt the scripted Codex server received in a ``turn/start``."""
    return [
        item.text
        for request in codex.requests("turn/start")
        if isinstance(request.params, TurnStartParams)
        for item in request.params.input
        if isinstance(item, UserInputText)
    ]
