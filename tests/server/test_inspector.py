"""Read-only inspector answers over attached run state and event history."""

from pathlib import Path

from tests.server.support import build_server_parts

from server.diagnostics import DiagnosticScope
from server.read_model import RunInspector
from server.wire.v2 import events_pb2
from vs_loop_state import RoundRecord
from vs_project import AgentRunConfiguration, Project, RunEnvironmentRecord


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
        configuration=AgentRunConfiguration(
            outer_loop="agent",
            run_environment=RunEnvironmentRecord(name="local"),
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
        ),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


def test_inspector_answers_round_and_failure_queries(tmp_path):  # noqa: ANN001, ANN201
    project, run_id = _project_run(tmp_path / "project")
    project.state.save_round(
        run_id,
        RoundRecord(
            round_number=1,
            commit="1" * 40,
            perf_metric=1100.0,
            perf_unit="total_ops_per_sec",
            passed=False,
            profile_skipped=False,
            official_evaluation_reason="Judge FAIL: latency regressed",
        ),
    )
    parts = build_server_parts(project.state.log_directory(run_id), project=project, run_id=run_id)
    inspector = RunInspector(parts.integration)

    assert '"round": 1' in inspector.round_detail(1)
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
        events_pb2.EventType.EVENT_TYPE_CONFIGURATION_FAILED,
        "Model credentials are missing",
        status=events_pb2.EventStatus.EVENT_STATUS_FAILED,
        diagnostic=parts.journal.diagnostic_for(
            RuntimeError("Model credentials are missing"),
            scope=DiagnosticScope.DIAGNOSTIC_SCOPE_CONFIGURATION,
            operation="Configuration",
        ),
        data=events_pb2.ConfigurationFailedData(
            code="model_auth_missing",
            stage="model_setup",
            message="Model credentials are missing",
            exit_code=2,
        ),
    )

    answer = RunInspector(parts.integration).answer("why did startup fail?")

    assert "Model credentials are missing" in answer
