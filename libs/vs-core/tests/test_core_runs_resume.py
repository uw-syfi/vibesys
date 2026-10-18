"""Core's own runs serve comparisons; they never hold an agent's resume, and never chase staleness.

Core runs the base (and, without a floor, the candidate) again in an agent's attempt to make a
comparison it can trust. The agent never asked for those runs, so its wait ends when its own
jobs do: it resumes on its history, and the comparison its next turn reads grows when core's
runs land. When base samples go stale faster than a base run takes, a run that lands without
growing the base's fresh samples is the last one a close call earns.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_baseline_in_flight import strategy_baseline
from .test_comparison_liveness import base_runs, end, running
from .test_measurement_comparison import FLOOR, STALENESS, Lab, stamped, started_with
from .test_resume_receipt_producers import dispatch_publication, publication_state


def attempt_lab(**terms: float) -> tuple[Lab, core.Continuation]:
    """An attempt whose agent's turn is about to wait, with no measurement made yet."""
    state, wait = publication_state()
    measured = {job.submission_id for job in state.evaluation.jobs}
    state = state.model_copy(
        update={
            "evaluation": core.EvaluationState(),
            "records": state.records.model_copy(
                update={
                    "records": tuple(
                        row for row in state.records.records if row.request_id not in measured
                    )
                }
            ),
        }
    )
    scope = state.attempts.attempts[0]
    lab = Lab(
        state=stamped(state, **terms),
        scopes={"a": core.Scope(owner=scope.attempt_id, generation=scope.generation)},
    )
    return lab, wait


def job_of(lab: Lab, request: core.SubmitMeasurement) -> core.ResourceId:
    (job,) = (j for j in lab.state.evaluation.jobs if j.submission_id == request.request_id)
    return job.resource_id


def suspended(
    lab: Lab, wait: core.Continuation, jobs: tuple[core.ResourceId, ...]
) -> core.Transition:
    waited = wait.model_copy(update={"jobs": jobs, "deadline_at": 1000.0})
    return core.step(lab.state, core.TurnSuspended(continuation=waited))


@pytest.mark.regression
@pytest.mark.parametrize("floor", [FLOOR, 0.0])
@pytest.mark.parametrize("order", ["wait-then-end", "end-then-wait"])
def test_an_agent_resumes_on_its_own_jobs_while_core_still_runs_the_base(
    floor: float, order: str
) -> None:
    """The agent's wait ends when its job does; core's base run lands later, into the comparison.

    The agent's call allocates its candidate's run and core's run of the base in its attempt.
    Core used to defer the resume until every submission in the attempt had ended, its own base
    run included, so the agent waited past its own job, and past its deadline when base runs
    followed one another. The resume is authorized at once, its dispatch is accepted, and the
    base run's end afterwards logs the comparison and leaves the published history as it was.
    """
    lab, wait = attempt_lab(floor=floor)
    candidate, base = lab.call("a", "c1", "input-1")
    assert base.plan.purpose == "baseline"
    if order == "wait-then-end":
        lab.state = started_with(lab.state, candidate)
        lab.state = suspended(lab, wait, (job_of(lab, candidate),)).state
        assert lab.state.evaluation.continuations[0].phase == core.ContinuationPhase.WAITING
        end(lab, candidate, "sample", 130.0)
    else:
        end(lab, candidate, "sample", 130.0)
        lab.state = suspended(lab, wait, (job_of(lab, candidate),)).state
    phase = lab.state.evaluation.continuations[0].phase
    assert phase == core.ContinuationPhase.AUTHORIZED, "the agent's own job ended: it resumes"
    assert base in running(lab), "core's base run is still under way"
    history = lab.state.attempts.attempts[0].evaluation_history
    assert history.covered_submissions == (candidate.request_id,)
    assert dispatch_publication(lab.state, wait).events == ()
    end(lab, base, "sample", 100.0)
    assert [c.candidate_input for c in lab.comparisons()] == ["input-1"]
    assert lab.state.attempts.attempts[0].evaluation_history == history
    assert dispatch_publication(lab.state, wait).events == ()


def close_call_lab(floor: float) -> Lab:
    """One candidate that reads like its base, both measured once: a close call."""
    lab = Lab.of("a", floor=floor)
    candidate, base = lab.call("a", "c1", "input-1")
    end(lab, candidate, "sample", 100.5)
    end(lab, base, "sample", 100.0)
    return lab


@pytest.mark.regression
@pytest.mark.parametrize("floor", [FLOOR, 0.0])
def test_a_base_that_goes_stale_faster_than_it_runs_is_not_chased(floor: float) -> None:
    """A close call's base run that lands after the sample it should join expired is the last.

    Each base run takes longer than ``base_staleness``, so when it lands the base still holds
    one fresh sample, and the comparison it changes is a close call again. Core used to take
    another base run at every such end, up to the attempt's submission limit (five); the base's
    fresh samples never grow, so none of them narrowed anything.
    """
    lab = close_call_lab(floor)
    for _ in range(core.MAX_SUBMISSIONS_PER_MEASUREMENT + 1):
        pending = running(lab)
        if not pending:
            break
        lab.advance(STALENESS + 1.0)
        for run in pending:
            end(lab, run, "sample", 100.5 if run.plan.purpose != "baseline" else 100.0)
    assert len(base_runs(lab)) == 2, "one base run for the close call, which did not narrow it"
    assert running(lab) == []


@given(
    floor=st.sampled_from((FLOOR, 0.0)),
    lags=st.lists(st.floats(min_value=0.0, max_value=2 * STALENESS), min_size=1, max_size=8),
)
def test_a_close_call_takes_a_bounded_number_of_base_runs_whatever_the_staleness(
    floor: float, lags: list[float]
) -> None:
    """However long each base run takes against ``base_staleness``, a close call stays bounded.

    The base never takes more runs than the most fresh samples a close call earns it, and the
    runs stop.
    """
    lab = close_call_lab(floor)
    most = core.BASE_MAX_SAMPLES if floor else core.MIN_SAMPLES_WITHOUT_FLOOR
    # The given lags, then runs that land at once, until nothing is under way.
    for lag in (*lags, *(0.0,) * (2 * most)):
        pending = running(lab)
        if not pending:
            break
        lab.advance(lag)
        for run in pending:
            end(lab, run, "sample", 100.5 if run.plan.purpose != "baseline" else 100.0)
    assert running(lab) == [], "every chain of runs stops"
    assert len(base_runs(lab)) <= most


def test_a_claimed_strategy_run_counts_as_the_base_run_under_way() -> None:
    """While the strategy's own run of the base is claimed and under way, no close call adds one."""
    lab = Lab.of("a", "b")
    own = strategy_baseline(lab)
    lab.call("a", "c1", "input-1")
    (candidate,) = [r for r in running(lab) if r.plan.purpose != "baseline"]
    end(lab, candidate, "sample", 100.5)
    assert own in running(lab)
    assert [r for r in base_runs(lab) if r != own] == [], "the strategy's run is the base run"
    lab.call("b", "c2", "input-2")
    (other,) = [r for r in running(lab) if r.plan.purpose != "baseline"]
    end(lab, other, "sample", 100.4)
    assert [r for r in base_runs(lab) if r != own] == [], "still the one run under way"
