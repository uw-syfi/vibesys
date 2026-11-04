"""A turn's per-model usage, cost basis and provider session reach the run journal unchanged.

The library prices each model a turn ran (a Claude turn can run several: Task subagents and
advisors have their own) and says how it reached the turn's cost. ``usage_from`` is the one seam
that carries those facts into the journal, so these tests drive a whole turn through
``AgentClient`` and read the journal event and the ``usage.jsonl`` row it wrote. Nothing here
prices anything: the figures come from the library and must come out as they went in.
"""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import agentshim
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vibesys.api.importing import ImportSource, SourceFormat, import_run_facts
from vibesys.events import CoreEventType, ModelTurnUsageData, TurnUsageData
from vibesys.run import CoreAgentEventSink, EventJournal
from vs_agent.api import (
    SHIPPED_PROVIDERS,
    AgentClient,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    CostBasis,
    SessionScope,
    UsageSpeed,
)
from vs_agent.api.testing import (
    FakeExecutor,
    FakeProvider,
    TokenUsage,
    assistant_text,
    fake_agentshim_launcher,
    scripted_turn,
    stream_peers,
)
from vs_core.api.testing import FAKE_REGISTRY
from vs_sim.api.testing import ManualClock, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Sequence

_MODELS = (
    "claude-opus-5-5",
    "claude-opus-5-5[1m]",
    "claude-haiku-5-5",
    "claude-sonnet-5-5",
    "gpt-6.1-sol",
    "vendor/odd-model",
    "mystery-model",
)


def _spec(workspace: Path) -> AgentSessionSpec:
    return AgentSessionSpec(
        role="implementer",
        provider="fake",
        workspace=workspace,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )


def _run_turn(
    root: Path, report: agentshim.UsageReport
) -> tuple[TurnUsageData, str | None, dict[str, object]]:
    """Run one scripted turn; its journal event, the session it returned, and its usage row."""
    journal = EventJournal(clock=ManualClock(), threads=SimThreads())
    journal.attach(root / "run", "run-1")
    logs = root / "logs"
    logs.mkdir()
    client = AgentClient(
        FakeProvider(turn=[assistant_text("done"), report], threads=SimThreads()),
        provider="fake",
        event_sink=CoreAgentEventSink(journal.record, clock=ManualClock()),
        log_dir=logs,
        clock=ManualClock(),
        threads=SimThreads(),
    )
    try:
        result = client.run(
            session_spec=_spec(root),
            turn=AgentTurnRequest(message="work", invocation_id="inv-1", label="round 1"),
            session_key=AgentSessionKey(SessionScope.ROLE, "implementer"),
        )
    finally:
        client.close()
    (event,) = (e for e in journal.read() if e.type is CoreEventType.TURN_USAGE_RECORDED)
    assert isinstance(event.data, TurnUsageData)
    row = json.loads((logs / "usage.jsonl").read_text(encoding="utf-8"))
    return event.data, result.provider_session_id, row


@st.composite
def _row_tokens(draw: st.DrawFn, *, turns: int) -> agentshim.TokenUsage:
    """One model's tokens under the library's invariants: cache within input, reasoning within output."""
    input_tokens = draw(st.integers(0, 10**7))
    output_tokens = draw(st.integers(0, 10**6))
    cache_read = draw(st.integers(0, input_tokens))
    cache_write = draw(st.integers(0, input_tokens - cache_read))
    return agentshim.TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        cache_write_1h_input_tokens=draw(st.integers(0, cache_write)),
        reasoning_output_tokens=draw(st.integers(0, output_tokens)),
        turns=turns,
    )


@st.composite
def _split_turns(draw: st.DrawFn) -> agentshim.ProviderUsage:
    """A turn the library split by model: one primary, the turn's requests on it alone.

    Priced rows carry the library's figure and the turn's cost is their sum when every row is
    priced; one unpriced row leaves the turn without a cost, as the library does.
    """
    models = draw(st.lists(st.sampled_from(_MODELS), min_size=1, max_size=4, unique=True))
    primary = draw(st.integers(0, len(models) - 1))
    turns = draw(st.integers(1, 40))
    speed = draw(st.none() | st.sampled_from(agentshim.UsageSpeed))
    rows: list[agentshim.ModelUsage] = []
    for index, model in enumerate(models):
        is_primary = index == primary
        priced = draw(st.booleans())
        rows.append(
            agentshim.ModelUsage(
                model=model,
                tokens=draw(_row_tokens(turns=turns if is_primary else 0)),
                recorded_usd=(
                    draw(st.floats(0, 1000, allow_nan=False, allow_infinity=False))
                    if priced
                    else None
                ),
                speed=speed if is_primary else None,
                primary=is_primary,
            )
        )
    total: float | None = 0.0
    for row in rows:
        total = None if total is None or row.recorded_usd is None else total + row.recorded_usd
    return agentshim.ProviderUsage(
        tokens=rows[primary].tokens,
        total_cost_usd=total,
        provider="claude",
        cost_source=(
            agentshim.CostSource.PRICE_TABLE
            if total is not None
            else agentshim.CostSource.UNKNOWN_MODEL
        ),
        by_model=tuple(rows),
    )


_NO_COST = {agentshim.CostSource.UNKNOWN_MODEL, agentshim.CostSource.NOT_REPORTED}
"""The sources that go with no cost: nothing was priced, and nothing was guessed."""


def _report(usage: agentshim.ProviderUsage) -> agentshim.UsageReport:
    return agentshim.UsageReport(usage=usage, cost_usd=usage.total_cost_usd)


@settings(max_examples=examples(pr=25, nightly=300), deadline=None)
@given(usage=_split_turns())
def test_every_model_a_turn_ran_is_journaled_once_with_one_primary_and_the_turns_sum_to_its_cost(
    usage: agentshim.ProviderUsage,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        data, _, _ = _run_turn(Path(directory), _report(usage))

    rows = data.by_model
    assert [row.model for row in rows] == [row.model for row in usage.by_model]
    assert sum(row.primary for row in rows) == 1
    for row, given_row in zip(rows, usage.by_model, strict=True):
        tokens = given_row.tokens
        assert (
            row.input_tokens,
            row.cache_read_input_tokens,
            row.cache_creation_input_tokens,
            row.cache_creation_1h_input_tokens,
            row.output_tokens,
            row.reasoning_output_tokens,
            row.turns,
            row.recorded_usd,
            row.primary,
        ) == (
            tokens.input_tokens,
            tokens.cache_read_input_tokens,
            tokens.cache_write_input_tokens,
            tokens.cache_write_1h_input_tokens,
            tokens.output_tokens,
            tokens.reasoning_output_tokens,
            tokens.turns,
            given_row.recorded_usd,
            given_row.primary,
        )
    # The rows sum to the turn's cost when every one is priced, and the turn has none when not.
    figures = [row.recorded_usd for row in rows]
    if None in figures:
        assert data.total_cost_usd is None
        assert data.cost_basis is CostBasis.UNKNOWN_MODEL
    else:
        assert data.total_cost_usd == usage.total_cost_usd
        assert math.isclose(
            math.fsum(f for f in figures if f is not None), data.total_cost_usd or 0
        )
        assert data.cost_basis is CostBasis.PRICE_TABLE
    # The turn's requests are counted once, on the primary row, however many models ran.
    assert sum(row.turns for row in rows) == usage.tokens.turns
    assert [row.turns for row in rows if not row.primary] == [0] * (len(rows) - 1)


@settings(max_examples=examples(pr=25, nightly=300), deadline=None)
@given(usage=_split_turns())
def test_the_main_conversations_tokens_stay_as_they_were_and_the_rows_are_a_superset(
    usage: agentshim.ProviderUsage,
) -> None:
    """``tokens`` feeds the store and replay, so a split must not move them."""
    with tempfile.TemporaryDirectory() as directory:
        data, _, _ = _run_turn(Path(directory), _report(usage))

    tokens = usage.tokens
    assert (
        data.input_tokens,
        data.cache_read_input_tokens,
        data.cache_creation_input_tokens,
        data.output_tokens,
        data.reasoning_output_tokens,
    ) == (
        tokens.input_tokens,
        tokens.cache_read_input_tokens,
        tokens.cache_write_input_tokens,
        tokens.output_tokens,
        tokens.reasoning_output_tokens,
    )
    assert data.input_tokens is not None
    assert sum(row.input_tokens for row in data.by_model) >= data.input_tokens


@pytest.mark.parametrize("source", list(agentshim.CostSource), ids=lambda each: each.value)
def test_every_way_the_library_reaches_a_cost_has_a_basis_of_the_same_name(
    tmp_path: Path, source: agentshim.CostSource
) -> None:
    usage = agentshim.ProviderUsage(
        tokens=agentshim.TokenUsage(input_tokens=5, output_tokens=2, turns=1),
        total_cost_usd=None if source in _NO_COST else 0.5,
        provider="claude",
        cost_source=source,
    )

    data, _, row = _run_turn(tmp_path, _report(usage))

    assert data.cost_basis == CostBasis(source.value)
    assert row["cost_basis"] == source.value


@pytest.mark.parametrize("speed", list(agentshim.UsageSpeed), ids=lambda each: each.value)
def test_every_speed_the_library_reports_is_journaled_by_name(
    tmp_path: Path, speed: agentshim.UsageSpeed
) -> None:
    tokens = agentshim.TokenUsage(input_tokens=5, output_tokens=2, turns=1)
    row = agentshim.ModelUsage("claude-opus-5-5", tokens, 0.5, speed, primary=True)
    usage = agentshim.ProviderUsage(
        tokens=tokens,
        total_cost_usd=0.5,
        provider="claude",
        cost_source=agentshim.CostSource.PRICE_TABLE,
        by_model=(row,),
    )

    data, _, _ = _run_turn(tmp_path, _report(usage))

    assert [each.speed for each in data.by_model] == [UsageSpeed(speed.value)]


def test_a_provider_that_reports_no_split_leaves_none_and_a_provider_figure_keeps_its_basis(
    tmp_path: Path,
) -> None:
    usage = agentshim.ProviderUsage(
        tokens=agentshim.TokenUsage(input_tokens=5, output_tokens=2, turns=1),
        total_cost_usd=0.5,
        provider="claude",
        cost_source=agentshim.CostSource.PROVIDER_REPORTED,
    )

    data, _, _ = _run_turn(tmp_path, _report(usage))

    assert data.by_model == ()
    assert data.cost_basis is CostBasis.PROVIDER_REPORTED
    assert data.total_cost_usd == 0.5


def test_a_turn_names_the_provider_conversation_it_ran_in_on_the_event_and_the_usage_row(
    tmp_path: Path,
) -> None:
    usage = agentshim.ProviderUsage(
        tokens=agentshim.TokenUsage(input_tokens=5, output_tokens=2, turns=1),
        provider="claude",
    )

    data, session_id, row = _run_turn(tmp_path, _report(usage))

    assert session_id is not None
    assert data.provider_session_id == session_id
    assert row["provider_session_id"] == session_id


@example(rows=[])
@given(rows=st.lists(st.sampled_from(_MODELS), max_size=3, unique=True))
@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
def test_the_usage_row_holds_the_split_in_the_journals_own_names(rows: Sequence[str]) -> None:
    """``usage.jsonl`` and the journal write one vocabulary, so a reader of either joins them."""
    tokens = agentshim.TokenUsage(input_tokens=9, output_tokens=3, turns=2)
    split = tuple(
        agentshim.ModelUsage(
            model,
            # Only the primary row counts the turn's requests, as the library builds them.
            agentshim.TokenUsage(input_tokens=9, output_tokens=3, turns=2 if index == 0 else 0),
            0.25,
            None,
            primary=index == 0,
        )
        for index, model in enumerate(rows)
    )
    usage = agentshim.ProviderUsage(
        tokens=tokens,
        total_cost_usd=0.25 * len(rows) if rows else None,
        provider="claude",
        cost_source=agentshim.CostSource.PRICE_TABLE if rows else None,
        by_model=split,
    )
    with tempfile.TemporaryDirectory() as directory:
        data, _, row = _run_turn(Path(directory), _report(usage))

    written = row["by_model"]
    assert isinstance(written, list)
    assert [item["model"] for item in written] == [each.model for each in data.by_model]
    for item, each in zip(written, data.by_model, strict=True):
        # The row names exactly the journal row's fields, in its names, with its values.
        assert item == each.model_dump(mode="json")
        assert set(item) == set(ModelTurnUsageData.model_fields)


def test_the_usage_row_names_the_turns_fields_and_nothing_else(tmp_path: Path) -> None:
    """A reader of ``usage.jsonl`` joins it to the journal by these keys, so they are pinned."""
    usage = agentshim.ProviderUsage(
        tokens=agentshim.TokenUsage(input_tokens=9, output_tokens=3, turns=2),
        total_cost_usd=0.5,
        provider="claude",
        cost_source=agentshim.CostSource.PROVIDER_REPORTED,
    )

    data, session_id, row = _run_turn(tmp_path, _report(usage))

    accounting = {
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_cost_usd",
        "duration_ms",
    }
    assert set(row) == {
        "timestamp",
        "kind",
        "round_label",
        "provider",
        "model",
        "reasoning_effort",
        *accounting,
        "cost_basis",
        "by_model",
        "provider_session_id",
        "skill_uses",
        "skills_invoked",
        "skills_offered",
    }
    # The accounting keys are the journal event's own, by the same names.
    assert accounting | {"cost_basis", "by_model", "provider_session_id"} <= set(
        TurnUsageData.model_fields
    )
    assert data.cost_basis is CostBasis.PROVIDER_REPORTED
    assert row["cost_basis"] == "provider_reported"
    assert row["provider_session_id"] == session_id


@settings(max_examples=examples(pr=30, nightly=300), deadline=None)
@given(
    cost=st.none() | st.floats(0, 1000, allow_nan=False, allow_infinity=False),
    source=st.none() | st.sampled_from(agentshim.CostSource),
)
def test_a_turn_the_client_records_always_records_a_basis_so_null_means_only_an_old_line(
    cost: float | None, source: agentshim.CostSource | None
) -> None:
    """Whatever the library said of a turn's cost, even nothing, the new line says something."""
    usage = agentshim.ProviderUsage(
        tokens=agentshim.TokenUsage(input_tokens=5, output_tokens=2, turns=1),
        total_cost_usd=cost,
        provider="claude",
        cost_source=source,
    )
    with tempfile.TemporaryDirectory() as directory:
        data, _, row = _run_turn(Path(directory), _report(usage))

    assert data.cost_basis is not None
    assert row["cost_basis"] == data.cost_basis.value
    if source is None and cost is None:
        assert data.cost_basis is CostBasis.NOT_REPORTED


@pytest.mark.parametrize("provider", SHIPPED_PROVIDERS)
def test_a_turn_of_every_shipped_provider_records_a_basis(tmp_path: Path, provider: str) -> None:
    """Each shipped CLI's own stream shape, through the production session path, with no cost
    printed: the line says ``not_reported`` (or the basis the library computed), never nothing."""
    if provider == "codex":
        data = _codex_turn(tmp_path, "gpt-6.1-sol")
    else:
        data, _, _ = _one_shot_turn(tmp_path, provider)

    assert data.cost_basis is not None


def _one_shot_turn(
    root: Path, provider: str
) -> tuple[TurnUsageData, str | None, dict[str, object]]:
    """One turn of a one-shot CLI, scripted in its own stream format, through ``AgentClient``."""
    journal = EventJournal(clock=ManualClock(), threads=SimThreads())
    journal.attach(root / "run", "run-1")
    logs = root / "logs"
    logs.mkdir()
    run = scripted_turn(
        provider,
        text="done",
        session_id="session-1",
        usage=TokenUsage(input_tokens=100, cache_read_input_tokens=40, output_tokens=10),
    )
    client = AgentClient(
        fake_agentshim_launcher(
            provider=provider, executor=FakeExecutor(run), threads=SimThreads()
        ),
        provider=provider,
        event_sink=CoreAgentEventSink(journal.record, clock=ManualClock()),
        log_dir=logs,
        clock=ManualClock(),
        threads=SimThreads(),
    )
    spec = AgentSessionSpec(
        role="implementer",
        provider=provider,
        workspace=root,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    try:
        result = client.run(
            session_spec=spec,
            turn=AgentTurnRequest(message="work", invocation_id="inv-1", label="round 1"),
            session_key=AgentSessionKey(SessionScope.ROLE, "implementer"),
        )
    finally:
        client.close()
    (event,) = (e for e in journal.read() if e.type is CoreEventType.TURN_USAGE_RECORDED)
    assert isinstance(event.data, TurnUsageData)
    row = json.loads((logs / "usage.jsonl").read_text(encoding="utf-8"))
    return event.data, result.provider_session_id, row


def _codex_turn(root: Path, model: str) -> TurnUsageData:
    """One Codex turn on ``model`` over the app-server transport, against a scripted peer.

    The transport is where agentshim prices a Codex turn, so this is the production path of a
    paid turn with no process, container or network.
    """
    journal = EventJournal(clock=ManualClock(), threads=SimThreads())
    journal.attach(root / "run", "run-1")
    launcher = fake_agentshim_launcher(
        provider="codex",
        executor=FakeExecutor([], peers=stream_peers("codex", "done").build),
        threads=SimThreads(),
        transport=agentshim.TransportKind.STREAM,
    )
    client = AgentClient(
        launcher,
        provider="codex",
        event_sink=CoreAgentEventSink(journal.record, clock=ManualClock()),
        clock=ManualClock(),
        threads=SimThreads(),
    )
    spec = AgentSessionSpec(
        role="implementer",
        provider="codex",
        model=model,
        workspace=root,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    try:
        client.run(
            session_spec=spec,
            turn=AgentTurnRequest(message="work", invocation_id="inv-1", label="round 1"),
            session_key=AgentSessionKey(SessionScope.ROLE, "implementer"),
        )
    finally:
        client.close()
    (event,) = (e for e in journal.read() if e.type is CoreEventType.TURN_USAGE_RECORDED)
    assert isinstance(event.data, TurnUsageData)
    return event.data


def _replay_money(data: TurnUsageData) -> object:
    """What replay accounting reads as the turn's money: an amount, or ``unknown``."""
    line = {
        "type": "turn_usage_recorded",
        "sequence": 1,
        "run_id": "run-1",
        "timestamp": "2026-10-31T00:00:00Z",
        "execution_id": "inv-1",
        "data": data.model_dump(mode="json"),
    }
    source = ImportSource("core-events.jsonl", SourceFormat.EVENTS, json.dumps(line).encode())
    facts = import_run_facts("run-1", (source,), strategy="single", registry=FAKE_REGISTRY)
    (money,) = (
        fact.value.value.quantity
        for fact in facts.facts
        if fact.value is not None
        and fact.value.field == "usage"
        and fact.value.value.unit == "total_cost_usd"
    )
    assert money is not None
    return money.value if money.field != "unknown" else "unknown"


def test_a_gpt_6_1_sol_turn_records_a_price_table_cost_that_replay_reads_as_an_amount(
    tmp_path: Path,
) -> None:
    """Regression: a turn on ``gpt-6.1-sol`` recorded no cost, so replay money read ``unknown``."""
    data = _codex_turn(tmp_path, "gpt-6.1-sol")

    (row,) = data.by_model
    assert row.model == "gpt-6.1-sol"
    assert row.primary
    assert data.cost_basis is CostBasis.PRICE_TABLE
    assert data.total_cost_usd is not None
    assert data.total_cost_usd > 0
    assert row.recorded_usd == data.total_cost_usd
    assert _replay_money(data) == data.total_cost_usd
    assert data.provider_session_id == "thread-1"


def test_a_turn_on_a_model_nobody_priced_reads_unknown_never_a_guess(tmp_path: Path) -> None:
    data = _codex_turn(tmp_path, "gpt-0-unpriced")

    assert data.total_cost_usd is None
    assert data.cost_basis is CostBasis.UNKNOWN_MODEL
    assert [row.recorded_usd for row in data.by_model] == [None]
    assert _replay_money(data) == "unknown"
