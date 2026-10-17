"""Core's rules for agent turns that wait, share an evaluation or meet a refusal, on a toy search.

These are the core-owned halves of the dynamic scenarios in
``tests/vibesys/orchestration/dynamic/search``: whatever the search decides, core must keep
every run it hosts live and consistent. The real kernel and the real agent pipeline
(`AgentTurns`) run a toy search (`toy_agent_search`) against scripted executors, with no
shell, no durable store and no clock: an example takes milliseconds.

- A turn that validated a wait is suspended and resumed once its evaluations settle, whichever
  turns wait, however long the jobs run, and whichever reply the suspended turn ended with.
- The roles of one workstream that ask for the same candidate share one evaluation: one
  charge, one handle, never refused for budget.
- Whatever core refuses of what the search asks (over a limit), the run still ends with every
  workspace closed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import (
    Budget,
    End,
    Faults,
    Journal,
    assert_live,
    sample_limits,
    sample_retries,
)
from vs_sim.api.testing import examples

from .toy_agent_search import (
    ENDINGS,
    LIMITS,
    ROLES,
    Config,
    Done,
    Ending,
    ToyHost,
    run_toy,
    summary,
)

if TYPE_CHECKING:
    from vs_core.api.testing import Trace

_SETTINGS = settings(
    max_examples=examples(pr=3, nightly=12), deadline=None, derandomize=True, database=None
)
_OPEN = {
    core.ContinuationPhase.WAITING,
    core.ContinuationPhase.AUTHORIZED,
    core.ContinuationPhase.REOPENING,
}


def assert_ended_closed(trace: Trace, limits: core.Limits = LIMITS) -> None:
    """The run is terminal and live, every workspace closed, no wait left, every ask answered."""
    assert trace.finished
    # The root only returned: the framework adopted the input and ended the run, once.
    finishes = [d for d in trace.decisions if isinstance(d, core.Finish)]
    assert [(f.result.outcome, f.result.reason) for f in finishes] == [
        ("success", "kept-input: the input")
    ]
    journal = Journal.from_log(trace.log)
    assert_live(journal, trace.core, Budget(retries=limits.request_retries.limit), End.TERMINAL)
    assert all(a.phase == core.AttemptPhase.TERMINAL for a in trace.core.attempts.attempts)
    assert not [c for c in trace.core.evaluation.continuations if c.phase in _OPEN]
    # Every wait core recorded was resumed once, and no turn was resumed without one.
    resumed = [r for r in trace.requests if isinstance(r, core.ResumeSessionTurn)]
    assert len(resumed) == len({r.continuation_id for r in resumed})
    assert {r.continuation_id for r in resumed} <= {
        c.continuation_id for c in trace.core.evaluation.continuations
    }
    ended = summary(trace).done
    assert all(item.closed for item in ended if item.opened)
    # One result per ask: a role answered, or it failed with a typed reason, never both or neither.
    for item in ended:
        assert not set(item.asked) & set(item.failed)


@dataclass(frozen=True)
class Scenario:
    """One generated run: the budgets, which turns wait, and how the executors misbehave."""

    workstreams: int
    in_flight: int
    waits: tuple[bool, ...]
    polls: tuple[int, ...]
    roles: tuple[str, ...]
    ends: tuple[Ending, ...]
    shares: bool
    faults: Faults

    def run(self) -> Trace:
        """Drive the toy search against the scenario's executors."""
        host = ToyHost(
            waits=self.waits,
            polls=self.polls,
            roles=self.roles,
            ends=self.ends,
            shares=self.shares,
        )
        config = Config(workstreams=self.workstreams, in_flight=self.in_flight)
        return run_toy(host, config, faults=self.faults)


_FAULTS = st.builds(Faults, duplicate=st.booleans(), reorder=st.booleans())
SCENARIOS = st.builds(
    Scenario,
    workstreams=st.integers(1, 4),
    in_flight=st.integers(1, 2),
    waits=st.lists(st.booleans(), min_size=1, max_size=6).map(tuple),
    polls=st.lists(st.integers(0, 4), min_size=1, max_size=4).map(tuple),
    roles=st.sampled_from((ROLES, ("judge",), ("implementer",))),
    ends=st.just(("wait",)),
    shares=st.booleans(),
    faults=_FAULTS,
)


@_SETTINGS
@given(scenario=SCENARIOS)
def test_a_run_ends_whatever_its_agents_wait_for(scenario: Scenario) -> None:
    assert_ended_closed(scenario.run())


def test_a_judge_that_waits_for_its_own_evaluation_is_resumed() -> None:
    """The v12 stall: a read-only turn that ends waiting is resumed once its job ends."""
    trace = Scenario(
        workstreams=1,
        in_flight=1,
        waits=(True,),
        polls=(2,),
        roles=("judge",),
        ends=("wait",),
        shares=True,
        faults=Faults(),
    ).run()

    assert_ended_closed(trace)
    assert len(trace.core.evaluation.continuations) == 1
    assert len([r for r in trace.requests if isinstance(r, core.ResumeSessionTurn)]) == 1
    assert summary(trace).done[0].asked == ("implementer", "judge")


@settings(max_examples=examples(pr=3, nightly=3), deadline=None, derandomize=True, database=None)
@given(scenario=SCENARIOS)
def test_a_run_ends_whatever_its_agents_wait_for_when_the_host_restarts_at_every_step(
    scenario: Scenario,
) -> None:
    """The same, with the whole envelope round-tripped through the codec after every step."""
    assert_ended_closed(replace(scenario, faults=replace(scenario.faults, reload=True)).run())


@_SETTINGS
@given(
    ends=st.lists(st.sampled_from(ENDINGS), min_size=1, max_size=4).map(tuple),
    roles=st.sampled_from((ROLES, ("judge",), ("implementer",))),
    polls=st.integers(0, 8),
)
def test_a_suspended_turn_is_a_wait_whatever_reply_it_ends_with(
    ends: tuple[Ending, ...], roles: tuple[str, ...], polls: int
) -> None:
    """Core holds a turn that validated a wait as suspended, so no reply may be accepted.

    A turn that ends with its final result or an invalid reply instead of the waiting reply must
    still yield: accepting or correcting it would request a turn core refuses, which halts the
    run (the driver raises). So what the search asks of core must not depend on which reply the
    suspended turn ended with: it equals what it asks when every turn ends with the waiting reply.
    """

    def run(chosen: tuple[Ending, ...]) -> Trace:
        return Scenario(
            workstreams=1,
            in_flight=1,
            waits=(True,),
            polls=(polls,),
            roles=roles,
            ends=chosen,
            shares=True,
            faults=Faults(),
        ).run()

    trace = run(ends)

    assert_ended_closed(trace)
    if "invalid" not in ends:
        # Whether core records a wait on an invalid reply is core's own rule, so only the other
        # replies are compared.
        assert trace.core.evaluation.continuations
        assert [type(d).__name__ for d in trace.decisions] == [
            type(d).__name__ for d in run(("wait",)).decisions
        ]


@_SETTINGS
@given(
    workstreams=st.integers(1, 3),
    in_flight=st.integers(1, 2),
    waits=st.lists(st.booleans(), min_size=1, max_size=3).map(tuple),
    polls=st.lists(st.integers(0, 4), min_size=1, max_size=3).map(tuple),
    faults=_FAULTS,
)
def test_roles_of_a_workstream_asking_for_one_candidate_share_one_evaluation(
    workstreams: int,
    in_flight: int,
    waits: tuple[bool, ...],
    polls: tuple[int, ...],
    faults: Faults,
) -> None:
    """The implementer submits an evaluation and ends its turn with the job still running (the v13
    run); its judge, a read-only turn of the same workstream, asks for the same candidate.

    Core admits one evaluation per exact candidate and scope, so the judge is handed the
    implementer's submission and may wait on it: its call is never refused, nothing is charged
    twice, and the first call of a workstream is the only one that is not a repeat.
    """
    host = ToyHost(waits=waits, polls=polls, roles=("judge",), submit_only=("implementer",))
    trace = run_toy(host, Config(workstreams=workstreams, in_flight=in_flight), faults=faults)

    assert_ended_closed(trace)
    calls = trace.core.evaluation.agent_calls
    assert calls
    assert all(call.rejection is None for call in calls), calls
    by_attempt: dict[str, list[core.AgentCall]] = {}
    for call in sorted(calls, key=lambda row: int(row.call_id.removeprefix("call-"))):
        by_attempt.setdefault(call.scope.owner.root, []).append(call)
    for asks in by_attempt.values():
        assert len({call.request_id for call in asks}) == 1, "one candidate, one submission"
        assert asks[0].repeat is False
        assert all(call.repeat for call in asks[1:])
    submitted = [r for r in trace.requests if isinstance(r, core.SubmitMeasurement)]
    assert len(submitted) == len(by_attempt), "one charge per candidate"


_LIMITS = st.builds(
    sample_limits,
    max_attempts=st.integers(1, 3),
    max_turns=st.integers(1, 6),
    max_parallel=st.just(2),
    request_retries=st.integers(0, 2).map(sample_retries),
    max_refunds=st.integers(0, 2),
)


@_SETTINGS
@given(
    config=st.builds(Config, workstreams=st.integers(1, 3), in_flight=st.integers(1, 2)),
    limits=_LIMITS,
    waits=st.lists(st.booleans(), min_size=1, max_size=3).map(tuple),
)
def test_a_run_ends_with_every_workspace_closed_whatever_core_refuses_over_its_limits(
    config: Config, limits: core.Limits, waits: tuple[bool, ...]
) -> None:
    """Core refuses a start, a turn or a settlement the limits do not allow.

    The search takes a refusal as a typed failure and settles what it opened, so the run must
    still reach terminal with nothing left open, whichever limit bites and wherever it does.
    """
    trace = run_toy(ToyHost(waits=waits, polls=(1,)), config, limits=limits)

    assert_ended_closed(trace, limits)


@_SETTINGS
@given(
    mode=st.sampled_from(core.StopMode),
    config=st.builds(Config, workstreams=st.integers(1, 3), in_flight=st.integers(1, 2)),
    waits=st.lists(st.booleans(), min_size=1, max_size=3).map(tuple),
    at=st.sampled_from([0.0, 1.0, 5.0]),
)
def test_a_stop_ends_the_run_with_every_resource_released_in_the_mode_the_operator_chose(
    mode: core.StopMode, config: Config, waits: tuple[bool, ...], at: float
) -> None:
    """Drain and cancel are the operator's choice; either way nothing is left open."""
    stop = core.RunControlEvent(
        control=core.ControlInput(control_id=core.ControlId(root="stop"), action="stop", mode=mode),
        now_at=at,
    )
    trace = run_toy(ToyHost(waits=waits, polls=(1,), events=(stop,)), config)

    assert trace.finished
    finishes = [d for d in trace.decisions if isinstance(d, core.Finish)]
    assert [f.mode for f in finishes] == [mode.value], "the mode the operator gave is the one used"
    assert all(a.phase == core.AttemptPhase.TERMINAL for a in trace.core.attempts.attempts)
    assert not [c for c in trace.core.evaluation.continuations if c.phase in _OPEN]
    assert all(item.closed for item in summary(trace).done if item.opened)


def _ends(trace: Trace) -> list[Done]:
    return sorted(summary(trace).done, key=lambda done: done.key)


def _paused(scenario: Scenario, pause_at: int) -> tuple[Trace, ToyHost]:
    host = ToyHost(
        waits=scenario.waits,
        polls=scenario.polls,
        roles=scenario.roles,
        ends=scenario.ends,
        shares=scenario.shares,
        pause_at=pause_at,
    )
    config = Config(workstreams=scenario.workstreams, in_flight=scenario.in_flight)
    return run_toy(host, config, faults=scenario.faults), host


#: A judge that waits for its evaluation, whose job reports running for two polls.
_WAITING_JUDGE = Scenario(
    workstreams=1,
    in_flight=1,
    waits=(True,),
    polls=(2,),
    roles=("judge",),
    ends=("wait",),
    shares=True,
    faults=Faults(),
)


@settings(max_examples=examples(pr=8, nightly=60), deadline=None, derandomize=True, database=None)
@example(scenario=_WAITING_JUDGE, pause_at=11)  # the job ends while paused (the live stall)
@example(scenario=_WAITING_JUDGE, pause_at=9)  # the waiting turn's checkpoint lands paused
@example(scenario=_WAITING_JUDGE, pause_at=7)  # the turn asks for its evaluation paused
@given(scenario=SCENARIOS, pause_at=st.integers(0, 40))
def test_a_pause_holds_the_run_and_after_the_resume_it_ends_as_it_would_have(
    scenario: Scenario, pause_at: int
) -> None:
    """A pause before any executor answer, then a resume once nothing runs, changes no outcome.

    Pausing holds what the search decides until the resume; work already under way (a turn,
    its evaluation call, its wait, a job's polls) finishes while paused, and what it yields
    is delivered. So a judge whose evaluation ends while the run is paused is resumed, and
    every workstream ends as it does unpaused: the same asks answered, nothing timed out.
    """
    trace, host = _paused(scenario, pause_at)

    assert_ended_closed(trace)
    # Held work resumes in another order, so parallel workstreams may end in another order.
    assert _ends(trace) == _ends(scenario.run())
    # The run may end while paused (its last decision, the Finish, is taken while paused).
    actions = [c.action for c in trace.core.run.controls]
    assert actions in (["pause"], ["pause", "resume"]) if host.paused_with else actions == []
    waits = {c.continuation_id for c in trace.core.evaluation.continuations}
    resumed = {r.continuation_id for r in trace.requests if isinstance(r, core.ResumeSessionTurn)}
    assert resumed == waits, "every wait recorded is resumed"
