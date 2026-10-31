"""A turn that fails still records what it spent, and the conversation it ran in.

The usage of a failed turn is the audit row of tokens already paid for, and its provider session
id names the transcript the CLI wrote, which a later read joins to the run. A failure reaches the
client three ways, each translated from the library's own error: a timeout that parsed part of the
turn, a reply refused for its size or shape after a complete result, and a turn that failed before reporting
anything. Each is driven here through the production session path with a scripted CLI.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import agentshim
import pytest
from agentshim.testing import FakeCommandHandle
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.events import CoreEventType, TurnUsageData
from vibesys.run import CoreAgentEventSink, EventJournal
from vs_agent.api import (
    AgentClient,
    AgentExecutionPolicy,
    AgentOutputSchemaError,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentTurnTimeoutError,
    CostBasis,
    SessionScope,
)
from vs_agent.api.testing import (
    FakeExecutor,
    FakeProvider,
    TokenUsage,
    assistant_text,
    fake_agentshim_launcher,
    scripted_turn,
)
from vs_sim.api.testing import ManualClock, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


_COUNTS = st.integers(0, 10**6)


_DEGENERATE_REPLY = "a" + " " * (1 << 15)
"""A reply that is mostly one long run of whitespace, which the client refuses as degenerate."""


def _tokens(draw: Callable[[st.SearchStrategy[int]], int]) -> TokenUsage:
    input_tokens = draw(_COUNTS)
    output_tokens = draw(_COUNTS)
    cache_read = draw(st.integers(0, input_tokens))
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=draw(st.integers(0, input_tokens - cache_read)),
        # A Claude result prints no reasoning count outside its per-model usage.
        reasoning_output_tokens=0,
    )


@st.composite
def _spent(draw: st.DrawFn) -> TokenUsage:
    """What a turn spent before it failed, under the library's invariants."""
    return _tokens(draw)


class _TimesOutAfterStreaming(FakeExecutor):
    """A CLI that prints ``lines`` and then outlasts its budget, as one that hangs mid-turn does."""

    def __init__(self, lines: Sequence[str]) -> None:
        super().__init__([])
        self._lines = lines

    def run(
        self, request: agentshim.CommandRequest, sink: agentshim.CommandStreamSink
    ) -> agentshim.CommandResult:
        sink.started(FakeCommandHandle())
        for line in self._lines:
            sink.stdout(line)
        raise agentshim.CliTimeoutError(request.argv, 1.0)


def _run(
    root: Path, source: FakeExecutor | FakeProvider
) -> tuple[BaseException, TurnUsageData, dict[str, object]]:
    """Run one claude turn that fails; the error, its journal event and its usage row."""
    journal = EventJournal(clock=ManualClock(), threads=SimThreads())
    journal.attach(root / "run", "run-1")
    logs = root / "logs"
    logs.mkdir()
    driver = (
        source
        if isinstance(source, FakeProvider)
        else fake_agentshim_launcher(provider="claude", executor=source, threads=SimThreads())
    )
    client = AgentClient(
        driver,
        provider="claude",
        event_sink=CoreAgentEventSink(journal.record, clock=ManualClock()),
        log_dir=logs,
        clock=ManualClock(),
        threads=SimThreads(),
    )
    spec = AgentSessionSpec(
        role="implementer",
        provider="claude",
        workspace=root,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    turn = AgentTurnRequest(message="work", invocation_id="inv-1", label="round 1")
    try:
        with pytest.raises((AgentTurnTimeoutError, AgentOutputSchemaError)) as raised:
            client.run(
                session_spec=spec,
                turn=turn,
                session_key=AgentSessionKey(SessionScope.ROLE, "implementer"),
            )
    finally:
        client.close()
    (event,) = (e for e in journal.read() if e.type is CoreEventType.TURN_USAGE_RECORDED)
    assert isinstance(event.data, TurnUsageData)
    row = json.loads((logs / "usage.jsonl").read_text(encoding="utf-8"))
    return raised.value, event.data, row


def _fields(data: TurnUsageData) -> tuple[int | None, ...]:
    return (
        data.input_tokens,
        data.cache_read_input_tokens,
        data.cache_creation_input_tokens,
        data.output_tokens,
        data.reasoning_output_tokens,
    )


def _expected(spent: TokenUsage) -> tuple[int, ...]:
    return (
        spent.input_tokens,
        spent.cache_read_input_tokens,
        spent.cache_write_input_tokens,
        spent.output_tokens,
        spent.reasoning_output_tokens,
    )


@settings(max_examples=examples(pr=15, nightly=150), deadline=None)
@given(spent=_spent())
def test_a_turn_that_timed_out_after_part_of_its_stream_records_its_spend_and_its_conversation(
    spent: TokenUsage,
) -> None:
    scripted = scripted_turn("claude", text="partial", session_id="sess-timeout", usage=spent)
    with tempfile.TemporaryDirectory() as directory:
        error, data, row = _run(Path(directory), _TimesOutAfterStreaming(scripted.stdout))

    assert isinstance(error, AgentTurnTimeoutError)
    assert error.provider_session_id == "sess-timeout"
    assert _fields(data) == _expected(spent)
    assert data.provider_session_id == "sess-timeout"
    assert data.cost_basis is not None
    assert row["provider_session_id"] == "sess-timeout"
    assert row["cost_basis"] == data.cost_basis.value
    assert (
        row["input_tokens"],
        row["cache_read_input_tokens"],
        row["cache_creation_input_tokens"],
        row["output_tokens"],
        row["reasoning_output_tokens"],
    ) == _expected(spent)


@settings(max_examples=examples(pr=8, nightly=60), deadline=None)
@given(spent=_spent())
def test_a_reply_refused_after_a_complete_result_records_its_spend_and_conversation(
    spent: TokenUsage,
) -> None:
    """The guard ends the turn once its reply is degenerate; the turn still finished and spent."""
    reported = agentshim.UsageReport(
        usage=agentshim.ProviderUsage(
            tokens=agentshim.TokenUsage(
                input_tokens=spent.input_tokens,
                output_tokens=spent.output_tokens,
                cache_read_input_tokens=spent.cache_read_input_tokens,
                cache_write_input_tokens=spent.cache_write_input_tokens,
            )
        ),
        cost_usd=None,
    )
    provider = FakeProvider(
        turn=[assistant_text(_DEGENERATE_REPLY), reported], threads=SimThreads()
    )
    with tempfile.TemporaryDirectory() as directory:
        error, data, row = _run(Path(directory), provider)

    assert isinstance(error, AgentOutputSchemaError)
    assert _fields(data)[:4] == _expected(spent)[:4]
    assert data.provider_session_id is not None
    assert row["provider_session_id"] == data.provider_session_id
    assert data.cost_basis is CostBasis.NOT_REPORTED


def test_a_turn_that_failed_before_reporting_anything_records_no_basis_it_was_not_given(
    tmp_path: Path,
) -> None:
    """Nothing reported is ``NOT_REPORTED`` on a new line, never null: null means an old line."""
    scripted = scripted_turn("claude", text="partial", session_id=None)
    error, data, row = _run(tmp_path, _TimesOutAfterStreaming(scripted.stdout[:1]))

    assert isinstance(error, AgentTurnTimeoutError)
    assert data.cost_basis is CostBasis.NOT_REPORTED
    assert row["cost_basis"] == "not_reported"
    assert data.provider_session_id is None
    assert row["provider_session_id"] is None
