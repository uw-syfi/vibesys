import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

from vibe_serve.launcher import _monitor, _selected_local_agent_cli, launch

TARGET_ARGS = ["--input", "examples/model-serving/moonshine-streaming"]


def _args_with_config(tmp_path: Path) -> list[str]:
    config = tmp_path / "agent.toml"
    config.write_text('[model]\nname = "test-model"\n')
    return [*TARGET_ARGS, "--config", str(config)]


def test_selected_local_agent_cli_uses_configured_default(tmp_path: Path):
    assert _selected_local_agent_cli(_args_with_config(tmp_path)) == "codex"


def test_selected_local_agent_cli_honors_override_and_skips_stub(tmp_path: Path):
    args = _args_with_config(tmp_path)
    assert _selected_local_agent_cli([*args, "--cli-provider", "claude"]) == "claude"
    assert _selected_local_agent_cli([*args, "--stub-agent"]) is None


def test_launch_reports_missing_agent_cli_before_starting_children(tmp_path: Path, capsys):
    with patch("vibe_serve.launcher.shutil.which", return_value=None):
        result = launch(_args_with_config(tmp_path))

    assert result == 1
    error = capsys.readouterr().err
    assert "agent CLI 'codex' was not found on PATH" in error
    assert "--cli-provider" in error


def test_monitor_reports_backend_failure_when_frontend_exits_first(tmp_path: Path):
    frontend = Mock()
    frontend.poll.return_value = 0
    backend = Mock()
    backend.poll.return_value = 7
    log_path = tmp_path / "backend.log"
    log_path.write_text("backend exploded\n")

    with patch("vibe_serve.launcher._report_backend_failure") as report:
        result = _monitor(frontend, backend, log_path)

    assert result == 7
    report.assert_called_once_with(backend, log_path)


def test_monitor_does_not_report_successful_backend(tmp_path: Path):
    frontend = Mock()
    frontend.poll.return_value = 0
    backend = Mock()
    backend.poll.return_value = 0

    with patch("vibe_serve.launcher._report_backend_failure") as report:
        result = _monitor(frontend, backend, tmp_path / "backend.log")

    assert result == 0
    report.assert_not_called()


def test_monitor_gives_backend_time_to_finish_after_frontend(tmp_path: Path):
    frontend = Mock()
    frontend.poll.return_value = 0
    backend = Mock()
    backend.poll.return_value = None
    backend.wait.return_value = 0

    with (
        patch("vibe_serve.launcher._terminate_backend") as terminate,
        patch("vibe_serve.launcher._report_backend_failure") as report,
    ):
        result = _monitor(frontend, backend, tmp_path / "backend.log")

    assert result == 0
    backend.wait.assert_called_once()
    terminate.assert_not_called()
    report.assert_not_called()


def test_monitor_treats_stuck_backend_termination_as_launcher_cleanup(tmp_path: Path):
    frontend = Mock()
    frontend.poll.return_value = 0
    backend = Mock()
    backend.poll.return_value = None
    backend.wait.side_effect = subprocess.TimeoutExpired("backend", 2)

    with (
        patch("vibe_serve.launcher._terminate_backend") as terminate,
        patch("vibe_serve.launcher._report_backend_failure") as report,
    ):
        result = _monitor(frontend, backend, tmp_path / "backend.log")

    assert result == 0
    terminate.assert_called_once_with(backend)
    report.assert_not_called()
