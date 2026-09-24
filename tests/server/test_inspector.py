"""Read-only inspector answers over attached run state and event history."""

from pathlib import Path

from tests.server.support import agent_descriptor, build_server_parts
from tests.support.run_execution import run_execution_record

from server.diagnostics import DiagnosticScope
from server.events import ConfigurationFailedData, EventStatus, EventType
from server.read_model import RunInspector
from vibesys.agent_run.state import AgentRunState, AgentRunStateStore, Hypothesis
from vibesys.schemas import OrchestratorPlan
from vs_loop_state.api import RoundRecord
from vs_project.api import Project, RunEnvironmentRecord


def _project_run(root: Path) -> tuple[Project, str]:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Make the queue fast.\n", encoding="utf-8")
    project = Project.open(root)
    project.state.create_project("queue")
    manifest = project.state.new_run_manifest(
        "queue",
        run_id="queue-run",
        branch="vibesys/queue-run",
        vibesys_version="0.2.0-test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=agent_descriptor(),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


def test_inspector_answers_round_and_failure_queries(tmp_path):  # noqa: ANN001, ANN201
    project, run_id = _project_run(tmp_path / "project")
    AgentRunStateStore(project.state.portable_namespace(run_id, "single")).save(
        AgentRunState(
            hypotheses=[
                Hypothesis(
                    hypothesis_id="H-01",
                    plan=OrchestratorPlan(
                        hypothesis_id="H-01",
                        hypothesis="Improve the queue",
                        task="Tune the queue",
                        pass_criteria="",
                        reasoning="",
                    ),
                    started_round=1,
                    rounds=[
                        RoundRecord(
                            round_number=1,
                            hypothesis_id="H-01",
                            commit="1" * 40,
                            perf_metric=1100.0,
                            perf_unit="total_ops_per_sec",
                            passed=False,
                            profile_skipped=False,
                            official_evaluation_reason="Judge FAIL: latency regressed",
                        )
                    ],
                )
            ]
        ),
    )
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)
    inspector = RunInspector(parts.integration)

    assert '"round_number": 1' in inspector.round_detail(1)
    assert "latency regressed" in inspector.answer("why did the judge fail?")


def test_inspector_explains_latest_failed_execution(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round 5", "prompt")
    parts.controller.after_agent(
        "implementer",
        "round 5",
        error=RuntimeError("agent process exited"),
        execution_id=execution.execution_id,
    )

    answer = RunInspector(parts.integration).answer("why did the agent fail?")

    assert "Latest failed agent execution" in answer
    assert "agent process exited" in answer


def test_inspector_explains_configuration_failure(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.journal.record(
        EventType.CONFIGURATION_FAILED,
        "Model credentials are missing",
        status=EventStatus.FAILED,
        diagnostic=parts.journal.diagnostic_for(
            RuntimeError("Model credentials are missing"),
            scope=DiagnosticScope.CONFIGURATION,
            operation="Configuration",
        ),
        data=ConfigurationFailedData(
            code="model_auth_missing",
            stage="model_setup",
            message="Model credentials are missing",
            exit_code=2,
        ),
    )

    answer = RunInspector(parts.integration).answer("why did startup fail?")

    assert "Model credentials are missing" in answer
