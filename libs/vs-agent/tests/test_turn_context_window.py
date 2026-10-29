"""A turn's status shows the model's context window as agentshim states it, or none.

VibeSys once kept its own prefix table of context windows. It had no Claude 5
entry, so ``claude-opus-5-5`` matched the ``claude-`` fallback and runs showed
a 200k limit for a 1M-token model. The window now comes from agentshim, and a
model agentshim does not know shows tokens used with no limit, never a guess.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import pytest

from vs_agent.api import (
    AgentClient,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    NullAgentEventSink,
    SessionScope,
)
from vs_agent.api.testing import FakeProvider, assistant_text, usage
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from pathlib import Path


class _WindowSink(NullAgentEventSink):
    """Records the context window each usage update carries."""

    def __init__(self) -> None:
        self.windows: list[int | None] = []

    @override
    def usage_update(
        self,
        input_tokens: int,
        *,
        context_window: int | None = None,
        model: str | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del input_tokens, model, agent_kind, round_label, invocation_id
        self.windows.append(context_window)


def _windows_of_a_turn(tmp_path: Path, provider: str, model: str) -> list[int | None]:
    sink = _WindowSink()
    client = AgentClient(
        FakeProvider(
            turn=[assistant_text("done"), usage(input_tokens=5, output_tokens=1)],
            threads=SimThreads(),
        ),
        provider=provider,
        event_sink=sink,
        clock=ManualClock(),
        threads=SimThreads(),
    )
    spec = AgentSessionSpec(
        role="implementer",
        provider=provider,
        model=model,
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    try:
        client.run(
            session_spec=spec,
            turn=AgentTurnRequest(message="work", invocation_id="inv", label="round 1"),
            session_key=AgentSessionKey(SessionScope.ROLE, "implementer"),
        )
    finally:
        client.close()
    return sink.windows


@pytest.mark.parametrize(
    "model", ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
)
def test_a_claude_5_model_shows_its_1m_window(tmp_path: Path, model: str) -> None:
    windows = _windows_of_a_turn(tmp_path, "claude", model)
    assert windows
    assert set(windows) == {1_000_000}


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("claude", "claude-opus-9-9"),
        ("claude", "claude-opus-5-5[1m]"),
        ("codex", "gpt-6-luna-preview"),
        ("fake", "claude-opus-5-5"),
    ],
)
def test_a_model_agentshim_does_not_know_shows_no_window(
    tmp_path: Path, provider: str, model: str
) -> None:
    windows = _windows_of_a_turn(tmp_path, provider, model)
    assert windows
    assert set(windows) == {None}
