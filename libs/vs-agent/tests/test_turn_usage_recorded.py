"""Settled per-turn token/cost accounting reaches the run as a typed event.

``AgentClient._write_usage_record`` already appends the usage-record JSONL
audit row for every finished turn, success or failure (see
``test_fake_client.py``'s ``test_every_dispatched_turn_records_one_usage_row_even_when_it_fails``
for the audit-row half of that contract). It now also publishes
``CoreEventType.TURN_USAGE_RECORDED`` through the injected event sink, so a
live run's settled per-turn accounting is a typed event, not only a local
file.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.events import CoreEventType, TurnUsageData
from vibesys.run import CoreAgentEventSink, EventJournal
from vs_agent.api import (
    AgentClient,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    SessionResumeError,
    SessionScope,
)
from vs_agent.api.testing import FakeProvider, assistant_text, usage
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from collections.abc import Sequence

    import agentshim


def _client_and_journal(
    tmp_path: Path, *, turn: Sequence[agentshim.AgentEvent]
) -> tuple[AgentClient, EventJournal]:
    journal = EventJournal(clock=ManualClock(), threads=SimThreads())
    journal.attach(tmp_path / "run", "run-1")
    client = AgentClient(
        FakeProvider(turn=turn, threads=SimThreads()),
        provider="fake",
        event_sink=CoreAgentEventSink(journal.record, clock=ManualClock()),
        clock=ManualClock(),
        threads=SimThreads(),
    )
    return client, journal


def _spec(tmp_path: Path) -> AgentSessionSpec:
    return AgentSessionSpec(
        role="implementer",
        provider="fake",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )


def test_a_finished_turn_publishes_its_settled_usage_with_routing_metadata(
    tmp_path: Path,
) -> None:
    client, journal = _client_and_journal(
        tmp_path, turn=[assistant_text("done"), usage(input_tokens=11, output_tokens=4)]
    )
    turn = AgentTurnRequest(message="work", invocation_id="inv-1", label="round 1")
    try:
        client.run(
            session_spec=_spec(tmp_path),
            turn=turn,
            session_key=AgentSessionKey(SessionScope.ROLE, "implementer"),
        )
    finally:
        client.close()

    events = journal.read()
    usage_event = next(event for event in events if event.type is CoreEventType.TURN_USAGE_RECORDED)
    assert usage_event.agent_kind == "implementer"
    assert usage_event.round_label == "round 1"
    assert usage_event.execution_id == "inv-1"
    assert isinstance(usage_event.data, TurnUsageData)
    assert usage_event.data.input_tokens == 11
    assert usage_event.data.output_tokens == 4
    assert usage_event.data.total_cost_usd == 0.0
    assert usage_event.data.duration_ms == 10


def test_a_turn_with_no_scripted_usage_report_still_publishes_an_unmeasured_event(
    tmp_path: Path,
) -> None:
    client, journal = _client_and_journal(tmp_path, turn=[assistant_text("done")])
    turn = AgentTurnRequest(message="work", invocation_id="inv-2", label="round 2")
    try:
        client.run(
            session_spec=_spec(tmp_path),
            turn=turn,
            session_key=AgentSessionKey(SessionScope.ROLE, "implementer"),
        )
    finally:
        client.close()

    events = journal.read()
    usage_event = next(event for event in events if event.type is CoreEventType.TURN_USAGE_RECORDED)
    assert isinstance(usage_event.data, TurnUsageData)
    assert usage_event.data.input_tokens is None
    assert usage_event.data.output_tokens is None


def test_a_turn_that_fails_before_dispatch_still_publishes_a_settled_usage_record(
    tmp_path: Path,
) -> None:
    """Mirrors the fake client's audit-row guarantee: usage is recorded even on failure.

    ``ROLE``-scoped keys are never durable (see ``session_key.py``), so a
    strict-continuation request against one fails inside ``_run`` before any
    provider dispatch -- the cheapest reliable way to force the ``finally``
    branch of ``AgentClient._logged_turn`` without a real provider failure.
    """
    client, journal = _client_and_journal(tmp_path, turn=[assistant_text("done")])
    turn = AgentTurnRequest(
        message="work",
        invocation_id="inv-3",
        label="round 3",
        expected_provider_session_id="some-checkpoint",
    )
    try:
        with pytest.raises(SessionResumeError, match="durable session key"):
            client.run(
                session_spec=_spec(tmp_path),
                turn=turn,
                session_key=AgentSessionKey(SessionScope.ROLE, "implementer"),
            )
    finally:
        client.close()

    events = journal.read()
    usage_events = [event for event in events if event.type is CoreEventType.TURN_USAGE_RECORDED]
    assert len(usage_events) == 1
    assert usage_events[0].round_label == "round 3"
    assert isinstance(usage_events[0].data, TurnUsageData)
    assert usage_events[0].data.input_tokens is None


def _run_turn(tmp_path: Path, *, invocation: str, report: agentshim.AgentEvent) -> TurnUsageData:
    client, journal = _client_and_journal(tmp_path, turn=[assistant_text("done"), report])
    turn = AgentTurnRequest(message="work", invocation_id=invocation, label="round 1")
    try:
        client.run(
            session_spec=_spec(tmp_path),
            turn=turn,
            session_key=AgentSessionKey(SessionScope.ROLE, "implementer"),
        )
    finally:
        client.close()
    (event,) = (e for e in journal.read() if e.type is CoreEventType.TURN_USAGE_RECORDED)
    assert event.execution_id == invocation
    assert isinstance(event.data, TurnUsageData)
    return event.data


@st.composite
def _turn_tokens(draw: st.DrawFn) -> dict[str, int]:
    """One turn's reading under the library's invariants: cache within input, reasoning within output."""
    input_tokens = draw(st.integers(0, 10**7))
    output_tokens = draw(st.integers(0, 10**6))
    cache_read = draw(st.integers(0, input_tokens))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_write_input_tokens": draw(st.integers(0, input_tokens - cache_read)),
        "reasoning_output_tokens": draw(st.integers(0, output_tokens)),
    }


@settings(max_examples=25, deadline=None)
@given(st.lists(_turn_tokens(), min_size=1, max_size=5))
def test_each_turn_event_carries_that_turns_own_usage_so_the_events_sum_to_the_run_total(
    readings: list[dict[str, int]],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        recorded = [
            _run_turn(
                Path(directory) / f"turn-{index}",
                invocation=f"inv-{index}",
                report=usage(**reading),
            )
            for index, reading in enumerate(readings)
        ]
    assert [
        {
            "input_tokens": data.input_tokens,
            "output_tokens": data.output_tokens,
            "cache_read_input_tokens": data.cache_read_input_tokens,
            "cache_write_input_tokens": data.cache_creation_input_tokens,
            "reasoning_output_tokens": data.reasoning_output_tokens,
        }
        for data in recorded
    ] == readings
    for field in ("input_tokens", "output_tokens", "reasoning_output_tokens"):
        assert sum(getattr(data, field) for data in recorded) == sum(r[field] for r in readings)


def test_a_turn_without_a_usage_report_leaves_every_field_absent_not_zero(tmp_path: Path) -> None:
    client, journal = _client_and_journal(tmp_path, turn=[assistant_text("done")])
    try:
        client.run(
            session_spec=_spec(tmp_path),
            turn=AgentTurnRequest(message="work", invocation_id="inv-9", label="round 1"),
            session_key=AgentSessionKey(SessionScope.ROLE, "implementer"),
        )
    finally:
        client.close()
    (event,) = (e for e in journal.read() if e.type is CoreEventType.TURN_USAGE_RECORDED)
    assert isinstance(event.data, TurnUsageData)
    assert (
        event.data.input_tokens,
        event.data.cache_creation_input_tokens,
        event.data.cache_read_input_tokens,
        event.data.output_tokens,
        event.data.reasoning_output_tokens,
    ) == (None, None, None, None, None)
