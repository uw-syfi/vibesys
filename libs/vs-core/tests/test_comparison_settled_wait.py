"""A comparison a verdict rests on is the settled one: the flow waits for core's runs to land (#3047).

A close call earns core more runs of either side, and they end after the measurement that asked
for the comparison. ``ctx.evaluation.measure_compared`` therefore hands back the comparison once
no run that feeds it is under way, or the latest one when its bound is spent. Core reports the end
of the last run that feeds it (``ComparisonSettled``), and the flow waits for that report, not on
a poll. These stories play the host of a one-measurement flow: the measurement ends with a
comparison that is still settling, and the run's view then shows the comparison growing until it
settles. Each story runs live and with a restart after every step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import FlowRun, fake_search
from vs_sim.api.testing import examples

from .flow_support import ROLES, Config
from .test_flows_restart_keeps import REVISION, measurement

if TYPE_CHECKING:
    from vs_core.api import SearchDef

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


def settle(run: FlowRun[Config]) -> None:
    """Core reports that no run feeding the comparison is under way any more."""
    run.send(
        core.ComparisonSettled(
            scope=SCOPE, candidate_input="candidate-input", base_input="base-input"
        )
    )


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
    assert run.published(Settled) is None
    show(run, 3, settling=False)
    settle(run)

    assert run.published(Settled) == Settled(candidate_samples=3, base_samples=3)
    assert run.finished is not None
    assert run.view.now_at == 0.0, "handed back when it settled, at no later look"


@RESTARTS
@pytest.mark.parametrize(
    "other",
    [
        {"candidate_input": "other-input"},
        {"base_input": "other-base"},
        {"scope": core.Scope(owner=core.AttemptId(root="b"), generation=0)},
        {"scope": core.Scope(owner=core.AttemptId(root="a"), generation=1)},
    ],
    ids=["another-input", "another-base", "another-attempt", "another-generation"],
)
def test_another_comparison_settling_does_not_end_the_wait(
    *, restarting: bool, other: dict[str, object]
) -> None:
    run = FlowRun.start(measuring, Config(), restarting=restarting)
    finish_measurement(run, 1, settling=True)
    show(run, 2, settling=True)
    told = {"scope": SCOPE, "candidate_input": "candidate-input", "base_input": "base-input"}
    run.send(core.ComparisonSettled.model_validate({**told, **other}))

    assert run.published(Settled) is None
    show(run, 3, settling=False)
    settle(run)
    assert run.published(Settled) == Settled(candidate_samples=3, base_samples=3)


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
    settle(run)

    assert run.published(Settled) == Settled(candidate_samples=1, base_samples=1)


@RESTARTS
@settings(max_examples=examples(pr=8, nightly=60), deadline=None, derandomize=True, database=None)
@given(waits=st.integers(0, 6))
def test_the_flow_waits_through_every_run_that_lands_and_hands_back_the_settled_comparison(
    *, restarting: bool, waits: int
) -> None:
    """Each run that lands grows the comparison; core reports only the last one's end."""
    run = FlowRun.start(measuring, Config(), restarting=restarting)
    finish_measurement(run, 1, settling=waits > 0)
    for landed in range(1, waits + 1):
        assert run.published(Settled) is None
        assert run.sleeping_until == (LIMIT * 3,), "it sleeps only until its bound"
        show(run, 1 + landed, settling=landed < waits)
    if waits:
        settle(run)

    assert run.published(Settled) == Settled(candidate_samples=1 + waits, base_samples=1 + waits)


def measuring_within(limit: float) -> SearchDef[Config]:
    """The measuring flow, with a measurement limit of ``limit``."""

    @fake_search("settled-within", Config, ROLES)
    async def root(ctx: core.Flow, config: Config) -> None:
        del config
        plan = measurement(REVISION, at=ctx.clock.now())
        found = await ctx.evaluation.measure_compared(plan, within=limit)
        assert not isinstance(found, core.Failed)
        assert found.comparison is not None
        ctx.publish(
            Settled(
                candidate_samples=found.comparison.candidate_samples,
                base_samples=found.comparison.base_samples,
            )
        )

    return root


def journaled_while_settling(limit: float) -> int:
    """The steps a flow journals while its comparison settles for its whole bound."""
    run = FlowRun.start(measuring_within(limit), Config())
    finish_measurement(run, 1, settling=True)
    before = len(run.state.journal)
    for _ in range(10_000):  # a hang guard only: a wait ends at its bound
        if run.published(Settled) is not None:
            break
        run.advance_to(min(run.sleeping_until))
    assert run.published(Settled) == Settled(candidate_samples=1, base_samples=1)
    return len(run.state.journal) - before


@pytest.mark.regression
@pytest.mark.parametrize("limit", [300.0, 1800.0])
def test_a_settling_comparison_journals_no_more_steps_for_a_longer_bound(limit: float) -> None:
    """A wait's journal does not grow with how long the comparison may settle.

    Every journaled step is replayed on every resume. Before, the flow looked at the view every
    5 run-clock seconds, journaling each look: at a 30-minute limit about a thousand steps for
    one idea's verdict.
    """
    assert journaled_while_settling(limit) == journaled_while_settling(LIMIT)


class Headline(core.Value):
    """What the headline flow publishes: the value its result's benchmark record states."""

    value: float | None


@fake_search("settled-headline", Config, ROLES)
async def measuring_headline(ctx: core.Flow, config: Config) -> None:
    del config
    plan = measurement(REVISION, at=ctx.clock.now())
    found = await ctx.evaluation.measure_compared(plan, within=LIMIT)
    assert not isinstance(found, core.Failed)
    stated = [e.reading.value for e in found.result.evidence if e.reading is not None]
    ctx.publish(Headline(value=stated[0] if stated else None))


def benchmark(value: float) -> core.EvidenceRef:
    """The official benchmark record of the candidate, stating ``value``."""
    return core.EvidenceRef(
        evidence_id=core.EvidenceId(root="benchmark"),
        kind=core.EvidenceKind.BENCHMARK,
        purpose="official",
        scope=SCOPE,
        source_request=core.RequestId(root="request-official"),
        candidate=REVISION,
        observation_sequence=1,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        provenance="trusted",
        status=core.ObservationStatus.SUCCEEDED,
        reading=core.Reading(metric="throughput", value=value, direction="max"),
    )


@pytest.mark.regression
@RESTARTS
def test_the_result_handed_back_states_what_the_ledger_states_once_settled(
    *, restarting: bool
) -> None:
    """The measurement's record is the ledger's record as it stands when the comparison settled.

    The measurement ended stating the mean of one sample; core's close-call runs then landed and
    the ledger's record states the mean of all three. Before, the flow handed back the record
    as the measurement ended, so a search that ranked by it ranked by the earlier mean.
    """
    run = FlowRun.start(measuring_headline, Config(), restarting=restarting)
    asked = run.expect(core.Measure)
    run.view.changes["measurements"] = (benchmark(120.0),)
    show(run, 1, settling=True)
    request = asked.request
    assert isinstance(request, core.Measure)
    result = core.MeasurementResult(
        scope=request.scope, evidence=(benchmark(120.0),), status=core.ObservationStatus.SUCCEEDED
    )
    run.answer(
        asked,
        core.MeasurementFinished(decision=asked.decision, result=result, comparison=comparison(1)),
    )
    run.view.changes["measurements"] = (benchmark(118.0),)
    show(run, 3, settling=False)
    run.advance_to(LIMIT * 3)

    assert run.published(Headline) == Headline(value=118.0)


@pytest.mark.regression
@RESTARTS
@pytest.mark.parametrize("until", [7.0, 55.0])
def test_a_settling_comparison_is_read_when_core_stops_restating_its_record(
    *, restarting: bool, until: float
) -> None:
    """The flow's bound is the moment core stops restating the official record.

    Core states it (``StatedComparison.answer_until``); the flow reads the ledger's record then,
    and the record does not move after. Before, the flow kept its own bound, so a run that
    landed between the two restated a record the round had already taken its figure from.
    """
    run = FlowRun.start(measuring_headline, Config(), restarting=restarting)
    asked = run.expect(core.Measure)
    run.view.changes["measurements"] = (benchmark(120.0),)
    stated = core.StatedComparison(comparison=comparison(1), settling=True, answer_until=until)
    run.view.changes["comparisons"] = (stated,)
    request = asked.request
    assert isinstance(request, core.Measure)
    result = core.MeasurementResult(
        scope=request.scope, evidence=(benchmark(120.0),), status=core.ObservationStatus.SUCCEEDED
    )
    run.answer(
        asked,
        core.MeasurementFinished(decision=asked.decision, result=result, comparison=comparison(1)),
    )
    assert run.sleeping_until == (until,)
    run.view.changes["measurements"] = (benchmark(118.0),)
    run.advance_to(until)

    assert run.published(Headline) == Headline(value=118.0)
