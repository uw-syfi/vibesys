import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

from vibe_serve.launcher import _monitor


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
