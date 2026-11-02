"""A ``FlowState`` survives the envelope codec: typed replies and operation results included."""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

import vs_core.api as core
from vs_core.api.testing import FAKE_REGISTRY

# test-isolation: the internal journal union must not drift from `Event`
from vs_core.flows import JournaledEvent

# test-isolation: the codec cache is the unit under test
from vs_core.flows._defs import FlowDef, snapshot_adapter

from .flow_support import Config, Plan, answer_of, ask, start

if TYPE_CHECKING:
    from vs_core.api.testing import FlowRun


class Note(core.Value):
    """The record the toy flow publishes."""

    plan: int


async def ask_twice(ctx: core.Flow, _config: Config) -> None:
    plan = answer_of(await ask(ctx, "first"))
    ctx.publish(Note(plan=plan))
    await ask(ctx, "second")


def _through_the_envelope(state: core.FlowState) -> core.FlowState:
    core_state = core.initial_state()
    envelope = core.RunEnvelope[core.FlowState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        strategy_id=core_state.run.declaration.strategy_id,
        state_schema=core_state.run.declaration.state_schema,
        core=core_state,
        strategy=state,
        event_cursor=core.EventCursor(sequence=0),
    )
    text = FAKE_REGISTRY.encode_envelope(envelope)
    return FAKE_REGISTRY.decode_envelope(core.RunEnvelope[core.FlowState], text).strategy


def _asked_once(run: FlowRun[Config]) -> None:
    run.reply(run.expect_turn("first"), Plan(n=4))


def test_a_flow_state_with_a_typed_reply_and_operation_results_round_trips_the_envelope() -> None:
    run = start(ask_twice)
    _asked_once(run)
    run.reply(run.expect_turn("second"), Plan(n=5))
    assert run.finished is not None

    restored = _through_the_envelope(run.state)
    assert restored == run.state
    assert run.driver.published(restored, Note) == Note(plan=4)
    kinds = {step.recorded.kind for step in restored.journal if step.recorded is not None}
    assert kinds == {"recorded_operation"}  # the prompt renders are the operations of the run
    assert any(
        step.event is not None and step.event.kind == "turn_ended" for step in restored.journal
    )


def test_a_search_resumes_from_the_state_read_back_from_the_envelope() -> None:
    run = start(ask_twice)
    _asked_once(run)
    second = run.expect_turn("second")

    run.state = _through_the_envelope(run.state)
    run.driver = run.driver.fresh()
    run.reply(second, Plan(n=5))
    assert run.finished is not None
    assert run.driver.published(run.state, Note) == Note(plan=4)


def test_the_journal_holds_every_event_as_it_is_but_operation_results() -> None:
    def members(alias: object) -> set[type]:
        annotated = alias.__value__  # ty: ignore[unresolved-attribute]
        return set(get_args(get_args(annotated)[0]))

    assert members(JournaledEvent) == members(core.Event) - {core.OperationFinished}


async def _flow_body(_ctx: core.Flow, _config: Config) -> None:
    return None


def test_the_snapshot_codec_is_shared_by_every_definition_of_one_snapshot_type() -> None:
    """A run builds its own definition, so a codec cached per definition is kept for every run."""
    first = FlowDef("a", _flow_body, Plan)
    second = FlowDef("b", _flow_body, Plan)

    assert snapshot_adapter(first) is snapshot_adapter(second)
