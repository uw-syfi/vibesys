"""A completed turn's cost/token accounting reaches both usage.jsonl and the live sink.

``AgentClient._write_usage_record`` is the single call site that already
appends a durable ``usage.jsonl`` row; this verifies the injected event sink
receives the exact same accounting alongside that write, not instead of it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, override

from vs_agent.api import (
    AgentClient,
    AgentEventSink,
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentTurnRequest,
)
from vs_agent.api.testing import FakeDriver
from vs_agent.drivers.fake import usage

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.events import AgentOutputChannel, AgentStatusData, TodoItemData, ToolResultPayload


@dataclass(frozen=True, slots=True)
class _TurnUsageCall:
    """One recorded ``AgentEventSink.turn_usage`` invocation."""

    input_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    output_tokens: int | None
    total_cost_usd: float | None
    duration_ms: int | None
    agent_kind: str | None
    round_label: str | None
    invocation_id: str | None


class _CapturingSink(AgentEventSink):
    """Minimal :class:`AgentEventSink` that records every ``turn_usage`` call."""

    def __init__(self) -> None:
        self.turn_usage_calls: list[_TurnUsageCall] = []

    @override
    def agent_output(
        self,
        content: str,
        *,
        channel: AgentOutputChannel = "assistant",
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del content, channel, status, agent_kind, round_label, invocation_id

    @override
    def tool_call(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        call_id: str | None = None,
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del tool, args, call_id, status, agent_kind, round_label, invocation_id

    @override
    def tool_result(
        self,
        tool: str,
        content: str,
        *,
        call_id: str | None = None,
        is_error: bool = False,
        payload: ToolResultPayload | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del tool, content, call_id, is_error, payload, agent_kind, round_label, invocation_id

    @override
    def todo_update(
        self,
        todos: list[TodoItemData],
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del todos, agent_kind, round_label, invocation_id

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
        del input_tokens, context_window, model, agent_kind, round_label, invocation_id

    @override
    def turn_usage(
        self,
        *,
        input_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_cost_usd: float | None = None,
        duration_ms: int | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        self.turn_usage_calls.append(
            _TurnUsageCall(
                input_tokens=input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
                output_tokens=output_tokens,
                total_cost_usd=total_cost_usd,
                duration_ms=duration_ms,
                agent_kind=agent_kind,
                round_label=round_label,
                invocation_id=invocation_id,
            )
        )


def test_turn_usage_reaches_the_sink_alongside_the_usage_jsonl_row(tmp_path: Path) -> None:
    driver = FakeDriver(
        turn=[usage(input_tokens=11, output_tokens=22, model="fake-model")],
        answer="done",
    )
    sink = _CapturingSink()
    client = AgentClient(driver, provider="fake", log_dir=tmp_path, event_sink=sink)
    spec = AgentSessionSpec(
        role="dynamic-implementer",
        provider="fake",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    turn = AgentTurnRequest(message="work", label="round-1", invocation_id="invocation-1")

    result = client.run(session_spec=spec, turn=turn)

    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 22

    # The durable usage.jsonl row still gets written.
    rows = [
        json.loads(line)
        for line in (tmp_path / "usage.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["kind"] == "dynamic-implementer"
    assert rows[0]["round_label"] == "round-1"
    assert rows[0]["input_tokens"] == 11
    assert rows[0]["output_tokens"] == 22

    # The live sink receives the exact same accounting, attributed the same way.
    assert len(sink.turn_usage_calls) == 1
    call = sink.turn_usage_calls[0]
    assert call.input_tokens == 11
    assert call.output_tokens == 22
    assert call.cache_creation_input_tokens == 0
    assert call.cache_read_input_tokens == 0
    assert call.total_cost_usd == 0.0
    assert call.duration_ms == 10
    assert call.agent_kind == "dynamic-implementer"
    assert call.round_label == "round-1"
    assert call.invocation_id == "invocation-1"


def test_turn_usage_reaches_the_sink_even_without_a_log_dir(tmp_path: Path) -> None:
    """The live sink is independent of the durable write: no log_dir, still one call.

    A caller that only wants the live cost ledger (no file logging configured)
    must still see every turn's accounting.
    """
    driver = FakeDriver(
        turn=[usage(input_tokens=5, output_tokens=7)],
        answer="done",
    )
    sink = _CapturingSink()
    client = AgentClient(driver, provider="fake", event_sink=sink)
    spec = AgentSessionSpec(
        role="dynamic-judge",
        provider="fake",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    turn = AgentTurnRequest(message="work")

    client.run(session_spec=spec, turn=turn)

    assert not (tmp_path / "usage.jsonl").exists()
    assert len(sink.turn_usage_calls) == 1
    call = sink.turn_usage_calls[0]
    assert call.input_tokens == 5
    assert call.output_tokens == 7
    assert call.agent_kind == "dynamic-judge"
