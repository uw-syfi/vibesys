"""Read-only inspector answers over attached run state and event history."""

from pathlib import Path

from tests.server.support import build_server_parts

from server.diagnostics import DiagnosticScope
from server.events import ConfigurationFailedData, EventStatus, EventType
from server.read_model import RunInspector


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

    assert "Latest failure" in answer
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
