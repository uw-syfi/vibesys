"""The run's steering queue holds every queued message in exactly one place.

The queue is the single source of truth with two drain points: the next invocation boundary
(the prompt splice) and a turn in flight whose client can take a message. This checks the
queue's accounting over generated sequences of queueing, attaching, refusing, and draining.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api.infrastructure import (
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeRunControlEventSink

if TYPE_CHECKING:
    from collections.abc import Callable


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
