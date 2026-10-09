"""An operator steer reaches a running agent turn, or waits for the next boundary.

The run's steering queue is the single source of truth with two drain points:
the next invocation boundary (the prompt splice) and a turn in flight whose
client can take a message. The first group drives both through the public
runtime with a scripted client, so the fallback is covered as well as delivery;
the second checks the queue's accounting over generated sequences.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from tests.support.runtime_agent_sessions import (
    _client,
    _ClientFactory,
    _environment,
    _EnvironmentOpener,
    _runtime,
    _RuntimeEffects,
)

from vs_agent.api import SteerOutcome
from vs_runtime.api import AgentRole
from vs_runtime.api.infrastructure import (
    RunControlTransition,
    RunControlTransitionKind,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeAgentExecutionLifecycleSink, FakeRunControlEventSink

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.api.testing import FakeAgentClient

STEER = "|focus on latency"


def _run_two_turns(
    client: FakeAgentClient, *, steer_during_first_turn: str | None
) -> tuple[list[str], list[RunControlTransition]]:
    """Run turns "one" and "two"; queue *steer_during_first_turn* while "one" is in flight."""
    role = AgentRole(id="worker", system_prompt="Work.")
    sink = FakeRunControlEventSink()
    control = create_run_control_channel(sink)
    if steer_during_first_turn is not None:
        text = steer_during_first_turn
        client.on_invoke(
            lambda call: control.queue_steer(text) if call.user_prompt == "one" else None
        )

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(
                _ClientFactory(client),
                _EnvironmentOpener(_environment()),
                FakeAgentExecutionLifecycleSink(),
            ),
            control=control,
        )
        session = await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
        for prompt in ("one", "two"):
            await session.turn(prompt)
        await runtime.workspaces.close()

    asyncio.run(scenario())
    return [call.user_prompt for call in client.calls_for("worker")], sink.transitions


def _kinds(transitions: list[RunControlTransition]) -> list[RunControlTransitionKind]:
    return [transition.kind for transition in transitions]


def test_a_steer_queued_during_a_turn_is_delivered_into_it_and_not_again_at_the_boundary() -> None:
    client = _client(responses=("a", "b")).set_steering(SteerOutcome.DELIVERED)

    prompts, transitions = _run_two_turns(client, steer_during_first_turn=STEER)

    assert client.steers == [STEER]
    assert prompts == ["one", "two"]
    assert _kinds(transitions) == [
        RunControlTransitionKind.STEER_QUEUED,
        RunControlTransitionKind.STEER_DELIVERED,
    ]
    delivered = transitions[1]
    assert (delivered.text, delivered.agent_kind) == (STEER, "worker")
    assert delivered.execution_id is not None


def test_a_client_that_cannot_steer_leaves_the_message_for_the_next_boundary() -> None:
    client = _client(responses=("a", "b"))  # default: UNSUPPORTED, like a one-shot driver

    prompts, transitions = _run_two_turns(client, steer_during_first_turn=STEER)

    assert client.steers == []
    assert prompts == ["one", "two" + STEER]
    assert _kinds(transitions) == [
        RunControlTransitionKind.STEER_QUEUED,
        RunControlTransitionKind.STEER_CONSUMED,
    ]


def test_a_message_the_provider_refuses_after_accepting_it_is_delivered_at_the_boundary() -> None:
    client = _client(responses=("a", "b")).set_steering(
        SteerOutcome.DELIVERED, refuses_after_accepting=True
    )

    prompts, transitions = _run_two_turns(client, steer_during_first_turn=STEER)

    assert prompts == ["one", "two" + STEER]
    assert _kinds(transitions) == [
        RunControlTransitionKind.STEER_QUEUED,
        RunControlTransitionKind.STEER_DELIVERED,
        RunControlTransitionKind.STEER_QUEUED,  # queued again after the refusal
        RunControlTransitionKind.STEER_CONSUMED,
    ]


def test_a_steer_with_no_turn_in_flight_is_never_offered() -> None:
    client = _client(responses=("a", "b")).set_steering(SteerOutcome.DELIVERED)
    role = AgentRole(id="worker", system_prompt="Work.")
    control = create_run_control_channel(FakeRunControlEventSink())

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(
                _ClientFactory(client),
                _EnvironmentOpener(_environment()),
                FakeAgentExecutionLifecycleSink(),
            ),
            control=control,
        )
        session = await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
        control.queue_steer(STEER)  # between turns: nothing is running
        await session.turn("one")
        await runtime.workspaces.close()

    asyncio.run(scenario())

    assert client.steers == []
    assert [call.user_prompt for call in client.calls_for("worker")] == ["one" + STEER]


class _Behavior(StrEnum):
    TAKES = "takes"
    DECLINES = "declines"
    TAKES_THEN_REFUSES = "takes_then_refuses"


@dataclass
class _Target:
    """A turn in flight with a scripted provider, recording what it was offered."""

    behavior: _Behavior
    agent_kind: str = "worker"
    round_label: str = "round-1"
    execution_id: str | None = "execution-1"
    taken: list[str] = field(default_factory=list)
    refusals: list[Callable[[], None]] = field(default_factory=list)

    def offer_steer(self, text: str, on_rejected: Callable[[], None]) -> bool:
        if self.behavior is _Behavior.DECLINES:
            return False
        self.taken.append(text)
        if self.behavior is _Behavior.TAKES_THEN_REFUSES:

            def refuse() -> None:
                self.taken.remove(text)
                on_rejected()

            self.refusals.append(refuse)
        return True


_OPERATIONS = st.lists(
    st.one_of(
        st.just(("queue",)),
        st.tuples(st.just("attach"), st.sampled_from(_Behavior)),
        st.just(("detach",)),
        st.just(("refuse",)),
        st.just(("boundary",)),
    ),
    max_size=40,
)


@given(operations=_OPERATIONS)
def test_every_queued_message_is_held_by_exactly_one_place(
    operations: list[tuple[object, ...]],
) -> None:
    control = create_run_control_channel(FakeRunControlEventSink())
    targets: list[tuple[_Target, Callable[[], None]]] = []
    every_target: list[_Target] = []
    queued: list[str] = []
    drained: list[str] = []

    for operation in operations:
        match operation:
            case ("queue",):
                queued.append(f"steer-{len(queued)}")
                control.queue_steer(queued[-1])
            case ("attach", behavior):
                target = _Target(_Behavior(str(behavior)))
                every_target.append(target)
                targets.append((target, control.attach_steer_target(target)))
            case ("detach",):
                if targets:
                    targets.pop(0)[1]()
            case ("refuse",):
                for target, _detach in targets:
                    if target.refusals:
                        target.refusals.pop(0)()
                        break
            case ("boundary",):
                drained.extend(control.take_pending_steer())
    drained.extend(control.take_pending_steer())

    held = [text for target in every_target for text in target.taken]
    assert sorted(drained + held) == sorted(queued)
