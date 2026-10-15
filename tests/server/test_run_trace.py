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
    CoreEventType,
    SpanEndedData,
    SpanStartedData,
    SpanUpdatedData,
)
from vibesys.events import TurnUsageData as CoreTurnUsageData
from vs_core.api.testing import FlowRun, Prompt, fake_search
from vs_sim.api.testing import examples

if TYPE_CHECKING:
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


@settings(max_examples=examples(pr=4, nightly=30), deadline=None)
@given(turns=st.integers(1, 3), order=st.randoms(use_true_random=False))
def test_the_snapshot_holds_the_spans_the_runtime_holds_and_usage_names_its_turn(
    tmp_path_factory: pytest.TempPathFactory, turns: int, order: Random
) -> None:
    parts = build_server_parts()
    parts.integration.attach(tmp_path_factory.mktemp("run"), run_id="run-trace")
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

    assert run.finished is not None
    turn = next(span for span in run.trace().spans if span.invocation is not None)
    parts.core_events.emit(
        CoreEventType.TURN_USAGE_RECORDED,
        execution_id=turn.invocation,
        data=CoreTurnUsageData(input_tokens=3),
    )
    usage = [e.data for e in parts.journal.read() if e.type is EventType.TURN_USAGE_RECORDED]
    assert usage == [TurnUsageData(input_tokens=3, span_id=turn.id)]
