"""Filesystem contract tests for the unified agent-run state store."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from tests.support import make_orchestrator_plan

from vibesys.loops.agent.hypotheses import reproject_run_evidence
from vibesys.loops.agent.model import (
    AgentRunState,
    Hypothesis,
    HypothesisResolution,
    HypothesisStrategy,
)
from vibesys.loops.agent.state import AgentRunStateStore
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.schemas import HypothesisStrategyUpdate, OrchestratorPlan
from vs_loop_state.api import RoundRecord
from vs_project.api import (
    PlainRunConfiguration,
    Project,
    RunEnvironmentRecord,
    StateTransition,
)


class _LegacyHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    claim: str | None = None
    task: str | None = None
    started_round: int
    rounds: list[int] = Field(default_factory=list)
    parent_round: int | None = None
    parent_commit: str | None = None
    declared_outcome: str | None = None
    review: str = "pending"
    resolution: str | None = None
    measurement: dict[str, object] | None = None
    candidate_retained: bool | None = None
    strategy: Literal["active", "completed", "parked", "abandoned"] = "active"
    strategy_reason: str | None = None


class _LegacyLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    hypotheses: list[_LegacyHypothesis] = Field(default_factory=list)


class _LegacyActive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    plan: OrchestratorPlan
    started_round: int
    parent_round: int | None = None
    parent_commit: str | None = None
    feedback: str | None = None
    next_step: str | None = None
    continuation_rounds: int = 0
    revert_applied: bool = False
    revert_commit: str | None = None
    gate_revalidation_pending: bool = False
    gate_approved_perf_metric: float | None = None
    gate_approved_perf_unit: str | None = None
    gate_approved_metrics: dict[str, float] = Field(default_factory=dict)
    gate_approved_evaluation_artifact: str | None = None
    gate_approved_candidate_disposition: str = "unassessed"
    gate_approved_candidate_metrics: dict[str, float] = Field(default_factory=dict)
    gate_approved_candidate_evaluation_artifact: str | None = None
    gate_approved_candidate_operating_point: str = ""
    gate_approved_candidate_retention_reason: str = ""
    gate_candidate_commit: str | None = None
    gate_accuracy_passed: bool = False


def _ops_space() -> MetricSpace:
    """The space a run measuring ``ops`` declares in its objectives file."""
    return MetricSpace(objectives=(Objective(name="ops", direction="max"),))


def _project(tmp_path: Path) -> Project:
    project = Project.open(tmp_path)
    project.state.create_project("test")
    run = project.state.new_run_manifest(
        "test",
        run_id="run-1",
        branch="vibesys/run-1",
        vibesys_version="test",
        trusted_input_baseline="a" * 40,
        configuration=PlainRunConfiguration(
            outer_loop="plain",
            run_environment=RunEnvironmentRecord(name="local"),
            agent_backend="stub",
            compute_backend="cpu",
            max_rounds=2,
            max_attempts_per_issue=1,
            max_issues_per_perf_eval=1,
        ),
    )
    project.state.create_run(run)
    return project


def _plan(identifier: str) -> OrchestratorPlan:
    return make_orchestrator_plan(
        hypothesis_id=identifier,
        hypothesis=f"claim {identifier}",
        task=f"implement {identifier}",
        criteria="tests pass",
        reasoning="test the claim",
    )


def test_store_round_trips_and_prepares_exact_state_transition(tmp_path: Path) -> None:
    project = _project(tmp_path)
    namespace = project.state.portable_namespace("run-1", "agent")
    store = AgentRunStateStore(namespace)
    state = AgentRunState(
        active_hypothesis_id="H-1",
        hypotheses=[Hypothesis(hypothesis_id="H-1", plan=_plan("H-1"), started_round=1)],
    )

    assert store.load_optional() is None
    transition = store.transition(state)
    assert isinstance(transition, StateTransition)
    assert store.load_optional() is None

    store.apply_transition(transition)
    assert store.load() == state


def test_legacy_migration_unifies_ledger_active_and_rounds_without_writing(tmp_path: Path) -> None:
    project = _project(tmp_path)
    portable = project.state.portable_namespace("run-1", "agent")
    local = project.state.local_namespace("run-1", "agent")
    portable.save(
        "hypotheses.json",
        _LegacyLedger(
            hypotheses=[
                _LegacyHypothesis(
                    hypothesis_id="H-1",
                    claim="remove contention",
                    task="shard the counter",
                    started_round=1,
                    rounds=[1],
                    strategy="parked",
                    strategy_reason="superseded",
                ),
                _LegacyHypothesis(
                    hypothesis_id="H-2",
                    claim="batch writes",
                    task="batch producer writes",
                    started_round=2,
                ),
            ]
        ),
    )
    local.save(
        "active.json",
        _LegacyActive(
            plan=_plan("H-2"),
            started_round=2,
            parent_round=1,
            parent_commit="a" * 40,
            feedback="measure the producer path",
            next_step="increase the batch size",
            continuation_rounds=1,
            gate_revalidation_pending=True,
        ),
    )
    first = RoundRecord(
        round_number=1,
        commit="a" * 40,
        perf_metric=100.0,
        perf_unit="ops",
        passed=True,
        reviewed=True,
        hypothesis_id="H-1",
        hypothesis_claim="remove contention",
        hypothesis_task="shard the counter",
        hypothesis_outcome="proven",
        official_evaluation=True,
    )
    project.state.save_round("run-1", first)
    store = AgentRunStateStore(portable)

    migrated = store.migrate_legacy(
        rounds=[first],
        local_namespace=local,
        legacy_space=_ops_space(),
    )

    assert store.load_optional() is None
    old = migrated.by_id("H-1")
    active = migrated.active_hypothesis
    assert old is not None
    assert old.strategy is HypothesisStrategy.PARKED
    assert old.rounds == [first]
    assert active is not None
    assert active.hypothesis_id == "H-2"
    assert active.feedback == "measure the producer path"
    assert active.next_step == "increase the batch size"
    assert active.continuation_rounds == 1
    assert active.gate_revalidation_pending is True

    store.save(migrated)
    store.cleanup_legacy(
        round_numbers=[1],
        local_namespace=local,
    )

    assert store.load() == migrated
    assert portable.load_optional("hypotheses.json", _LegacyLedger) is None
    assert local.load_optional("active.json", _LegacyActive) is None
    assert project.state.load_rounds("run-1") == []


def test_existing_unified_state_wins_over_legacy_inputs(tmp_path: Path) -> None:
    project = _project(tmp_path)
    portable = project.state.portable_namespace("run-1", "agent")
    local = project.state.local_namespace("run-1", "agent")
    store = AgentRunStateStore(portable)
    unified = AgentRunState(
        hypotheses=[Hypothesis(hypothesis_id="H-1", plan=_plan("H-1"), started_round=1)]
    )
    store.save(unified)

    assert (
        store.migrate_legacy(rounds=[], local_namespace=local, legacy_space=MetricSpace())
        == unified
    )


def test_unified_state_written_before_a_metric_space_adopts_the_legacy_one(tmp_path: Path) -> None:
    """State that declares no space is reprojected in the caller's fallback."""
    project = _project(tmp_path)
    portable = project.state.portable_namespace("run-1", "agent")
    local = project.state.local_namespace("run-1", "agent")
    store = AgentRunStateStore(portable)
    baseline = RoundRecord(
        round_number=1,
        commit="a" * 40,
        perf_metric=100.0,
        perf_unit="ops",
        passed=True,
        reviewed=True,
        hypothesis_id="H-1",
        hypothesis_outcome="proven",
        official_evaluation=True,
    )
    regression = RoundRecord(
        round_number=2,
        commit="b" * 40,
        perf_metric=90.0,
        perf_unit="ops",
        passed=True,
        reviewed=True,
        hypothesis_id="H-1",
        hypothesis_outcome="proven",
        hypothesis_parent_round=1,
        official_evaluation=True,
    )
    without_direction = reproject_run_evidence(
        AgentRunState(
            hypotheses=[
                Hypothesis(
                    hypothesis_id="H-1",
                    plan=_plan("H-1"),
                    started_round=1,
                    rounds=[baseline, regression],
                )
            ]
        )
    )
    store.save(without_direction)

    reprojected = store.migrate_legacy(
        rounds=[],
        local_namespace=local,
        legacy_space=_ops_space(),
    )

    hypothesis = reprojected.by_id("H-1")
    assert hypothesis is not None
    assert hypothesis.resolution is HypothesisResolution.DISPROVEN
    assert hypothesis.candidate_retained is False
    assert hypothesis.measurement is not None
    assert hypothesis.measurement.direction == "max"


def test_stale_legacy_ledger_active_without_checkpoint_is_not_resurrected(tmp_path: Path) -> None:
    project = _project(tmp_path)
    portable = project.state.portable_namespace("run-1", "agent")
    local = project.state.local_namespace("run-1", "agent")
    portable.save(
        "hypotheses.json",
        _LegacyLedger(
            hypotheses=[
                _LegacyHypothesis(
                    hypothesis_id="H-1",
                    started_round=1,
                    rounds=[1],
                    strategy="active",
                )
            ]
        ),
    )
    completed = RoundRecord(
        round_number=1,
        commit="a" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=True,
        reviewed=True,
        hypothesis_id="H-1",
        hypothesis_outcome="proven",
    )

    migrated = AgentRunStateStore(portable).migrate_legacy(
        rounds=[completed],
        local_namespace=local,
        legacy_space=MetricSpace(),
    )

    assert migrated.active_hypothesis_id is None
    assert migrated.active_hypothesis is None


def test_legacy_migration_normalizes_rounds_without_hypothesis_ids(tmp_path: Path) -> None:
    project = _project(tmp_path)
    portable = project.state.portable_namespace("run-1", "agent")
    local = project.state.local_namespace("run-1", "agent")
    record = RoundRecord(
        round_number=1,
        commit="a" * 40,
        perf_metric=10.0,
        perf_unit="ops",
        passed=True,
    )

    migrated = AgentRunStateStore(portable).migrate_legacy(
        rounds=[record],
        local_namespace=local,
        legacy_space=_ops_space(),
    )

    assert len(migrated.hypotheses) == 1
    assert migrated.hypotheses[0].hypothesis_id == "legacy-round-1"
    assert migrated.hypotheses[0].rounds[0].hypothesis_id == "legacy-round-1"


def test_legacy_migration_replays_strategy_updates_from_active_plan(tmp_path: Path) -> None:
    project = _project(tmp_path)
    portable = project.state.portable_namespace("run-1", "agent")
    local = project.state.local_namespace("run-1", "agent")
    portable.save(
        "hypotheses.json",
        _LegacyLedger(
            hypotheses=[
                _LegacyHypothesis(
                    hypothesis_id="H-1",
                    started_round=1,
                    rounds=[1],
                    strategy="completed",
                )
            ]
        ),
    )
    first = RoundRecord(
        round_number=1,
        commit="a" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=True,
        hypothesis_id="H-1",
    )
    active_plan = _plan("H-2").model_copy(
        update={
            "hypothesis_updates": [
                HypothesisStrategyUpdate(
                    hypothesis_id="H-1",
                    disposition="abandoned",
                    reason="A better direction supersedes it.",
                )
            ]
        }
    )
    local.save(
        "active.json",
        _LegacyActive(plan=active_plan, started_round=2, parent_round=1),
    )

    migrated = AgentRunStateStore(portable).migrate_legacy(
        rounds=[first],
        local_namespace=local,
        legacy_space=MetricSpace(),
    )

    prior = migrated.by_id("H-1")
    assert prior is not None
    assert prior.strategy is HypothesisStrategy.ABANDONED
    assert prior.strategy_reason == "A better direction supersedes it."
    assert migrated.active_hypothesis_id == "H-2"
