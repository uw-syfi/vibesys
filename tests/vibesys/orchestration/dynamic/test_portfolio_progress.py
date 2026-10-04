"""Planner evidence compares complete directions with trusted benchmark gaps."""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.evaluation_scenarios import ScenarioOutcome, ScenarioSpec, capture_projection
from tests.vibesys.orchestration.dynamic._support import (
    Script,
    dynamic_options,
    implementation,
    portfolio,
)

from vibesys.metrics import MetricComparison
from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.input_gate import InputGate
from vibesys.orchestration.dynamic.models import (
    DynamicState,
    DynamicWorkstream,
    EvaluationResult,
    PortfolioPlan,
    VerifiedCandidate,
    WorkstreamPhase,
    WorkstreamPlan,
)
from vibesys.orchestration.dynamic.parents.api import ParentCatalog, ParentSnapshot, ingest
from vibesys.orchestration.dynamic.prompts import render_portfolio
from vibesys.orchestration.dynamic.rounds import Rounds
from vs_evaluation.api import EvidenceKind
from vs_runtime.api import (
    AccuracyEvaluation,
    AgentCapability,
    AgentEvaluation,
    AgentEvaluationStage,
    AgentEvaluationStageOutcome,
    AgentEvaluationStatus,
    BenchmarkEvaluation,
    BenchmarkFailureKind,
    MetricDirection,
    PartialMeasurement,
    RunFacts,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


async def _commit(_label: str) -> None:
    pass


def _rounds(state: DynamicState) -> Rounds:
    options = dynamic_options()
    lock = asyncio.Lock()
    gate = InputGate(FakeRun(PLUGIN), options, state, lock=lock, commit=_commit)
    return Rounds(options, state, gate, lock=lock, commit=_commit)


def _iteration(identifier: str, sequence: int, value: float) -> DynamicWorkstream:
    plan = PortfolioPlan.model_validate(portfolio(identifier)).workstreams[0]
    assert isinstance(plan, WorkstreamPlan)
    return DynamicWorkstream(
        hypothesis_id=identifier,
        sequence=sequence,
        planning_call=sequence,
        plan=plan,
        parent_revision="root",
        evaluation=EvaluationResult(
            revision=f"revision-{sequence}",
            accuracy_passed=True,
            benchmark_passed=False,
            partial_measurement=PartialMeasurement(
                name="warmup_rate", value=value, target=79.7, direction="max", unit="items/s"
            ),
        ),
    )


def test_parent_projection_preserves_global_comparable_catalog_order() -> None:
    """A later producer's better partial precedes an earlier producer's comparable row."""
    state = DynamicState(workstreams=[_iteration("a", 1, 14), _iteration("b", 2, 38)])
    rounds = _rounds(state)
    catalog = ParentCatalog()
    for identifier, ordinal, value in (("a", 1, 14), ("b", 2, 38)):
        evaluation = capture_projection(
            ScenarioSpec(
                outcome=ScenarioOutcome.CORRECTNESS_FAIL,
                revision=f"revision-{ordinal}",
                patch=f"patch-{ordinal}",
                benchmark_failure=True,
                partial=PartialMeasurement(
                    name="warmup_rate", value=value, target=79.7, direction="max", unit="items/s"
                ),
            )
        )
        assert evaluation.handle_id is not None
        assert evaluation.content_digest is not None
        accuracy = next(
            row for row in evaluation.trusted_evidence if row.kind is EvidenceKind.ACCURACY
        )
        benchmark = next(
            row for row in evaluation.trusted_evidence if row.kind is EvidenceKind.BENCHMARK
        )
        catalog = ingest(
            catalog,
            ParentSnapshot(
                hypothesis_id=identifier,
                generation=ordinal,
                revision=evaluation.revision,
                content_digest=evaluation.content_digest,
                handle_id=evaluation.handle_id,
                submission_index=1,
                accuracy=accuracy,
                benchmark=benchmark,
                retained=True,
            ),
        )
    rounds.parents = catalog
    offered = rounds.buildable()
    assert [row.hypothesis_id for row in offered] == ["b", "a"]
    assert [row.best_partial for row in offered] == [True, False]


def test_flat_continuations_and_children_expose_the_gap_outside_compact_history() -> None:
    """r20b regression: the planner must see 13, 14, 14.5 against 79.7 together."""
    state = DynamicState(workstreams=[_iteration("fifo", 1, 13)])
    rounds = _rounds(state)
    second = _iteration("fifo", 2, 14)
    second.measured_iterations = rounds.measured_iterations(state.workstreams[0])
    state.workstreams[0] = second
    child = _iteration("cache", 3, 14.5)
    child.plan.parent_hypothesis_id = "fifo"
    state.workstreams.append(child)
    grandchild = _iteration("batch", 4, 14)
    grandchild.plan.parent_hypothesis_id = "cache"
    state.workstreams.append(grandchild)
    # Enough other directions to evict the original direction from compact rows.
    state.workstreams.extend(_iteration(f"other-{i}", i, 1) for i in range(5, 23))

    view = rounds.portfolio_view()
    gap = view.gaps[0]
    assert gap.best_value == 14.5
    assert gap.required_value == 79.7
    assert gap.required_ratio == pytest.approx(79.7 / 14.5)
    trend = next(item for item in view.trends if item.hypothesis_id == "fifo")
    assert [item.value for item in trend.iterations] == [13, 14, 14.5, 14]
    assert [item.revision for item in trend.iterations] == [f"revision-{i}" for i in range(1, 5)]
    history = rounds.planner_context()["history"]
    assert isinstance(history, str)
    assert "fifo" not in history
    prompt = render_portfolio(
        capacity=1,
        in_flight=0,
        remaining=1,
        objective="Improve the measured objective.",
        environment_notes="",
        skills=(),
        root_revision="root",
        parent_offer_snapshot="fixture-parent-offer",
        parent_base_accuracy=None,
        profiling=True,
        **rounds.planner_context(),
    )
    assert "Best achieved: 14.5; pass requires: 79.7" in prompt
    assert "iteration 1, hypothesis `fifo`, revision `revision-1`: warmup_rate = 13.0" in prompt


@given(
    values=st.lists(st.integers(min_value=1, max_value=1000), min_size=1, max_size=24),
    direction=st.sampled_from(list(MetricDirection)),
    target=st.integers(min_value=1, max_value=1000),
    failed_continuations=st.integers(min_value=0, max_value=4),
    accuracy_failed=st.booleans(),
)
@example(
    values=[13, 14, 14],
    direction=MetricDirection.MAXIMIZE,
    target=80,
    failed_continuations=2,
    accuracy_failed=True,
)
def test_gap_and_trend_preserve_all_iterations_for_either_direction(
    *,
    values: list[int],
    direction: MetricDirection,
    target: int,
    failed_continuations: int,
    accuracy_failed: bool,
) -> None:
    state = DynamicState()
    rounds = _rounds(state)
    sequence = 0
    measured_sequences = []
    for value in values:
        sequence += 1
        measured_sequences.append(sequence)
        current = _iteration("direction", sequence, value)
        assert current.evaluation is not None
        assert current.evaluation.partial_measurement is not None
        current.evaluation = current.evaluation.model_copy(
            update={
                "revision": "same-verified-revision",
                "partial_measurement": current.evaluation.partial_measurement.model_copy(
                    update={"direction": direction.value, "target": float(target)}
                ),
            }
        )
        if state.workstreams:
            current.measured_iterations = rounds.measured_iterations(state.workstreams[0])
        current.verified = VerifiedCandidate(
            revision=current.evaluation.revision,
            content_digest="a" * 64,
            observation_sequence=sequence,
            benchmark_passed=False,
            partial_measurement=current.evaluation.partial_measurement,
        )
        state.workstreams[:] = [current]
        for _ in range(failed_continuations):
            sequence += 1
            failed = _iteration("direction", sequence, value)
            assert failed.evaluation is not None
            failed.evaluation = (
                failed.evaluation.model_copy(update={"accuracy_passed": False})
                if accuracy_failed
                else None
            )
            failed.phase = WorkstreamPhase.FAILED
            failed.verified = current.verified
            failed.measured_iterations = rounds.measured_iterations(state.workstreams[0])
            state.workstreams[:] = [failed]
    # Serialization is the same boundary used by resume.
    view = _rounds(DynamicState.model_validate_json(state.model_dump_json())).portfolio_view()
    gap = view.gaps[0]
    best = max(values) if direction is MetricDirection.MAXIMIZE else min(values)
    assert gap.best_value == best
    expected_ratio = target / best if direction is MetricDirection.MAXIMIZE else best / target
    assert gap.required_ratio == pytest.approx(expected_ratio)
    assert [item.value for item in view.trends[0].iterations] == values
    assert [item.sequence for item in view.trends[0].iterations] == measured_sequences
    observations = [(item.sequence, item.revision, item.name) for item in view.trends[0].iterations]
    assert len(observations) == len(set(observations)) == len(values)


@pytest.mark.parametrize("disposition", ["parked", "abandoned"])
def test_retiring_a_direction_requires_a_typed_reason(disposition: str) -> None:
    plan = portfolio("new")
    plan["hypothesis_updates"] = [
        {"hypothesis_id": "flat", "disposition": disposition, "reason": "No measured progress."}
    ]
    with pytest.raises(ValidationError, match="reason_kind"):
        PortfolioPlan.model_validate(plan)
    plan["hypothesis_updates"][0]["reason_kind"] = "infeasible"
    parsed = PortfolioPlan.model_validate(plan)
    assert parsed.hypothesis_updates[0].reason_kind == "infeasible"


@given(
    reason=st.text().filter(
        lambda value: (
            value not in {"infeasible", "falsified", "blocked", "superseded", "lower_priority"}
        )
    )
)
def test_planner_rejects_unknown_reason_kinds(reason: str) -> None:
    plan = portfolio("new")
    plan["hypothesis_updates"] = [
        {
            "hypothesis_id": "old",
            "disposition": "parked",
            "reason_kind": reason,
            "reason": "Measured evidence.",
        }
    ]
    with pytest.raises(ValidationError, match="reason_kind"):
        PortfolioPlan.model_validate(plan)


@pytest.mark.parametrize(("value", "target"), [(0.0, 80.0), (1e-300, 1e300), (-1.0, 80.0)])
def test_an_undefined_multiplicative_gap_still_reports_the_measurement(
    value: float, target: float
) -> None:
    item = _iteration("direction", 1, value)
    assert item.evaluation is not None
    partial = PartialMeasurement(name="warmup_rate", value=value, target=target, direction="max")
    item.evaluation = item.evaluation.model_copy(update={"partial_measurement": partial})
    rounds = _rounds(DynamicState(workstreams=[item]))
    gap = rounds.portfolio_view().gaps[0]
    assert gap.best_value == value
    assert gap.required_value == target
    assert gap.required_ratio is None
    prompt = render_portfolio(
        capacity=1,
        in_flight=0,
        remaining=1,
        objective="Improve the measured objective.",
        environment_notes="",
        skills=(),
        root_revision="root",
        parent_offer_snapshot="fixture-parent-offer",
        parent_base_accuracy=None,
        profiling=True,
        **rounds.planner_context(),
    )
    assert "needed improvement ratio: undefined." in prompt
    assert "nonpositive" not in prompt


def test_distinct_workload_names_units_and_targets_are_not_silently_compared() -> None:
    first = _iteration("one", 1, 13)
    second = _iteration("two", 2, 100)
    third = _iteration("three", 3, 200)
    fourth = _iteration("four", 4, 14)
    for item, name, unit, target in (
        (second, "headline_rate", "items/s", 79.7),
        (third, "warmup_rate", "items/min", 79.7),
        (fourth, "warmup_rate", "items/s", 100),
    ):
        assert item.evaluation is not None
        assert item.evaluation.partial_measurement is not None
        item.evaluation = item.evaluation.model_copy(
            update={
                "partial_measurement": PartialMeasurement(
                    name=name,
                    unit=unit,
                    target=target,
                    value=item.evaluation.partial_measurement.value,
                    direction="max",
                )
            }
        )
    view = _rounds(DynamicState(workstreams=[first, second, third, fourth])).portfolio_view()
    gaps = {(gap.name, gap.unit, gap.required_value): gap.best_value for gap in view.gaps}
    assert gaps == {
        ("warmup_rate", "items/s", 79.7): 13,
        ("warmup_rate", "items/s", 100): 14,
        ("headline_rate", "items/s", 79.7): 100,
        ("warmup_rate", "items/min", 79.7): 200,
    }


def test_failed_accuracy_does_not_hide_the_previous_verified_measurement() -> None:
    item = _iteration("direction", 1, 14.5)
    assert item.evaluation is not None

    item.verified = VerifiedCandidate(
        revision="previous-verified",
        content_digest="a" * 64,
        benchmark_passed=False,
        partial_measurement=item.evaluation.partial_measurement,
    )
    item.evaluation = item.evaluation.model_copy(update={"accuracy_passed": False})
    view = _rounds(DynamicState(workstreams=[item])).portfolio_view()
    assert view.gaps[0].best_value == 14.5
    assert [row.revision for row in view.trends[0].iterations] == ["previous-verified"]


@pytest.mark.parametrize("accuracy_failed", [False, True], ids=["blocked", "accuracy-failed"])
@pytest.mark.parametrize("observation_sequence", [None, 1], ids=["legacy", "recorded"])
def test_failed_continuation_keeps_the_verified_observations_original_sequence(
    *,
    accuracy_failed: bool,
    observation_sequence: int | None,
) -> None:
    first = _iteration("direction", 1, 13)
    assert first.evaluation is not None
    first.verified = VerifiedCandidate(
        revision=first.evaluation.revision,
        content_digest="a" * 64,
        benchmark_passed=False,
        partial_measurement=first.evaluation.partial_measurement,
        metric_name="throughput",
        metric_value=14,
        metric_direction=MetricDirection.MAXIMIZE,
    )
    if observation_sequence is not None:
        first.verified = first.verified.model_copy(
            update={"observation_sequence": observation_sequence}
        )
    state = DynamicState(workstreams=[first])
    rounds = _rounds(state)
    original = rounds.measured_iterations(first)
    failed = _iteration("direction", 2, 1000)
    assert failed.evaluation is not None
    failed.evaluation = (
        failed.evaluation.model_copy(update={"accuracy_passed": False}) if accuracy_failed else None
    )
    failed.phase = WorkstreamPhase.FAILED
    failed.verified = first.verified
    failed.measured_iterations = original
    state.workstreams[:] = [failed]

    resumed = _rounds(DynamicState.model_validate_json(state.model_dump_json()))
    assert resumed.portfolio_view().trends[0].iterations == original
    # Legacy measurements remain historical facts, but no missing canonical receipt
    # can grant a new buildable parent.
    assert resumed.buildable() == ()


@given(values=st.lists(st.integers(min_value=1, max_value=1000), min_size=2, max_size=12))
def test_legacy_candidate_recovers_the_latest_matching_observation(values: list[int]) -> None:
    state = DynamicState()
    rounds = _rounds(state)
    for sequence, value in enumerate(values, start=1):
        current = _iteration("direction", sequence, value)
        assert current.evaluation is not None
        current.evaluation = current.evaluation.model_copy(update={"revision": "same-revision"})
        if state.workstreams:
            current.measured_iterations = rounds.measured_iterations(state.workstreams[0])
        state.workstreams[:] = [current]
    original = rounds.measured_iterations(current)
    assert current.evaluation is not None
    failed = current.model_copy(
        update={
            "sequence": len(values) + 1,
            "evaluation": None,
            "phase": WorkstreamPhase.FAILED,
            "measured_iterations": original,
            "verified": VerifiedCandidate(
                revision="same-revision",
                content_digest="a" * 64,
                partial_measurement=current.evaluation.partial_measurement,
            ),
        }
    )
    assert rounds.measured_iterations(failed) == original


@pytest.mark.parametrize("values", [(13, 14), (13, 13)], ids=["changed-value", "repeated-value"])
def test_new_agent_measurement_on_the_same_revision_keeps_its_own_sequence(
    tmp_path: Path, values: tuple[int, int]
) -> None:
    continuation = PortfolioPlan.model_validate(portfolio("direction", continue_hypothesis=True))
    assert isinstance(continuation.workstreams[0], WorkstreamPlan)
    continuation.workstreams[0].task = "Remove the recorded benchmark blocker."
    script = Script(
        {
            ORCHESTRATOR.id: [
                portfolio("direction"),
                continuation.model_dump(),
            ],
            IMPLEMENTER.id: [
                {"summary": "Partial trusted evidence.", "outcome": "blocked", "evidence": []}
            ]
            * 2,
        }
    )
    runs: list[FakeRun] = []
    revisions: list[str] = []
    measurements: list[int] = []

    def respond(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        if role.id == IMPLEMENTER.id:
            run = runs[0]
            workspace = run.workspaces.candidates[-1]
            if not revisions:
                assert workspace.revision is not None
                revisions.append(workspace.revision)
            revision = revisions[0]
            value = values[len(measurements)]
            measurements.append(value)
            run.evaluation.record_agent_evaluation(
                workspace,
                AgentEvaluation(
                    revision=revision,
                    content_digest=hashlib.sha256(f"patch for {revision}".encode()).hexdigest(),
                    kinds=("accuracy", "benchmark"),
                    status=AgentEvaluationStatus.FAILED,
                    failure="benchmark below its gate",
                    stages=(
                        AgentEvaluationStage(
                            kind="accuracy", outcome=AgentEvaluationStageOutcome.PASSED
                        ),
                        AgentEvaluationStage(
                            kind="benchmark",
                            outcome=AgentEvaluationStageOutcome.FAILED,
                            partial_measurement=PartialMeasurement(
                                name="warmup_rate", value=value, target=79.7, direction="max"
                            ),
                        ),
                    ),
                ),
            )
        return script.respond(role, history, message, response)

    async def scenario() -> DynamicState:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            responder=respond,
            supports_parallel_candidates=True,
            facts=RunFacts(domain_id="generic", objective="Improve.", accuracy_configured=True),
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        runs.append(run)
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=2, max_in_flight=1))
        state = await run.state.load(DynamicState)
        assert state is not None
        return state

    state = asyncio.run(scenario())
    rows = _rounds(state).portfolio_view().trends[0].iterations
    assert [(row.sequence, row.value) for row in rows] == [(1, values[0]), (2, values[1])]
    assert {row.revision for row in rows} == set(revisions)


def test_failed_accuracy_cannot_restore_a_headline_measurement_from_round_history(
    tmp_path: Path,
) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("incorrect")],
            IMPLEMENTER.id: [implementation("incorrect")],
            JUDGE.id: [{"passed": True, "analysis": "Reviewable."}],
        }
    )

    async def scenario() -> DynamicState:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            responder=script.respond,
            supports_parallel_candidates=True,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve.",
                accuracy_configured=True,
                benchmark_configured=True,
            ),
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        run.evaluation.script_accuracy(AccuracyEvaluation(executed=True, feedback="Incorrect."))
        run.evaluation.script_benchmark(
            *(
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=value,
                    metric_direction=MetricDirection.MAXIMIZE,
                    row={"throughput": value},
                )
                for value in [1, 1000]
            )
        )
        await PLUGIN.orchestrate(run, dynamic_options(official_eval_every=1, max_in_flight=1))
        state = await run.state.load(DynamicState)
        assert state is not None
        return state

    state = asyncio.run(scenario())
    evaluation = state.workstreams[0].evaluation
    assert evaluation is not None
    assert evaluation.accuracy_passed is False
    assert evaluation.metric_value == 1000
    record = state.search.rounds[0]
    assert record.perf_metric is None
    assert record.metrics == {}
    assert _rounds(state).portfolio_view().trends[0].iterations == ()
    # A saved round written before the fix still has its authoritative failed evaluation.
    record.perf_metric = 1000
    record.perf_unit = "throughput"
    record.perf_direction = "max"
    record.perf_provenance = "framework"
    record.perf_comparison = MetricComparison.BETTER
    resumed = DynamicState.model_validate_json(state.model_dump_json())
    assert _rounds(resumed).portfolio_view().trends[0].iterations == ()


def test_dispatch_retains_partial_iterations_lineage_and_infeasibility_reason(
    tmp_path: Path,
) -> None:
    child = PortfolioPlan.model_validate(portfolio("child"))
    assert isinstance(child.workstreams[0], WorkstreamPlan)
    child.workstreams[0].parent_hypothesis_id = "direction"
    retire = portfolio("next")
    retire["hypothesis_updates"] = [
        {
            "hypothesis_id": "direction",
            "disposition": "parked",
            "reason_kind": "infeasible",
            "reason": "Three measured iterations are flat relative to the required gap.",
        }
    ]
    script = Script(
        {
            ORCHESTRATOR.id: [
                portfolio("direction"),
                portfolio("direction", continue_hypothesis=True),
                child.model_dump(),
                retire,
            ],
            IMPLEMENTER.id: [
                implementation("direction"),
                implementation("direction"),
                implementation("child"),
                {"summary": "No next mechanism.", "outcome": "blocked", "evidence": []},
            ],
            JUDGE.id: [{"passed": True, "analysis": "Correct."}] * 3,
        }
    )

    async def scenario() -> DynamicState:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            responder=script.respond,
            supports_parallel_candidates=True,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve.",
                accuracy_configured=True,
                benchmark_configured=True,
            ),
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        measurements = [
            BenchmarkEvaluation(
                executed=True,
                feedback="Below the trusted gate.",
                failure_kind=BenchmarkFailureKind.WORKLOAD,
                partial_measurement=PartialMeasurement(
                    name="warmup_rate", value=value, target=79.7, direction="max", unit="items/s"
                ),
            )
            for value in [1, 13, 14, 14.5]
        ]
        run.evaluation.script_benchmark(*measurements)
        run.evaluation.script_accuracy(*[AccuracyEvaluation(executed=True) for _ in range(3)])
        await PLUGIN.orchestrate(
            run, dynamic_options(max_rounds=4, max_in_flight=1, official_eval_every=1)
        )
        state = await run.state.load(DynamicState)
        assert state is not None
        return state

    state = asyncio.run(scenario())
    direction = next(item for item in state.workstreams if item.hypothesis_id == "direction")
    assert direction.strategy_reason_kind == "infeasible"
    assert [row.value for row in direction.measured_iterations] == [13]
    assert state.workstreams[1].lineage_parent_id == "direction"
    planner = [message for role, _, message in script.calls if role == ORCHESTRATOR.id]
    assert "Best achieved: 14.5; pass requires: 79.7" in planner[3]
    view = _rounds(state).portfolio_view()
    assert [row.value for row in view.trends[0].iterations] == [13, 14, 14.5]
    history = _rounds(state).planner_context()["history"]
    assert isinstance(history, str)
    assert "infeasible" in history


@given(parents=st.lists(st.integers(min_value=0, max_value=100), min_size=1, max_size=20))
def test_each_direction_trend_contains_exactly_its_transitive_descendants(
    parents: list[int],
) -> None:
    state = DynamicState()
    lineage: dict[str, str | None] = {}
    for index, choice in enumerate(parents):
        identifier = f"direction-{index}"
        parent = (
            None if index == 0 or choice % (index + 1) == index else f"direction-{choice % index}"
        )
        lineage[identifier] = parent
        item = _iteration(identifier, index + 1, index + 1)
        item.plan.parent_hypothesis_id = parent
        state.workstreams.append(item)
    view = _rounds(state).portfolio_view()
    for trend in view.trends:
        descendants = []
        for identifier in lineage:
            current = identifier
            while current is not None and current != trend.hypothesis_id:
                current = lineage[current]
            if current == trend.hypothesis_id:
                descendants.append(identifier)
        assert [row.hypothesis_id for row in trend.iterations] == descendants
