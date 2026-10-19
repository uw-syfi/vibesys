"""The time the run's capacity gate holds a turn is never charged to its limit (S179).

The gate holds a turn its provider stopped (an outage, a spent quota) inside the turn's own call
and reports the hold (``TurnHeld``). Core moves the turn's bound by it: the holds that ended are
credited whole, the current one by its length up to the budget once released, by the budget while
the gate is in it, and without end while it is parked for the operator; a hold that began after
the turn's own time ran out credits nothing. A turn core ends while the gate holds it is lost to
its provider (``PROVIDER_UNAVAILABLE``), not by its transport, and is asked again whatever the
role's ``lost_turns``, a resumed turn included.

The toy search runs on the real core; the implementer's first turns never answer (they overrun),
and the gate's report arrives while they run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import Answer, Running, Succeeded, Trace

from .test_agent_turn_runs import assert_ended_closed
from .test_role_limits_through_core import implementer_turns
from .toy_agent_search import IMPLEMENTER, ToyHost, run_toy, summary, toy_search

_SETTINGS = settings(max_examples=12, deadline=None, derandomize=True, database=None)
BUDGET = 900.0
"""The most of the current hold the gate lets core credit (the outage budget, scaled down)."""


@dataclass(frozen=True)
class Shape:
    """A hold as the gate reports it, in seconds relative to the turn's dispatch."""

    credit: float = 0.0
    since: float | None = None
    released: float | None = None
    parked: bool = False

    def hold(self, dispatched: float) -> core.CapacityHold:
        return core.CapacityHold(
            credit=self.credit,
            since=None if self.since is None else dispatched + self.since,
            released_at=None if self.released is None else dispatched + self.released,
            parked=self.parked,
            budget=BUDGET,
        )

    def ends_after(self, limit: float) -> float:
        """How long after its dispatch core ends a turn with stated ``limit`` and this hold."""
        at = limit + self.credit
        if self.since is None or self.since > at:
            return at
        if self.parked:
            return math.inf
        current = BUDGET if self.released is None else self.released - self.since
        return at + min(current, BUDGET)

    @property
    def holding(self) -> bool:
        return self.since is not None and self.released is None


@dataclass
class HeldHost(ToyHost):
    """The implementer's first ``overruns`` turns never answer; the gate reports ``shape``.

    ``resumed`` makes the overrunning turns the ones that continue an evaluation wait.
    ``junk_first`` of the implementer's turns before them reply with JSON that does not parse,
    so core corrects them first.
    """

    overruns: int = 1
    junk_first: int = 0
    shape: Shape = field(default_factory=Shape)
    resumed: bool = False
    dispatched_at: dict[str, float] = field(default_factory=dict)
    _started: int = 0
    _overrunning: set[str] = field(default_factory=set)
    _junk: set[str] = field(default_factory=set)

    def during_turn(
        self, request: core.Request, state: core.CoreState
    ) -> tuple[core.CoreEvent, ...]:
        events = super().during_turn(request, state)
        kind = core.ResumeSessionTurn if self.resumed else core.DispatchTurn
        if (
            not isinstance(request, kind)
            or request.turn.session.role_id.root != "toy-implementer"
            or request.request_id is None
        ):
            return events
        self._started += 1
        if self._started <= self.junk_first:
            self._junk.add(request.request_id.root)
            return events
        if self._started > self.junk_first + self.overruns:
            return events
        self._overrunning.add(request.request_id.root)
        now = state.run.now_at
        self.dispatched_at[request.turn.invocation_id.root] = now
        held = core.TurnHeld(invocation_id=request.turn.invocation_id, hold=self.shape.hold(now))
        return (*events, held)

    def _turn(
        self, request: core.DispatchTurn | core.ResumeSessionTurn, state: core.CoreState
    ) -> Answer:
        if request.request_id is not None and request.request_id.root in self._overrunning:
            return Running(resource_id=core.ResourceId(root="lease:overrun"))
        answer = super()._turn(request, state)
        if request.request_id is not None and request.request_id.root in self._junk:
            assert isinstance(answer, Succeeded)
            return replace(answer, output_json="not json")
        return answer


def _results(trace: Trace, host: HeldHost) -> list[core.TurnResult]:
    return [
        event
        for event in trace.events
        if isinstance(event, core.TurnResult)
        and event.invocation.invocation_id.root in host.dispatched_at
    ]


shapes = st.one_of(
    st.just(Shape()),
    st.builds(Shape, credit=st.floats(0.0, 2 * BUDGET), since=st.floats(0.0, 60.0)),
    st.builds(
        lambda credit, since, length: Shape(credit, since, since + length),
        st.floats(0.0, 2 * BUDGET),
        st.floats(0.0, 60.0),
        st.floats(0.0, 2 * BUDGET),
    ),
    st.builds(
        lambda credit, since: Shape(credit, since, parked=True),
        st.floats(0.0, BUDGET),
        st.floats(0.0, 60.0),
    ),
    # Late: the hold began after the turn's own time ran out.
    st.builds(Shape, since=st.just(1e6)),
)


def _ended_at(trace: Trace, role: core.Role) -> float | None:
    """When core ended the implementer's first turn: its re-ask is dispatched one backoff later."""
    turns = [
        record
        for record in trace.core.records.records
        if isinstance(record.request, core.DispatchTurn)
        and record.request.turn.session.role_id.root == "toy-implementer"
    ]
    if len(turns) < 2:  # the first turn and its re-ask
        return None
    return turns[1].until.at - role.turn_time - role.lost_turn_backoff.delay(0)


@_SETTINGS
@given(shape=shapes)
def test_a_turn_ends_at_its_limit_plus_the_hold_it_is_credited(shape: Shape) -> None:
    implementer = IMPLEMENTER.model_copy(update={"lost_turns": 1})
    host = HeldHost(overruns=1, shape=shape)

    trace = run_toy(host, definition=toy_search(implementer=implementer))

    assert_ended_closed(trace)
    [(_, dispatched)] = host.dispatched_at.items()
    expected = dispatched + shape.ends_after(implementer.turn_time)
    ended_at = _ended_at(trace, implementer)
    if expected >= trace.core.run.deadline_at:
        # The run's deadline comes first (a parked turn ends only with the run): no re-ask.
        assert ended_at is None
        return
    assert ended_at == pytest.approx(expected)
    [ended] = _results(trace, host)
    lost_to_provider = (
        shape.holding
        and shape.since is not None
        and shape.since <= implementer.turn_time + shape.credit
    )
    assert (ended.failure is core.TurnFailureKind.PROVIDER_UNAVAILABLE) is lost_to_provider
    if not lost_to_provider:
        assert ended.failure is core.TurnFailureKind.TRANSPORT_LOST


def _asked_again(
    overruns: int, *, resumed: bool, corrected: int = 0, turn_time: float | None = None
) -> Trace:
    implementer = IMPLEMENTER.model_copy(
        update={
            "lost_turns": 0,
            "corrections": corrected,
            "turn_time": turn_time or IMPLEMENTER.turn_time,
        }
    )
    host = HeldHost(
        overruns=overruns,
        junk_first=corrected,
        shape=Shape(since=1.0),
        resumed=resumed,
        # The first turn waits on an evaluation, and its resume is the one that overruns.
        waits=(True, False) if resumed else (),
        roles=("implementer",),
    )
    trace = run_toy(host, definition=toy_search(implementer=implementer))
    assert_ended_closed(trace)
    results = _results(trace, host)
    assert [r.failure for r in results] == [core.TurnFailureKind.PROVIDER_UNAVAILABLE] * overruns
    assert summary(trace).done[0].failed == ()
    return trace


@_SETTINGS
@given(overruns=st.integers(1, core.MAX_LOST_TURNS + 1))
def test_a_turn_lost_while_held_is_asked_again_whatever_the_roles_lost_turns(
    overruns: int,
) -> None:
    """Before, the role's ``lost_turns`` (here none) ended the ask at the first loss."""
    trace = _asked_again(overruns, resumed=False)
    assert len(implementer_turns(trace)) == overruns + 1


def test_a_resumed_turn_lost_while_held_is_asked_again() -> None:
    """Before, a turn that continued an evaluation wait was never asked again."""
    _asked_again(1, resumed=True)


@pytest.mark.regression
@pytest.mark.parametrize("corrected", [0, 4], ids=["no-corrections", "after-4-corrections"])
def test_outage_losses_never_spend_the_reask_chain(corrected: int) -> None:
    """More outage losses in one ask than ``MAX_REASK_CHAIN`` allows re-asks still recover.

    Before (review of #3283, MED-2), each re-ask after a ``PROVIDER_UNAVAILABLE`` loss was a
    link of the correction chain, so the 11th was rejected (``correction retry bound
    exhausted``) and the ask failed as if the role were broken; after 4 real corrections, the
    7th. An outage is the provider's, bounded by the gate's budget and its pause.
    """
    overruns = core.MAX_REASK_CHAIN + 2
    # Turns short enough that the run's deadline comes after every loss.
    trace = _asked_again(overruns, resumed=False, corrected=corrected, turn_time=1000.0)
    assert len(implementer_turns(trace)) == corrected + overruns + 1
