"""The scale harness drives the production shell and loop, and its knobs reach the run.

Each test checks one knob against what the run observably did, so a harness that ignored
the knob (a script never consulted, a control never delivered) fails here instead of
making a later scenario pass vacuously. Runs are kept to two workstreams and one round:
every commit validates the whole envelope, so cost grows quickly with the run's size.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.scale_dynamic_run import Scenario, run_scale
from tests.support.timed_dynamic_run import CommitCapExceededError, TimingProfile
from tests.vibesys.orchestration.dynamic.strategy._replies import reviewed

from vs_core.api import RunStatus
from vs_runtime.api.core import DispatchCapExceededError
from vs_runtime.api.infrastructure import RunStopped
from vs_slurm.api import SecondsRange, SlurmTimingProfile

_EXACT = TimingProfile(
    turn_s=SecondsRange.exactly(40.0),
    slurm=SlurmTimingProfile(
        queue_wait_s=SecondsRange.exactly(30.0),
        run_s=SecondsRange.exactly(150.0),
        completing_s=SecondsRange.exactly(35.0),
    ),
)


def test_a_scenario_runs_its_workstreams_in_parallel_to_an_adopted_candidate() -> None:
    run = run_scale(Scenario(in_flight=2, rounds=1, profile=_EXACT))

    assert run.error is None, run.error
    assert run.core.run.status is RunStatus.TERMINAL
    assert run.core.run.result is not None
    assert run.core.run.result.outcome == "success"
    assert dict(run.agent_turns) == {"orchestrator": 1, "implementer": 2, "judge": 2}
    assert run.max_overlap("implementer") == 2
    assert run.decisions, "the strategy's decisions are recorded"
    assert run.ended_at > run.started_at


def test_the_judge_script_decides_which_candidates_are_eligible() -> None:
    run = run_scale(
        Scenario(in_flight=2, rounds=1, profile=_EXACT, judge=lambda _n: reviewed(passed=False))
    )

    assert run.error is None, run.error
    assert run.agent_turns["judge"] >= 2, "the scripted judge was consulted"
    assert not [item for item in run.core.settlement.settlements if item.eligible]


def test_a_pause_resume_pair_is_delivered_and_the_run_still_finishes() -> None:
    run = run_scale(
        Scenario(in_flight=2, rounds=1, profile=_EXACT, pause_after=10.0, resume_after=200.0)
    )

    assert run.paused_at is not None
    assert run.paused_at >= run.started_at + 10.0
    assert run.error is None, run.error
    assert run.core.run.status is RunStatus.TERMINAL
    assert run.ended_at > run.started_at + 200.0


def test_the_run_deadline_ends_the_run_through_core() -> None:
    run = run_scale(Scenario(in_flight=2, rounds=1, profile=_EXACT, deadline_at=26.0))

    assert run.error is None, run.error
    assert run.core.run.status is RunStatus.TERMINAL
    assert run.core.run.result is not None
    assert run.core.run.result.reason == "run deadline reached"


def test_hitting_the_dispatch_cap_fails_the_run_instead_of_ending_it_quietly() -> None:
    with pytest.raises(DispatchCapExceededError):
        run_scale(Scenario(in_flight=2, rounds=1, profile=_EXACT, max_dispatches=5))


# Controls land before the first workstream turn starts (the baseline measurement and the
# planner turn take about 250 s on `_EXACT`). Later stacked PRs widen the window as the
# run lifecycle learns to handle controls during work.
_CONTROL_WINDOW_S = 240.0
_EXAMPLES = 20 if os.environ.get("VIBESYS_FULL_PROPERTIES") == "1" else 4


@st.composite
def _scenarios(draw: st.DrawFn) -> Scenario:
    stop = draw(st.none() | st.floats(min_value=1.0, max_value=_CONTROL_WINDOW_S))
    pause = draw(st.none() | st.floats(min_value=1.0, max_value=_CONTROL_WINDOW_S))
    resume = None if pause is None else pause + draw(st.floats(min_value=1.0, max_value=200.0))
    return Scenario(
        in_flight=draw(st.integers(min_value=1, max_value=2)),
        rounds=1,
        profile=_EXACT,
        stop_after=stop,
        pause_after=pause,
        resume_after=resume,
    )


@settings(max_examples=_EXAMPLES, deadline=None, derandomize=True)
@given(_scenarios())
def test_a_run_with_early_controls_ends_live_and_consistent(scenario: Scenario) -> None:
    """Liveness (bounded requests, no spin, nothing open, no orphan wait) is checked by
    ``run_scale`` itself; here the run must also end in a state the controls explain.
    """
    run = run_scale(scenario)

    if run.error is None:
        assert run.core.run.status is RunStatus.TERMINAL
        assert run.core.run.result is not None
    else:
        assert isinstance(run.error, RunStopped), run.error
        assert scenario.stop_after is not None


def test_a_run_that_keeps_committing_without_settling_fails_instead_of_spinning() -> None:
    run = run_scale(Scenario(in_flight=2, rounds=1, profile=_EXACT, max_commits=50))

    assert isinstance(run.error, CommitCapExceededError)
