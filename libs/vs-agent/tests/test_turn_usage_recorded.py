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

from typing import TYPE_CHECKING

import pytest

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
    from pathlib import Path

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
