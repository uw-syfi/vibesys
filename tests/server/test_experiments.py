"""Server projection tests for the authoritative agent-run aggregate."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

from tests.server.support import build_server_parts

from server.api.experiments import (
    ExperimentLoadToken,
    ExperimentProjection,
    ExperimentQueryResult,
    build_experiment_log,
)
from server.api.protocol import ExperimentCursor, ExperimentQuery, HypothesisEntry, PerformanceQuery
from server.events import EventType, ExperimentsChangedData
from vibesys.loops.agent.model import (
    AgentRunState,
    Hypothesis,
    HypothesisMeasurement,
    HypothesisResolution,
    HypothesisReview,
    HypothesisStrategy,
)
from vibesys.loops.agent.state import AgentRunStateStore
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    OrchestratorPlan,
    PerfDeltaReason,
)
from vs_loop_state import MetricComparison, PerfProvenance, RoundRecord
from vs_project import AgentRunConfiguration, Project, RunEnvironmentRecord

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class _RoundFields(TypedDict, total=False):
    """Keyword fields of ``RoundRecord``, so helper overrides stay checked."""

    round_number: int
    commit: str | None
    perf_metric: float | None
    perf_unit: str | None
    passed: bool
    profile_skipped: bool
    reviewed: bool
    hypothesis_id: str | None
    hypothesis_declared_outcome: str | None
    judge_verdict: Literal["pass", "fail", "deferred"] | None
    hypothesis_outcome: str | None
    hypothesis_claim: str | None
    hypothesis_task: str | None
    hypothesis_parent_round: int | None
    hypothesis_parent_commit: str | None
    metrics: dict[str, float]
    evaluation_artifact: str | None
    official_evaluation: bool
    official_evaluation_reason: str | None
    candidate_disposition: str
    candidate_metrics: dict[str, float]
    candidate_evaluation_artifact: str | None
    candidate_operating_point: str
    candidate_retention_reason: str
    candidate_retained: bool | None
    perf_direction: Literal["max", "min"] | None
    perf_baseline_round: int | None
    perf_baseline_commit: str | None
    perf_baseline_metric: float | None
    perf_delta_pct: float | None
    perf_comparison: MetricComparison | None
    perf_provenance: PerfProvenance | None


class _HypothesisFields(TypedDict, total=False):
    """Keyword fields of ``Hypothesis``, so helper overrides stay checked."""

    hypothesis_id: str
    plan: OrchestratorPlan
    started_round: int
    parent_round: int | None
    parent_commit: str | None
    rounds: list[RoundRecord]
    feedback: str | None
    next_step: str | None
    continuation_rounds: int
    revert_applied: bool
    revert_commit: str | None
    gate_revalidation_pending: bool
    gate_approved_perf_metric: float | None
    gate_approved_perf_unit: str | None
    gate_approved_metrics: dict[str, float]
    gate_approved_evaluation_artifact: str | None
    gate_approved_candidate_disposition: str
    gate_approved_candidate_metrics: dict[str, float]
    gate_approved_candidate_evaluation_artifact: str | None
    gate_approved_candidate_operating_point: str
    gate_approved_candidate_retention_reason: str
    gate_candidate_commit: str | None
    gate_accuracy_passed: bool
    declared_outcome: HypothesisOutcome | None
    review: HypothesisReview
    resolution: HypothesisResolution | None
    measurement: HypothesisMeasurement | None
    candidate_retained: bool | None
    strategy: HypothesisStrategy
    strategy_reason: str | None
    last_experiment_revision: int


def _round(number: int, **overrides: Unpack[_RoundFields]) -> RoundRecord:
    fields: _RoundFields = {
        "round_number": number,
        "commit": f"c{number}",
        "perf_metric": None,
        "perf_unit": None,
        "passed": False,
    }
    fields.update(overrides)
    return RoundRecord(**fields)


def _hypothesis(
    identifier: str, started_round: int, /, **overrides: Unpack[_HypothesisFields]
) -> Hypothesis:
    fields: _HypothesisFields = {
        "hypothesis_id": identifier,
        "plan": OrchestratorPlan(
            hypothesis_id=identifier,
            hypothesis=f"claim for {identifier}",
            task=f"test {identifier}",
            pass_criteria="",
            reasoning="",
        ),
        "started_round": started_round,
    }
    fields.update(overrides)
    return Hypothesis(**fields)


def test_round_carries_its_own_verdict_and_causal_delta() -> None:
    """Per-round review and delta cross once, on the experiment log's row."""
    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                rounds=[
                    _round(
                        1,
                        hypothesis_id="H-01",
                        judge_verdict="deferred",
                        perf_delta_pct=12.5,
                        hypothesis_outcome="proven",
                        candidate_disposition="pareto_frontier",
                    )
                ],
            )
        ]
    )

    (entry,) = build_experiment_log(state)
    (round_entry,) = entry.rounds

    assert round_entry.judge_verdict == "deferred"
    assert round_entry.perf_delta_pct == 12.5
    assert round_entry.hypothesis_outcome == HypothesisResolution.PROVEN
    assert round_entry.candidate_disposition == CandidateDisposition.PARETO_FRONTIER


def test_round_reads_an_implementer_outcome_from_its_own_vocabulary() -> None:
    """Both vocabularies reach a round record, and both stay closed sets."""
    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                rounds=[_round(1, hypothesis_id="H-01", hypothesis_outcome="nominated")],
            )
        ]
    )

    (entry,) = build_experiment_log(state)

    assert entry.rounds[0].hypothesis_outcome == HypothesisOutcome.NOMINATED


def test_round_drops_a_retired_outcome_rather_than_failing_the_log() -> None:
    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                rounds=[
                    _round(
                        1,
                        hypothesis_id="H-01",
                        hypothesis_outcome="retired_value",
                        candidate_disposition="retained",
                    )
                ],
            )
        ]
    )

    (entry,) = build_experiment_log(state)

    assert entry.rounds[0].hypothesis_outcome is None
    assert entry.rounds[0].candidate_disposition is None


def test_projection_uses_nested_rounds_and_one_official_measurement_tuple() -> None:
    hypothesis = _hypothesis(
        "H-01",
        1,
        rounds=[
            _round(1, hypothesis_id="H-01", perf_metric=100.0, perf_unit="ops_s"),
            _round(2, hypothesis_id="H-01", perf_metric=125.0, perf_unit="ops_s"),
        ],
        review=HypothesisReview.PASS,
        resolution=HypothesisResolution.DISPROVEN,
        measurement=HypothesisMeasurement(
            round=1,
            metric="throughput",
            value=100.0,
            unit="ops_s",
            direction="max",
            baseline_value=110.0,
            delta_pct=-9.09,
        ),
        candidate_retained=False,
        strategy=HypothesisStrategy.ABANDONED,
        strategy_reason="The official baseline regressed.",
    )

    (entry,) = build_experiment_log(AgentRunState(hypotheses=[hypothesis]))

    assert [round_.round for round_ in entry.rounds] == [1, 2]
    assert (entry.first_round, entry.last_round) == (1, 2)
    assert entry.resolved_outcome == "disproven"
    assert entry.judge_verdict == "pass"
    assert entry.kept is False
    assert entry.strategy_disposition == "abandoned"
    # Do not combine the newer second-round value with the official first-round
    # causal comparison.
    assert (entry.perf_metric, entry.perf_unit, entry.perf_delta_pct) == (
        100.0,
        "ops_s",
        -9.09,
    )
    assert (entry.perf_metric_name, entry.perf_direction, entry.perf_baseline_value) == (
        "throughput",
        "max",
        110.0,
    )


def test_projection_carries_baseline_identity_and_the_no_delta_reason() -> None:
    """Why a number has no delta crosses as a typed value, not as an absence."""
    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                measurement=HypothesisMeasurement(
                    round=1,
                    metric="throughput",
                    value=125.0,
                    unit="ops_s",
                    baseline_round=1,
                    baseline_commit="c1",
                    baseline_value=100.0,
                    delta_pct=25.0,
                ),
            ),
            _hypothesis(
                "H-02",
                2,
                measurement=HypothesisMeasurement(
                    round=2,
                    metric="throughput",
                    value=130.0,
                    unit="ops_s",
                    delta_reason=PerfDeltaReason.NO_BASELINE_YET,
                ),
            ),
            _hypothesis(
                "H-03",
                3,
                measurement=HypothesisMeasurement(
                    round=3,
                    metric="throughput",
                    value=140.0,
                    unit="ops_s",
                    delta_reason=PerfDeltaReason.BASELINE_UNRESOLVED,
                ),
            ),
            _hypothesis(
                "H-04",
                4,
                rounds=[
                    _round(
                        4,
                        hypothesis_id="H-04",
                        perf_metric=150.0,
                        perf_unit="ops_s",
                        official_evaluation=True,
                        perf_provenance="implementer",
                    )
                ],
            ),
        ]
    )

    measured, first, unresolved, reported = build_experiment_log(state)

    assert (measured.perf_baseline_round, measured.perf_baseline_commit) == (1, "c1")
    assert measured.perf_delta_reason is None
    assert first.perf_delta_reason is PerfDeltaReason.NO_BASELINE_YET
    assert unresolved.perf_delta_reason is PerfDeltaReason.BASELINE_UNRESOLVED
    assert (unresolved.perf_baseline_round, unresolved.perf_baseline_commit) == (None, None)
    # The self-reported number itself stays off the entry-level measurement
    # tuple; only the reason says a number exists that nobody trusted-measured.
    assert reported.perf_metric is None
    assert reported.perf_delta_reason is PerfDeltaReason.NOT_FRAMEWORK_MEASURED


def test_hypothesis_entry_round_trips_the_no_delta_reason() -> None:
    """The new fields survive the wire, and a legacy payload stays valid."""
    entry = HypothesisEntry(
        hypothesis_id="H-01",
        first_round=1,
        last_round=1,
        perf_delta_reason=PerfDeltaReason.BASELINE_UNRESOLVED,
        perf_baseline_round=3,
        perf_baseline_commit="abc1234deadbeef",
    )

    decoded = HypothesisEntry.model_validate_json(entry.model_dump_json())
    assert decoded.perf_delta_reason is PerfDeltaReason.BASELINE_UNRESOLVED
    assert (decoded.perf_baseline_round, decoded.perf_baseline_commit) == (3, "abc1234deadbeef")

    legacy = HypothesisEntry.model_validate(
        {"hypothesis_id": "H-02", "first_round": 1, "last_round": 1}
    )
    assert legacy.perf_delta_reason is None
    assert (legacy.perf_baseline_round, legacy.perf_baseline_commit) == (None, None)


def test_projection_surfaces_active_hypothesis_before_a_round_finishes() -> None:
    state = AgentRunState(
        active_hypothesis_id="H-02",
        hypotheses=[_hypothesis("H-02", 2)],
    )

    (entry,) = build_experiment_log(state)

    assert entry.active is True
    assert entry.rounds == []
    assert (entry.first_round, entry.last_round) == (2, 2)
    assert entry.claim == "claim for H-02"


def test_projection_uses_the_orchestrator_title_when_present() -> None:
    hypothesis = _hypothesis(
        "H-01",
        1,
        plan=OrchestratorPlan(
            hypothesis_id="H-01",
            title="Batch decode requests",
            hypothesis="claim for H-01",
            task="test H-01",
            pass_criteria="",
            reasoning="",
        ),
    )

    (entry,) = build_experiment_log(AgentRunState(hypotheses=[hypothesis]))

    assert entry.title == "Batch decode requests"


def test_projection_derives_a_title_from_the_claim_when_the_plan_title_is_empty() -> None:
    hypothesis = _hypothesis(
        "H-01",
        1,
        plan=OrchestratorPlan(
            hypothesis_id="H-01",
            hypothesis="Batching decode requests reduces overhead. More detail follows.",
            task="test H-01",
            pass_criteria="",
            reasoning="",
        ),
    )

    (entry,) = build_experiment_log(AgentRunState(hypotheses=[hypothesis]))

    assert entry.title == "Batching decode requests reduces overhead"


def test_projection_title_is_none_without_any_text() -> None:
    hypothesis = _hypothesis(
        "H-01",
        1,
        plan=OrchestratorPlan(
            hypothesis_id="H-01",
            hypothesis="",
            task="test H-01",
            pass_criteria="",
            reasoning="",
        ),
    )

    (entry,) = build_experiment_log(AgentRunState(hypotheses=[hypothesis]))

    assert entry.title is None


def test_projection_orders_hypotheses_by_started_round() -> None:
    state = AgentRunState(
        hypotheses=[
            _hypothesis("H-B", 3, rounds=[_round(3, hypothesis_id="H-B")]),
            _hypothesis("H-A", 1, rounds=[_round(1, hypothesis_id="H-A")]),
        ]
    )

    entries = build_experiment_log(state)

    assert [entry.hypothesis_id for entry in entries] == ["H-A", "H-B"]


def test_revisioned_projection_returns_only_changed_entries() -> None:
    projection = ExperimentProjection()
    initial = AgentRunState(
        experiment_revision=1,
        hypotheses=[
            _hypothesis("H-01", 1, last_experiment_revision=1),
            _hypothesis("H-02", 2, last_experiment_revision=1),
        ],
    )
    full = projection.replace("run", "projection", initial)

    assert full.update.reset is True
    assert [entry.hypothesis_id for entry in full.entries] == ["H-01", "H-02"]
    unchanged = projection.query(
        "run",
        "projection",
        ExperimentCursor(run_id="run", projection_id=full.update.projection_id, revision=1),
    )
    assert isinstance(unchanged, ExperimentQueryResult)
    assert unchanged.entries == []
    assert unchanged.update.reset is False

    changed = initial.clone()
    changed.experiment_revision = 2
    changed.hypotheses[1].last_experiment_revision = 2
    changed.hypotheses[1].plan.task = "changed task"
    projection.update("run", "projection", changed)
    delta = projection.query(
        "run",
        "projection",
        ExperimentCursor(run_id="run", projection_id=full.update.projection_id, revision=1),
    )

    assert isinstance(delta, ExperimentQueryResult)
    assert [entry.hypothesis_id for entry in delta.entries] == ["H-02"]
    assert delta.update.from_revision == 1
    assert delta.update.through_revision == 2


def test_revisioned_projection_combines_remove_then_recreate_in_order() -> None:
    projection = ExperimentProjection()
    original = AgentRunState(
        experiment_revision=1,
        hypotheses=[_hypothesis("H-01", 1, last_experiment_revision=1)],
    )
    full = projection.replace("run", "projection", original)
    projection.update("run", "projection", AgentRunState(experiment_revision=2))
    recreated = AgentRunState(
        experiment_revision=3,
        hypotheses=[
            _hypothesis(
                "H-01",
                3,
                last_experiment_revision=3,
                plan=_hypothesis("H-01", 1).plan.model_copy(update={"task": "new task"}),
            )
        ],
    )
    projection.update("run", "projection", recreated)

    delta = projection.query(
        "run",
        "projection",
        ExperimentCursor(run_id="run", projection_id=full.update.projection_id, revision=1),
    )

    assert isinstance(delta, ExperimentQueryResult)
    assert [entry.action for entry in delta.entries] == ["new task"]
    assert delta.update.removed_hypothesis_ids == []


def test_revisioned_projection_resets_for_a_history_gap_or_run_change() -> None:
    projection = ExperimentProjection(history_limit=1)
    first = AgentRunState(
        experiment_revision=1,
        hypotheses=[_hypothesis("H-01", 1, last_experiment_revision=1)],
    )
    full = projection.replace("run", "projection", first)
    second = first.clone()
    second.experiment_revision = 2
    second.hypotheses[0].last_experiment_revision = 2
    projection.update("run", "projection", second)
    third = second.clone()
    third.experiment_revision = 3
    third.hypotheses[0].last_experiment_revision = 3
    projection.update("run", "projection", third)

    gap = projection.query(
        "run",
        "projection",
        ExperimentCursor(run_id="run", projection_id=full.update.projection_id, revision=1),
    )

    assert isinstance(gap, ExperimentQueryResult)
    assert gap.update.reset is True
    assert isinstance(projection.query("other-run", "other-projection", None), ExperimentLoadToken)


def test_legacy_invalidation_forces_a_full_snapshot_at_the_same_revision() -> None:
    projection = ExperimentProjection()
    old = AgentRunState(hypotheses=[_hypothesis("H-01", 1)])
    original = projection.replace("run", "projection", old)
    stale_cursor = ExperimentCursor(
        run_id="run",
        projection_id=original.update.projection_id,
        revision=0,
    )
    projection.invalidate("run", "projection", None)

    assert isinstance(projection.query("run", "projection", stale_cursor), ExperimentLoadToken)

    new = old.clone()
    new.hypotheses[0].plan.task = "changed by a legacy writer"
    reset = projection.replace("run", "projection", new)
    assert reset.update.reset is True
    assert reset.entries[0].action == "changed by a legacy writer"
    assert reset.update.projection_id != stale_cursor.projection_id

    # A second client carrying the same old cursor must also receive the new
    # contents after the first client's authoritative reload populated cache.
    second_client = projection.query("run", "projection", stale_cursor)
    assert isinstance(second_client, ExperimentQueryResult)
    assert second_client.update.reset is True
    assert second_client.entries[0].action == "changed by a legacy writer"


def test_equal_revision_authoritative_snapshot_starts_a_new_cursor_chain() -> None:
    projection = ExperimentProjection()
    old = AgentRunState(
        experiment_revision=4,
        hypotheses=[_hypothesis("H-01", 1, last_experiment_revision=4)],
    )
    original = projection.replace("run", "projection", old)
    stale_cursor = ExperimentCursor(
        run_id="run",
        projection_id=original.update.projection_id,
        revision=4,
    )

    restored = old.clone()
    restored.hypotheses[0].plan.task = "restored contents"
    projection.update("run", "projection", restored, changed_keys=None)

    result = projection.query("run", "projection", stale_cursor)
    assert isinstance(result, ExperimentQueryResult)
    assert result.update.reset is True
    assert result.update.projection_id != stale_cursor.projection_id
    assert result.entries[0].action == "restored contents"


def test_invalidation_during_a_cold_load_rejects_the_stale_snapshot() -> None:
    projection = ExperimentProjection()
    token = projection.query("run", "projection", None)
    assert isinstance(token, ExperimentLoadToken)

    projection.invalidate("run", "projection", revision=2)
    stale = AgentRunState(
        experiment_revision=1,
        hypotheses=[_hypothesis("H-stale", 1, last_experiment_revision=1)],
    )

    assert projection.install_loaded("run", "projection", stale, token) is None
    assert isinstance(projection.query("run", "projection", None), ExperimentLoadToken)


def _configuration() -> AgentRunConfiguration:
    return AgentRunConfiguration(
        outer_loop="agent",
        inner_loop="single-agent",
        interface="inprocess",
        agent_backend="stub",
        compute_backend="cpu",
        profiler="none",
        max_rounds=3,
        max_retries_per_round=1,
        judge_every=1,
        official_eval_every=1,
        memory_layout="files",
        run_environment=RunEnvironmentRecord(name="local"),
    )


def _project_run(
    project: Path,
    configuration: AgentRunConfiguration | None = None,
) -> tuple[Project, str]:
    project.mkdir()
    (project / "OBJECTIVE.md").write_text("Make the queue fast.\n", encoding="utf-8")
    vibesys_project = Project.open(project)
    vibesys_project.state.create_project("queue")
    manifest = vibesys_project.state.new_run_manifest(
        "queue",
        run_id="queue-run",
        branch="vibesys/queue-run",
        vibesys_version="0.2.0-test",
        configuration=configuration or _configuration(),
        trusted_input_baseline="0" * 40,
    )
    vibesys_project.state.create_run(manifest)
    return vibesys_project, manifest.run_id


def test_service_reads_only_authoritative_agent_state(tmp_path: Path) -> None:
    project, run_id = _project_run(tmp_path / "project")
    portable = project.state.portable_namespace(run_id, "agent")
    AgentRunStateStore(portable).save(
        AgentRunState(
            active_hypothesis_id="H-02",
            hypotheses=[
                _hypothesis("H-01", 1, rounds=[_round(1, hypothesis_id="H-01")]),
                _hypothesis("H-02", 2),
            ],
        )
    )
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)
    response = parts.api.execute(ExperimentQuery())

    assert [entry.hypothesis_id for entry in response.experiments] == ["H-01", "H-02"]
    assert response.experiments[1].active is True
    assert response.experiments_ready is True


def test_service_projects_committed_live_state_without_reloading_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_id = _project_run(tmp_path / "project")
    store = AgentRunStateStore(project.state.portable_namespace(run_id, "agent"))
    initial = AgentRunState(
        experiment_revision=1,
        hypotheses=[
            _hypothesis("H-01", 1, last_experiment_revision=1),
            _hypothesis("H-02", 2, last_experiment_revision=1),
        ],
    )
    store.save(initial)
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)
    full = parts.api.execute(ExperimentQuery())
    assert full.experiment_update is not None

    changed = initial.clone()
    changed.experiment_revision = 2
    changed.hypotheses[1].last_experiment_revision = 2
    changed.hypotheses[1].plan.task = "project this row only"
    store.save(changed)
    parts.integration.publish_committed_state("agent", changed, changed_keys=("H-02",))
    parts.journal.record(
        EventType.EXPERIMENTS_CHANGED,
        data=ExperimentsChangedData(reason="round_persisted", revision=2),
    )
    monkeypatch.setattr(
        AgentRunStateStore,
        "load_optional",
        lambda _self: (_ for _ in ()).throw(AssertionError("unexpected state reload")),
    )

    delta = parts.api.execute(
        ExperimentQuery(
            after=ExperimentCursor(
                run_id=run_id,
                projection_id=full.experiment_update.projection_id,
                revision=1,
            )
        )
    )

    assert [entry.hypothesis_id for entry in delta.experiments] == ["H-02"]
    assert delta.experiment_update is not None
    assert delta.experiment_update.reset is False
    assert delta.experiment_update.through_revision == 2


def test_committed_update_wins_a_race_with_a_cold_authoritative_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_id = _project_run(tmp_path / "project")
    store = AgentRunStateStore(project.state.portable_namespace(run_id, "agent"))
    initial = AgentRunState(
        experiment_revision=1,
        hypotheses=[_hypothesis("H-01", 1, last_experiment_revision=1)],
    )
    store.save(initial)
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)
    loaded = Event()
    release = Event()
    original_load = AgentRunStateStore.load_optional
    load_count = 0

    def delayed_load(current_store: AgentRunStateStore) -> AgentRunState | None:
        nonlocal load_count
        load_count += 1
        state = original_load(current_store)
        loaded.set()
        assert release.wait(timeout=5)
        return state

    monkeypatch.setattr(AgentRunStateStore, "load_optional", delayed_load)
    with ThreadPoolExecutor(max_workers=1) as executor:
        response_future = executor.submit(parts.api.execute, ExperimentQuery())
        assert loaded.wait(timeout=5)
        changed = initial.clone()
        changed.experiment_revision = 2
        changed.hypotheses[0].last_experiment_revision = 2
        changed.hypotheses[0].plan.task = "new committed contents"
        store.save(changed)
        parts.integration.publish_committed_state("agent", changed, changed_keys=("H-01",))
        parts.journal.record(
            EventType.EXPERIMENTS_CHANGED,
            data=ExperimentsChangedData(reason="round_persisted", revision=2),
        )
        release.set()
        response = response_future.result(timeout=5)

    assert load_count == 1
    assert response.experiment_update is not None
    assert response.experiment_update.through_revision == 2
    assert response.experiments[0].action == "new committed contents"


def test_service_resets_when_another_project_attaches_with_the_same_run_id(
    tmp_path: Path,
) -> None:
    first_project, run_id = _project_run(tmp_path / "first")
    first_state = AgentRunState(
        experiment_revision=1,
        hypotheses=[_hypothesis("H-first", 1, last_experiment_revision=1)],
    )
    AgentRunStateStore(first_project.state.portable_namespace(run_id, "agent")).save(first_state)
    parts = build_server_parts(
        first_project.state.log_directory(run_id),
        project=first_project,
        run_id=run_id,
    )
    first = parts.api.execute(ExperimentQuery())
    assert first.experiment_update is not None

    second_project, _ = _project_run(tmp_path / "second")
    second_state = AgentRunState(
        experiment_revision=1,
        hypotheses=[_hypothesis("H-second", 1, last_experiment_revision=1)],
    )
    AgentRunStateStore(second_project.state.portable_namespace(run_id, "agent")).save(second_state)
    parts.attach(
        second_project.state.log_directory(run_id),
        project=second_project,
        run_id=run_id,
    )
    parts.journal.record(
        EventType.EXPERIMENTS_CHANGED,
        data=ExperimentsChangedData(reason="project_attached"),
    )

    response = parts.api.execute(
        ExperimentQuery(
            after=ExperimentCursor(
                run_id=run_id,
                projection_id=first.experiment_update.projection_id,
                revision=1,
            )
        )
    )

    assert [entry.hypothesis_id for entry in response.experiments] == ["H-second"]
    assert response.experiment_update is not None
    assert response.experiment_update.reset is True
    assert response.experiment_update.projection_id != first.experiment_update.projection_id


def test_committed_state_is_projected_synchronously_before_later_mutation(tmp_path: Path) -> None:
    project, run_id = _project_run(tmp_path / "project")
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)
    state = AgentRunState(hypotheses=[_hypothesis("H-01", 1)])

    parts.integration.publish_committed_state("agent", state)
    state.hypotheses[0].plan.task = "uncommitted mutation"
    response = parts.api.execute(ExperimentQuery())

    assert response.experiments[0].action == "test H-01"


def test_service_reads_performance_from_authoritative_agent_state(tmp_path: Path) -> None:
    project, run_id = _project_run(tmp_path / "project")
    portable = project.state.portable_namespace(run_id, "agent")
    AgentRunStateStore(portable).save(
        AgentRunState(
            hypotheses=[
                _hypothesis(
                    "H-01",
                    1,
                    rounds=[
                        _round(
                            1,
                            hypothesis_id="H-01",
                            perf_metric=42.0,
                            perf_unit="ops_s",
                            passed=True,
                        )
                    ],
                )
            ]
        )
    )
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)
    response = parts.api.execute(PerformanceQuery())

    assert [(item.round, item.perf_metric) for item in response.performance] == [(1, 42.0)]


def test_service_adapts_legacy_state_read_only(tmp_path: Path) -> None:
    project, run_id = _project_run(tmp_path / "project")
    project.state.save_round(
        run_id,
        _round(
            1,
            hypothesis_id="H-01",
            hypothesis_claim="legacy claim",
            hypothesis_task="legacy task",
        ),
    )
    portable = project.state.portable_namespace(run_id, "agent")
    store = AgentRunStateStore(portable)
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)
    (entry,) = parts.api.execute(ExperimentQuery()).experiments

    assert entry.hypothesis_id == "H-01"
    assert entry.claim == "legacy claim"
    assert store.load_optional() is None


def test_service_rebuilds_legacy_measurement_and_resolution_from_round_evidence(
    tmp_path: Path,
) -> None:
    """Legacy summaries may omit measurements, but nested evidence is complete."""
    configuration = _configuration().model_copy(update={"objectives": ("ops_s:max",)})
    project, run_id = _project_run(tmp_path / "project", configuration)
    project.state.save_round(
        run_id,
        _round(
            1,
            commit="a" * 40,
            hypothesis_id="H-parent",
            hypothesis_outcome="proven",
            passed=True,
            reviewed=True,
            official_evaluation=True,
            perf_metric=100.0,
            perf_unit="ops_s",
        ),
    )
    project.state.save_round(
        run_id,
        _round(
            2,
            commit="b" * 40,
            hypothesis_id="H-regression",
            hypothesis_parent_round=1,
            hypothesis_parent_commit="a" * 40,
            hypothesis_outcome="proven",
            passed=True,
            reviewed=True,
            official_evaluation=True,
            perf_metric=90.0,
            perf_unit="ops_s",
        ),
    )
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)
    entries = parts.api.execute(ExperimentQuery()).experiments

    regression = next(entry for entry in entries if entry.hypothesis_id == "H-regression")
    assert regression.resolved_outcome == "disproven"
    assert (regression.perf_metric, regression.perf_unit, regression.perf_delta_pct) == (
        90.0,
        "ops_s",
        -10.0,
    )
    # The configured objective direction and the rebuilt causal baseline reach
    # the wire, so the client can label the numbers above.
    assert (
        regression.perf_metric_name,
        regression.perf_direction,
        regression.perf_baseline_value,
    ) == ("ops_s", "max", 100.0)


def test_service_projects_a_within_noise_delta_as_inconclusive(tmp_path: Path) -> None:
    """Regression for #507: the read path must use the run's stored tolerance.

    The server reprojects hypothesis evidence on every read. It has no access
    to the task's ``objectives.toml``, so the tolerance has to travel with the
    run state; otherwise a 1% delta under a 5% noise model reaches the client
    as ``proven`` while the round record says the run learned nothing.
    """
    configuration = _configuration().model_copy(update={"objectives": ("ops_s:max",)})
    project, run_id = _project_run(tmp_path / "project", configuration)
    portable = project.state.portable_namespace(run_id, "agent")
    AgentRunStateStore(portable).save(
        AgentRunState(
            metrics=MetricSpace(
                objectives=(Objective(name="ops_s", direction="max"),),
                relative_noise=0.05,
            ),
            hypotheses=[
                _hypothesis(
                    "H-parent",
                    1,
                    rounds=[
                        _round(
                            1,
                            commit="a" * 40,
                            hypothesis_id="H-parent",
                            hypothesis_declared_outcome="nominated",
                            judge_verdict="pass",
                            passed=True,
                            official_evaluation=True,
                            perf_metric=100.0,
                            perf_unit="ops_s",
                            perf_direction="max",
                        )
                    ],
                ),
                _hypothesis(
                    "H-within-noise",
                    2,
                    parent_round=1,
                    parent_commit="a" * 40,
                    rounds=[
                        _round(
                            2,
                            commit="b" * 40,
                            hypothesis_id="H-within-noise",
                            hypothesis_parent_round=1,
                            hypothesis_parent_commit="a" * 40,
                            hypothesis_declared_outcome="nominated",
                            hypothesis_outcome="inconclusive",
                            judge_verdict="pass",
                            passed=True,
                            official_evaluation=True,
                            perf_metric=101.0,
                            perf_unit="ops_s",
                            perf_direction="max",
                        )
                    ],
                ),
            ],
        )
    )
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)
    entries = parts.api.execute(ExperimentQuery()).experiments

    entry = next(item for item in entries if item.hypothesis_id == "H-within-noise")
    assert entry.resolved_outcome == "inconclusive"


def test_service_returns_authoritative_empty_log_after_attach(tmp_path: Path) -> None:
    project, run_id = _project_run(tmp_path / "project")
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)
    response = parts.api.execute(ExperimentQuery())

    assert response.experiments == []
    assert response.experiments_ready is True
