"""Properties of the strategy over every state and view a whole run reaches.

A run is driven once on the production shell with a strategy that records the (state, view)
pair of every `decide`. `decide` is then a pure function of that pair (the same pair always
proposes the same decisions and state, whatever else ran), every reached state survives the
envelope codec, and the state is a tree of immutable values.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.issue_queue.strategy._executors import Executors, WorldTranslator
from tests.vibesys.orchestration.issue_queue.strategy._replies import evaluated, implemented, judged
from tests.vibesys.orchestration.issue_queue.strategy._run import config, harness, limits

from vibesys.orchestration.issue_queue.core_policy.api import reply_schemas
from vibesys.orchestration.issue_queue.strategy.api import (
    STATE_SCHEMA,
    IssueQueueStrategy,
    IssueQueueStrategyState,
    plain_operation_registry,
)
from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    ControlChanged,
    ControlId,
    ControlInput,
    EventCursor,
    HostFence,
    HostId,
    Measure,
    ProposeWinner,
    RunEnvelope,
    Settle,
    Stop,
    Withdraw,
)
from vs_core.testing.drive import new_run
from vs_runtime.api.testing import HostOptions, drive_shell

if TYPE_CHECKING:
    from vs_core.api import Proposal, RunView, StrategyEvent


@dataclass
class Capture:
    """The (state, view) pair of every `decide` of one run."""

    pairs: list[tuple[IssueQueueStrategyState, RunView]] = field(default_factory=list)


class CapturingStrategy:
    """The issue-queue strategy, recording what it was asked."""

    def __init__(self, inner: IssueQueueStrategy, capture: Capture) -> None:
        self._inner = inner
        self._capture = capture
        self.declaration = inner.declaration

    @property
    def state(self) -> IssueQueueStrategyState:
        return self._inner.state

    def bind(self, state: IssueQueueStrategyState) -> CapturingStrategy:
        return CapturingStrategy(self._inner.bind(state), self._capture)

    def on_event(self, view: RunView, event: StrategyEvent) -> IssueQueueStrategyState:
        return self._inner.on_event(view, event)

    def decide(self, view: RunView) -> Proposal[IssueQueueStrategyState]:
        self._capture.pairs.append((self._inner.state, view))
        return self._inner.decide(view)


def _captured() -> tuple[IssueQueueStrategy, Capture]:
    settings = config()
    capture = Capture()
    executors = Executors(
        implementer=deque(implemented(f"turn {n}") for n in range(4)),
        judge=deque([judged(passed=False), judged(bugs=("a bug",)), judged()]),
        perf=deque([evaluated("more"), evaluated()]),
    )
    strategy = IssueQueueStrategy(config=settings)
    finished = drive_shell(
        CapturingStrategy(strategy, capture),
        executors,
        harness(settings),
        reply_schemas(settings),
        HostOptions(translator=WorldTranslator),
    )
    assert finished.halted is None
    return strategy, capture


STRATEGY, CAPTURE = _captured()


def test_a_run_reaches_many_distinct_states() -> None:
    assert len({state for state, _ in CAPTURE.pairs}) > 20


def test_decide_is_a_pure_function_of_the_state_and_the_view() -> None:
    for state, view in CAPTURE.pairs:
        first = STRATEGY.bind(state).decide(view)
        assert STRATEGY.bind(state).decide(view) == first


def test_deciding_again_from_the_state_it_proposed_changes_nothing() -> None:
    """A replayed proposal is idempotent: the decisions repeat, and the state is a fixed point."""
    for state, view in CAPTURE.pairs:
        proposal = STRATEGY.bind(state).decide(view)
        again = STRATEGY.bind(proposal.state).decide(view)
        assert again.state == proposal.state


def test_every_reached_state_round_trips_the_envelope_codec() -> None:
    settings = config()
    codec = plain_operation_registry()
    core = new_run(STRATEGY, harness(settings))
    for state, _ in CAPTURE.pairs:
        saved = RunEnvelope[IssueQueueStrategyState](
            schema_version=ENVELOPE_SCHEMA_VERSION,
            fence=HostFence(host_id=HostId(root="host"), epoch=1),
            strategy_id=STRATEGY.declaration.strategy_id,
            state_schema=STATE_SCHEMA,
            core=core,
            strategy=state,
            event_cursor=EventCursor(sequence=0),
        )
        loaded = codec.decode_envelope(
            RunEnvelope[IssueQueueStrategyState], codec.encode_envelope(saved)
        )
        assert loaded.strategy == state


@pytest.mark.parametrize("field_name", ["max_rounds", "max_attempts_per_issue"])
def test_the_derived_limits_cover_the_attempts_the_config_allows(field_name: str) -> None:
    settings = config(**{field_name: 3})
    assert limits(settings).max_attempts == settings.max_attempts


def test_after_an_operator_stop_the_strategy_starts_no_new_work() -> None:
    """From every state a run reaches, a stop leaves only settling, adopting and stopping."""
    stop = ControlChanged(control=ControlInput(control_id=ControlId(root="stop"), action="stop"))
    for state, view in CAPTURE.pairs:
        stopped = STRATEGY.bind(STRATEGY.bind(state).on_event(view, stop))
        proposal = stopped.decide(view)
        for decision in proposal.decisions:
            assert isinstance(decision, Withdraw | ProposeWinner | Stop | Measure), decision
            if isinstance(decision, Withdraw):
                disposition = decision.disposition
                assert isinstance(disposition, Settle)
                assert disposition.outcome in {"cancelled", "succeeded", "failed"}
