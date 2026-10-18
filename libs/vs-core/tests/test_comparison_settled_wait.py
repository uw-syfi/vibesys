"""A comparison a verdict rests on is the settled one: the flow waits for core's runs to land (#3047).

A close call earns core more runs of either side, and they end after the measurement that asked
for the comparison. ``ctx.evaluation.measure_compared`` therefore hands back the comparison once
no run that feeds it is under way, or the latest one when its bound is spent. These stories play
the host of a one-measurement flow: the measurement ends with a comparison that is still settling,
and the run's view then shows the comparison growing until it settles. Each story runs live and
with a restart after every step.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import FlowRun, fake_search
from vs_sim.api.testing import examples

from .flow_support import ROLES, Config
from .test_flows_restart_keeps import REVISION, measurement

LIMIT = 30.0
CANDIDATE = core.RevisionRef.of_git_commit("candidate")
BASE = core.RevisionRef.of_git_commit("base")
SCOPE = core.Scope(owner=core.AttemptId(root="a"), generation=0)
RESTARTS = pytest.mark.parametrize("restarting", [False, True], ids=["live", "restart-every-step"])


class Settled(core.Value):
    """What the flow publishes: the comparison it was handed, by its sample counts."""

    candidate_samples: int
    base_samples: int


@fake_search("settled", Config, ROLES)
async def measuring(ctx: core.Flow, config: Config) -> None:
    del config
    plan = measurement(REVISION, at=ctx.clock.now())
    found = await ctx.evaluation.measure_compared(plan, within=LIMIT)
    assert not isinstance(found, core.Failed)
    comparison = found.comparison
    assert comparison is not None
    ctx.publish(
        Settled(
            candidate_samples=comparison.candidate_samples, base_samples=comparison.base_samples
        )
    )


def comparison(samples: int) -> core.Comparison:
    """The comparison of a candidate and a base that each hold ``samples`` runs."""
    return core.Comparison(
        scope=SCOPE,
        metric="throughput",
        direction="max",
        candidate=CANDIDATE,
        candidate_input="candidate-input",
        base=BASE,
        base_input="base-input",
        candidate_value=120.0,
        base_value=100.0,
        ratio=1.2,
        noise=0.1,
        verdict=core.ComparisonVerdict.WITHIN_NOISE,
        candidate_samples=samples,
        base_samples=samples,
    )


def show(run: FlowRun[Config], samples: int, *, settling: bool) -> None:
    """The run's view now holds the comparison over ``samples`` runs a side."""
    run.view.changes["comparisons"] = (
        core.StatedComparison(comparison=comparison(samples), settling=settling),
    )


def finish_measurement(run: FlowRun[Config], samples: int, *, settling: bool) -> None:
    """The measurement ends with the comparison over ``samples`` runs a side."""
    asked = run.expect(core.Measure)
    show(run, samples, settling=settling)
    request = asked.request
    assert isinstance(request, core.Measure)
    result = core.MeasurementResult(
        scope=request.scope, evidence=(), status=core.ObservationStatus.SUCCEEDED
    )
    run.answer(
        asked,
        core.MeasurementFinished(
            decision=asked.decision, result=result, comparison=comparison(samples)
        ),
    )


def poll(run: FlowRun[Config]) -> float:
    """Move the clock to the flow's next look at the comparison; the time it looked at."""
    (at,) = run.sleeping_until
    run.advance_to(at)
    return at


@RESTARTS
def test_a_comparison_that_is_not_settling_is_handed_back_at_once(*, restarting: bool) -> None:
    run = FlowRun.start(measuring, Config(), restarting=restarting)
    finish_measurement(run, 3, settling=False)

    assert run.sleeping_until == ()
    assert run.published(Settled) == Settled(candidate_samples=3, base_samples=3)


@RESTARTS
def test_a_settling_comparison_is_awaited_until_core_s_runs_have_landed(
    *, restarting: bool
) -> None:
    run = FlowRun.start(measuring, Config(), restarting=restarting)
    finish_measurement(run, 1, settling=True)
    assert run.published(Settled) is None, "the first comparison is not the one a verdict rests on"

    show(run, 2, settling=True)
    poll(run)
    assert run.published(Settled) is None
    show(run, 3, settling=False)
    poll(run)

    assert run.published(Settled) == Settled(candidate_samples=3, base_samples=3)
    assert run.finished is not None


@RESTARTS
def test_a_comparison_that_never_settles_is_handed_back_as_it_stands_when_its_bound_is_spent(
    *, restarting: bool
) -> None:
    run = FlowRun.start(measuring, Config(), restarting=restarting)
    finish_measurement(run, 1, settling=True)
    show(run, 2, settling=True)
    run.advance_to(LIMIT * 3 + 1.0)

    assert run.published(Settled) == Settled(candidate_samples=2, base_samples=2)
    assert run.sleeping_until == ()


@RESTARTS
def test_a_comparison_whose_base_samples_went_stale_is_the_last_one_seen(
    *, restarting: bool
) -> None:
    """The view no longer states it (no fresh base sample): there is nothing more to wait for."""
    run = FlowRun.start(measuring, Config(), restarting=restarting)
    finish_measurement(run, 1, settling=True)
    run.view.changes["comparisons"] = ()
    poll(run)

    assert run.published(Settled) == Settled(candidate_samples=1, base_samples=1)


@RESTARTS
@settings(max_examples=examples(pr=8, nightly=60), deadline=None, derandomize=True, database=None)
@given(waits=st.integers(0, 6))
def test_the_flow_looks_again_for_each_run_that_lands_and_hands_back_the_settled_comparison(
    *, restarting: bool, waits: int
) -> None:
    run = FlowRun.start(measuring, Config(), restarting=restarting)
    finish_measurement(run, 1, settling=waits > 0)
    for landed in range(1, waits + 1):
        assert run.published(Settled) is None
        show(run, 1 + landed, settling=landed < waits)
        poll(run)

    assert run.published(Settled) == Settled(candidate_samples=1 + waits, base_samples=1 + waits)
