"""Read-only inspector answers over attached run state and event history."""

from pathlib import Path

from tests.server.support import agent_descriptor, build_server_parts, run_record

from server.diagnostics import DiagnosticScope
from server.events import ConfigurationFailedData, EventStatus, EventType
from server.read_model import RunInspector
from vs_project.api import Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record


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
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=agent_descriptor(),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


def test_inspector_answers_round_and_failure_queries(tmp_path: Path) -> None:
    project, run_id = _project_run(tmp_path / "project")
    log_dir = project.state.log_directory(run_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "run-1.log").write_text(
        "starting\n"
        '{"round_number": 1, "commit": "' + "1" * 40 + '", "passed": false,\n'
        ' "judge_verdict": "fail", "official_evaluation_reason": "Judge FAIL: latency regressed"}\n'
    )
    parts = build_server_parts(log_dir, record=run_record(project, run_id))
    inspector = RunInspector(parts.integration)

    assert '"round_number": 1' in inspector.round_detail(1)
    assert "latency regressed" in inspector.answer("why did the judge fail?")


def test_inspector_explains_latest_failed_execution(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    execution = parts.start_execution("implementer", "round 5", "prompt")
    parts.controller.after_agent(
        "implementer",
        "round 5",
        error=RuntimeError("agent process exited"),
        execution_id=execution.execution_id,
    )

    answer = RunInspector(parts.integration).answer("why did the agent fail?")

    assert "Latest failed agent execution" in answer
    assert "agent process exited" in answer


def test_inspector_explains_configuration_failure(tmp_path: Path) -> None:
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
