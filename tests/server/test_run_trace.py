"""The run trace on the wire: span events fold into the snapshot's spans, and usage names its turn.

A real flow search runs (``vs_core``'s ``FlowRun``) and every step's span changes are emitted as
the run's commit observer emits them. The server must then hold, in its snapshot, exactly the
spans the runtime holds, and a turn's usage record must name the agent-turn span that ran as the
record's invocation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.support import build_server_parts

import vs_core.api as core
from server.events import EventType, TurnUsageData
from vibesys.events import (
    AgentOutputChunkData,
    CoreEventType,
    SpanEndedData,
    SpanStartedData,
    SpanUpdatedData,
    ToolCallData,
    ToolResultData,
)
from vibesys.events import TurnUsageData as CoreTurnUsageData
from vs_core.api.testing import FlowRun, Prompt, fake_search
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path
    from random import Random

    import pytest
    from tests.server.support import ServerParts

_EVENTS = {
    core.SpanChange.STARTED: (CoreEventType.SPAN_STARTED, SpanStartedData),
    core.SpanChange.UPDATED: (CoreEventType.SPAN_UPDATED, SpanUpdatedData),
    core.SpanChange.ENDED: (CoreEventType.SPAN_ENDED, SpanEndedData),
}


class Plan(core.Value):
    """What the toy agent answers."""

    n: int


class Config(core.Value):
    """The toy search's configuration."""

    turns: int = 1


ROLE = core.Role(
    name="worker",
    title="Worker",
    prompt="fake/worker.j2",
    reply=Plan,
    workspace=None,
    access=core.Access.READ_ONLY,
    turn_time=600.0,
    corrections=0,
    lost_turns=0,
    charge=core.Charge.TURN,
    exchanges=core.Exchanges(
        correction="fake/correction.j2", resume="fake/resume.j2", wait_unrecorded="fake/wait.j2"
    ),
)


@fake_search("trace", Config, (ROLE,))
async def search(ctx: core.Flow, config: Config) -> None:
    async def worker(ctx: core.Flow, number: int) -> None:
        await ctx.agents.session(ROLE).turn(Prompt(text=f"t{number}"), within=10.0)

    async with ctx.group() as tasks:
        for number in range(config.turns):
            tasks.start(f"w{number}", worker, number)
        async for _ in tasks:
            pass


def emit_changes(parts: ServerParts, before: core.RunTrace | None, after: core.RunTrace) -> None:
    """Emit what the commit observer emits for one committed step."""
    for change, span in core.span_changes(before, after):
        event_type, data = _EVENTS[change]
        parts.core_events.emit(event_type, data=data(span=span))


def run_to_end(parts: ServerParts, directory: Path, turns: int, order: Random) -> FlowRun:
    """Run the toy search to its end, emitting every step's span changes."""
    parts.integration.attach(directory, run_id="run-trace")
    run = FlowRun.start(search, Config(turns=turns))
    before: core.RunTrace | None = None
    for _ in range(4 * turns + 4):  # each turn is a render and a reply; bounded against hangs
        after = run.trace()
        emit_changes(parts, before, after)
        assert parts.api.snapshot().spans == list(after.spans)
        if run.finished is not None:
            break
        before = after
        asked = [a for a in run.open if isinstance(a.request, core.RequestTurn)]
        run.reply(order.choice(asked), Plan(n=1))
    return run


@settings(max_examples=examples(pr=4, nightly=30), deadline=None)
@given(turns=st.integers(1, 3), order=st.randoms(use_true_random=False))
def test_the_snapshot_holds_the_spans_the_runtime_holds_and_usage_names_its_turn(
    tmp_path_factory: pytest.TempPathFactory, turns: int, order: Random
) -> None:
    parts = build_server_parts()
    run = run_to_end(parts, tmp_path_factory.mktemp("run"), turns, order)
    assert run.finished is not None
    turn = next(span for span in run.trace().spans if span.invocation is not None)
    parts.core_events.emit(
        CoreEventType.TURN_USAGE_RECORDED,
        execution_id=turn.invocation,
        data=CoreTurnUsageData(input_tokens=3),
    )
    usage = [e.data for e in parts.journal.read() if e.type is EventType.TURN_USAGE_RECORDED]
    assert usage == [TurnUsageData(input_tokens=3, span_id=turn.id)]


@settings(max_examples=examples(pr=4, nightly=30), deadline=None)
@given(
    turns=st.integers(1, 3),
    order=st.randoms(use_true_random=False),
    spend=st.lists(st.integers(0, 10**6), min_size=3, max_size=3),
)
def test_each_turns_usage_is_attributed_to_its_own_span_and_the_spans_sum_to_the_run_total(
    tmp_path_factory: pytest.TempPathFactory, turns: int, order: Random, spend: list[int]
) -> None:
    parts = build_server_parts()
    run = run_to_end(parts, tmp_path_factory.mktemp("run"), turns, order)
    turn_spans = [span for span in run.trace().spans if span.invocation is not None]
    assert len(turn_spans) == turns
    for span, tokens in zip(turn_spans, spend, strict=False):
        parts.core_events.emit(
            CoreEventType.TURN_USAGE_RECORDED,
            execution_id=span.invocation,
            data=CoreTurnUsageData(
                input_tokens=tokens, output_tokens=tokens // 2, reasoning_output_tokens=tokens // 4
            ),
        )
    usage = [
        e.data
        for e in parts.journal.read()
        if e.type is EventType.TURN_USAGE_RECORDED and isinstance(e.data, TurnUsageData)
    ]
    by_span = {data.span_id: data for data in usage}
    assert set(by_span) == {span.id for span in turn_spans}
    for span, tokens in zip(turn_spans, spend, strict=False):
        data = by_span[span.id]
        assert (data.input_tokens, data.output_tokens, data.reasoning_output_tokens) == (
            tokens,
            tokens // 2,
            tokens // 4,
        )
    assert sum(data.input_tokens or 0 for data in usage) == sum(spend[:turns])


def _transcript_data(
    kind: int, number: int
) -> AgentOutputChunkData | ToolCallData | ToolResultData:
    if kind == 0:
        return AgentOutputChunkData(channel="assistant", content=f"words {number}")
    if kind == 1:
        return ToolCallData(tool="Read", call_id=f"c{number}", args={"path": "a.py"})
    return ToolResultData(tool="Read", call_id=f"c{number}", content="x")


_TRANSCRIPT_TYPES = {
    0: CoreEventType.AGENT_OUTPUT_CHUNK,
    1: CoreEventType.TOOL_CALL,
    2: CoreEventType.TOOL_RESULT,
}


@settings(max_examples=examples(pr=4, nightly=30), deadline=None)
@given(
    turns=st.integers(1, 3),
    order=st.randoms(use_true_random=False),
    kinds=st.lists(st.integers(0, 2), min_size=1, max_size=8),
)
def test_every_transcript_event_of_a_turn_names_the_turns_span(
    tmp_path_factory: pytest.TempPathFactory, turns: int, order: Random, kinds: list[int]
) -> None:
    """The server stamps a turn's output, tool calls and results with its span, nothing else."""
    parts = build_server_parts()
    run = run_to_end(parts, tmp_path_factory.mktemp("run"), turns, order)
    spans = [span for span in run.trace().spans if span.invocation is not None]
    assert len(spans) == turns
    for span in spans:
        for number, kind in enumerate(kinds):
            parts.core_events.emit(
                _TRANSCRIPT_TYPES[kind],
                execution_id=span.invocation,
                data=_transcript_data(kind, number),
            )
    parts.core_events.emit(
        CoreEventType.AGENT_OUTPUT_CHUNK,
        execution_id="no-such-invocation",
        data=AgentOutputChunkData(channel="assistant", content="stray"),
    )
    parts.core_events.emit(
        CoreEventType.AGENT_OUTPUT_CHUNK,
        data=AgentOutputChunkData(channel="assistant", content="no turn"),
    )

    by_invocation = {span.invocation: span.id for span in spans}
    transcript = [
        e
        for e in parts.journal.read()
        if e.type in {EventType.AGENT_OUTPUT_CHUNK, EventType.TOOL_CALL, EventType.TOOL_RESULT}
    ]
    assert len(transcript) == turns * len(kinds) + 2
    for event in transcript:
        assert event.span_id == by_invocation.get(event.execution_id)
    assert sum(event.span_id is not None for event in transcript) == turns * len(kinds)
