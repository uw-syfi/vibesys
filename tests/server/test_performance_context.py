"""Server projection tests for the /perf objective context."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.server.support import agent_descriptor, build_server_parts
from tests.support.run_execution import run_execution_record

from server.api.performance import build_performance_context, summarize_objective
from server.api.protocol import PerformanceQuery
from vibesys.agent_run.hypotheses import reproject_run_evidence
from vibesys.agent_run.readmodel import project_run_view
from vibesys.agent_run.state import (
    AgentRunState,
    Hypothesis,
    HypothesisMeasurement,
)
from vibesys.api.contracts import RunStatus
from vibesys.evaluators.metrics import MetricSpace
from vibesys.search.hypothesis import OrchestratorPlan
from vs_loop_state.api import RoundRecord
from vs_project.api import Project, RunEnvironmentRecord

if TYPE_CHECKING:
    from pathlib import Path

    from server.api.service import RunApi
    from vibesys.api import RunView


def _hypothesis(identifier: str, started_round: int, **overrides: object) -> Hypothesis:
    fields: dict[str, object] = {
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
    return Hypothesis.model_validate(fields)


def _measurement(round_number: int, **overrides: object) -> HypothesisMeasurement:
    fields: dict[str, object] = {
        "round": round_number,
        "metric": "total_ops_per_sec",
        "value": 2000.0,
    }
    fields.update(overrides)
    return HypothesisMeasurement.model_validate(fields)


def _configuration(objectives: tuple[str, ...]) -> MetricSpace:
    return MetricSpace.model_validate(
        {
            "objectives": [
                {"name": name, "direction": direction}
                for objective in objectives
                for name, _, direction in (objective.partition(":"),)
            ]
        }
    )


def _project_run(project: Path, objectives: tuple[str, ...]) -> tuple[Project, str]:
    project.mkdir()
    (project / "OBJECTIVE.md").write_text("Make the queue fast.\n", encoding="utf-8")
    vibesys_project = Project.open(project)
    vibesys_project.state.create_project("queue")
    manifest = vibesys_project.state.new_run_manifest(
        "queue",
        run_id="queue-run",
        branch="vibesys/queue-run",
        vibesys_version="0.2.0-test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=agent_descriptor(metric_space=_configuration(objectives)),
        trusted_input_baseline="0" * 40,
    )
    vibesys_project.state.create_run(manifest)
    return vibesys_project, manifest.run_id


def _view(state: AgentRunState) -> RunView:
    """Project a hand-built `AgentRunState` into the `RunView` `build_performance_context` reads."""
    return project_run_view(
        state,
        run_id="run-1",
        status=RunStatus.ACTIVE,
        experiment_revision=state.experiment_revision,
        loop="single-agent",
    )


def _service(project: Project, run_id: str) -> RunApi:
    return build_server_parts(
        project.state.log_directory(run_id), project=project, run_id=run_id
    ).api


def test_service_projects_context_from_round_evidence_and_objective_prose(
    tmp_path: Path,
) -> None:
    project, run_id = _project_run(tmp_path / "project", ("total_ops_per_sec:max",))
    document = project.state.portable_namespace(run_id, "runtime").external_directory()
    (document / "effective-objective.md").write_text(
        "# Objective\n\nMaximize queue throughput measured by the mpmc benchmark.\n\nMore detail.\n",
        encoding="utf-8",
    )
    state = reproject_run_evidence(
        AgentRunState(
            hypotheses=[
                _hypothesis(
                    "H-01",
                    1,
                    rounds=[
                        RoundRecord(
                            round_number=2,
                            commit="c2",
                            hypothesis_id="H-01",
                            passed=True,
                            official_evaluation=True,
                            perf_metric=2000.0,
                            perf_unit="total_ops_per_sec",
                            perf_baseline_round=1,
                            perf_baseline_commit="e17fce8123abc",
                            perf_baseline_metric=1000.0,
                        )
                    ],
                )
            ]
        )
    )

    project.state.portable_namespace(run_id, "single").slot("state.json", AgentRunState).save(state)
    response = _service(project, run_id).execute(PerformanceQuery())

    context = response.performance_context
    assert context is not None
    assert context.objective_metric == "total_ops_per_sec"
    assert context.objective_unit == "total_ops_per_sec"
    assert context.objective_direction == "max"
    assert context.objective_baseline_value == 1000.0
    assert context.objective_baseline_round == 1
    assert context.objective_baseline_commit == "e17fce8123abc"
    assert context.objective_description == (
        "Maximize queue throughput measured by the mpmc benchmark."
    )


def test_service_names_the_objective_before_the_first_measurement(tmp_path: Path) -> None:
    project, run_id = _project_run(tmp_path / "project", ("total_ops_per_sec:max",))
    project.state.portable_namespace(run_id, "single").slot("state.json", AgentRunState).save(
        AgentRunState()
    )

    response = _service(project, run_id).execute(PerformanceQuery())

    assert response.performance == []
    context = response.performance_context
    assert context is not None
    assert context.objective_metric == "total_ops_per_sec"
    assert context.objective_direction == "max"
    assert context.objective_baseline_value is None
    assert context.objective_description is None


def test_service_returns_no_context_without_an_attached_run() -> None:
    response = build_server_parts().api.execute(PerformanceQuery())

    assert response.performance == []
    assert response.performance_context is None


def test_build_context_copies_the_newest_measurement_as_one_tuple() -> None:
    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                measurement=_measurement(1, value=1000.0, baseline_value=900.0),
            ),
            _hypothesis(
                "H-02",
                2,
                measurement=_measurement(
                    3,
                    value=2000.0,
                    unit="ops/s",
                    direction="max",
                    baseline_round=1,
                    baseline_commit="abc1234",
                    baseline_value=1000.0,
                ),
            ),
        ]
    )

    context = build_performance_context(_view(state), objectives=())

    assert context is not None
    assert context.objective_unit == "ops/s"
    assert context.objective_direction == "max"
    assert context.objective_baseline_value == 1000.0
    assert context.objective_baseline_round == 1
    assert context.objective_baseline_commit == "abc1234"


def test_build_context_falls_back_to_the_manifest_direction() -> None:
    state = AgentRunState(hypotheses=[_hypothesis("H-01", 1, measurement=_measurement(1))])

    context = build_performance_context(_view(state), objectives=("total_ops_per_sec:min",))

    assert context is not None
    assert context.objective_direction == "min"


def test_build_context_is_none_with_nothing_to_say() -> None:
    assert build_performance_context(_view(AgentRunState()), objectives=()) is None
    assert build_performance_context(None, objectives=()) is None


def test_build_context_carries_prose_before_the_metric_is_known() -> None:
    context = build_performance_context(None, objectives=(), objective_description="How it runs.")

    assert context is not None
    assert context.objective_metric is None
    assert context.objective_description == "How it runs."


def test_summarize_objective_returns_the_first_prose_paragraph() -> None:
    text = "# Objective\n\nLine one\ncontinues here.\n\nSecond paragraph.\n"

    assert summarize_objective(text) == "Line one continues here."
    assert summarize_objective("# Heading only\n") is None
    assert summarize_objective("") is None


def test_summarize_objective_bounds_a_long_paragraph() -> None:
    summary = summarize_objective("word " * 200)

    assert summary is not None
    assert len(summary) <= 280
    assert summary.endswith("…")
