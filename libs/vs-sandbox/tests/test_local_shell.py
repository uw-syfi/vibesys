"""Tests for the unconfined local shell sandbox and the shared result type."""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, cast

import pytest

from vs_sandbox.api import LocalShellSandbox, Sandbox, SandboxExecutionResult

if TYPE_CHECKING:
    from pathlib import Path


def test_result_defaults_describe_a_successful_untruncated_run() -> None:
    result = SandboxExecutionResult(output="x")

    assert result.exit_code is None
    assert not result.truncated
    assert result.stdout == ""
    assert result.stderr == ""


def test_local_shell_satisfies_the_sandbox_protocol(tmp_path: Path) -> None:
    sandbox: Sandbox = LocalShellSandbox(tmp_path)

    assert sandbox.id.startswith("local-")
    assert sandbox.execute("true").exit_code == 0


def test_ids_are_unique_per_instance(tmp_path: Path) -> None:
    assert LocalShellSandbox(tmp_path).id != LocalShellSandbox(tmp_path).id


def test_commands_run_in_the_root_dir(tmp_path: Path) -> None:
    result = LocalShellSandbox(tmp_path).execute("pwd")

    assert result.exit_code == 0
    assert result.stdout.strip() == str(tmp_path.resolve())
    assert result.output.strip() == str(tmp_path.resolve())


def test_environment_is_isolated_unless_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VS_LOCAL_SHELL_PROBE", "from-parent")

    isolated = LocalShellSandbox(tmp_path, env={"PATH": "/usr/bin:/bin", "ONLY": "1"})
    inherited = LocalShellSandbox(tmp_path, env={"ONLY": "2"}, inherit_env=True)

    assert isolated.execute('echo "${VS_LOCAL_SHELL_PROBE:-unset}:$ONLY"').stdout.strip() == (
        "unset:1"
    )
    assert inherited.execute('echo "$VS_LOCAL_SHELL_PROBE:$ONLY"').stdout.strip() == (
        "from-parent:2"
    )


def test_env_edits_apply_to_later_commands(tmp_path: Path) -> None:
    sandbox = LocalShellSandbox(tmp_path, inherit_env=True)
    sandbox.env["VS_DEVICE"] = "3"

    assert sandbox.execute("echo $VS_DEVICE").stdout.strip() == "3"


def test_stderr_lines_are_tagged_and_streams_are_kept_apart(tmp_path: Path) -> None:
    result = LocalShellSandbox(tmp_path).execute("echo out; echo err >&2")

    assert result.exit_code == 0
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert result.output == "out\n\n[stderr] err"


def test_silent_success_reports_no_output(tmp_path: Path) -> None:
    result = LocalShellSandbox(tmp_path).execute("true")

    assert result.output == "<no output>"


def test_non_zero_exit_appends_the_code(tmp_path: Path) -> None:
    result = LocalShellSandbox(tmp_path).execute("echo boom >&2; exit 7")

    assert result.exit_code == 7
    assert result.output.endswith("Exit code: 7")
    assert "[stderr] boom" in result.output


def test_output_past_the_cap_is_cut_and_flagged(tmp_path: Path) -> None:
    result = LocalShellSandbox(tmp_path, max_output_chars=10).execute("printf 'a%.0s' $(seq 50)")

    assert result.truncated
    assert len(result.stdout) <= 10
    assert result.stderr == ""
    assert result.output.startswith("a" * 10)
    assert "Output truncated at 10 characters" in result.output
    assert "a" * 11 not in result.output


def test_timeout_returns_exit_code_124(tmp_path: Path) -> None:
    result = LocalShellSandbox(tmp_path).execute("sleep 5", timeout=1)

    assert result.exit_code == 124
    assert "timed out after 1 seconds" in result.output


def test_default_timeout_applies(tmp_path: Path) -> None:
    result = LocalShellSandbox(tmp_path, timeout=1).execute("sleep 5")

    assert result.exit_code == 124


@pytest.mark.parametrize("command", ["", None])
def test_empty_command_is_rejected_without_running(tmp_path: Path, command: str | None) -> None:
    result = LocalShellSandbox(tmp_path).execute(cast("str", command))

    assert result.exit_code == 1
    assert "non-empty string" in result.output


def test_non_positive_timeouts_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        LocalShellSandbox(tmp_path, timeout=0)
    with pytest.raises(ValueError, match="timeout must be positive"):
        LocalShellSandbox(tmp_path).execute("true", timeout=-1)


def test_launch_failure_is_reported_not_raised(tmp_path: Path) -> None:
    result = LocalShellSandbox(tmp_path / "missing").execute("true")

    assert result.exit_code == 1
    assert result.output.startswith("Error executing command")


def test_cancelling_execute_stops_the_command_and_its_descendants(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    alive = tmp_path / "alive"
    os.mkfifo(ready)
    os.mkfifo(alive)
    # The test holds the read end of `alive`; the shell and its background
    # `sleep` each hold a write end. Reading end-of-file therefore means every
    # process the command started has exited, with no probe of a pid that may
    # vanish mid-read (/proc/<pid>/stat raises ProcessLookupError then).
    alive_reader = os.open(alive, os.O_RDONLY | os.O_NONBLOCK)
    sandbox = LocalShellSandbox(tmp_path, inherit_env=True)
    cancel = threading.Event()
    outcome: list[SandboxExecutionResult | BaseException] = []

    def run() -> None:
        try:
            outcome.append(
                sandbox.execute(
                    f"exec 3> {alive}; sleep 1000 & echo go > {ready}; wait",
                    timeout=3600,
                    cancel=cancel,
                )
            )
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-731004 [BLE001]; the test reports any worker failure.
            # > Narrower types would leave the main thread blocked on the FIFO.
            outcome.append(error)
            ready.write_text("")  # release the reader below

    worker = threading.Thread(target=run)
    worker.start()
    try:
        ready.read_text(encoding="utf-8")
        cancel.set()
        worker.join()
        result = outcome[0]
        assert isinstance(result, SandboxExecutionResult), result
        assert result.cancelled
        # b"" is end-of-file (no writer left); a surviving writer raises BlockingIOError.
        assert os.read(alive_reader, 1) == b""
    finally:
        os.close(alive_reader)


def test_an_unset_cancel_event_leaves_the_command_alone(tmp_path: Path) -> None:
    result = LocalShellSandbox(tmp_path).execute("echo done", cancel=threading.Event())

    assert result.exit_code == 0
    assert not result.cancelled
    assert result.stdout == "done\n"
