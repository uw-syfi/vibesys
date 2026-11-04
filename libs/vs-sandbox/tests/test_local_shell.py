"""Tests for the unconfined local shell sandbox and the shared result type."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api import CommandResult, CommandRunner, LocalShellRunner
from vs_sim.api import OsThreads, SubprocessPipedProcessLauncher

if TYPE_CHECKING:
    from pathlib import Path
from vs_sim.api import DISCARD


def test_result_defaults_describe_a_successful_untruncated_run() -> None:
    result = CommandResult(output="x")

    assert result.exit_code is None
    assert not result.truncated
    assert result.stdout == ""
    assert result.stderr == ""


def test_local_shell_satisfies_the_sandbox_protocol(tmp_path: Path) -> None:
    sandbox: CommandRunner = LocalShellRunner(
        tmp_path, threads=OsThreads(), launcher=SubprocessPipedProcessLauncher()
    )

    assert sandbox.id.startswith("local-")
    assert sandbox.execute("true", output=DISCARD).exit_code == 0


def test_ids_are_unique_per_instance(tmp_path: Path) -> None:
    assert (
        LocalShellRunner(
            tmp_path, threads=OsThreads(), launcher=SubprocessPipedProcessLauncher()
        ).id
        != LocalShellRunner(
            tmp_path, threads=OsThreads(), launcher=SubprocessPipedProcessLauncher()
        ).id
    )


def test_commands_run_in_the_root_dir(tmp_path: Path) -> None:
    result = LocalShellRunner(
        tmp_path, threads=OsThreads(), launcher=SubprocessPipedProcessLauncher()
    ).execute("pwd", output=DISCARD)

    assert result.exit_code == 0
    assert result.stdout.strip() == str(tmp_path.resolve())
    assert result.output.strip() == str(tmp_path.resolve())


def test_environment_is_isolated_unless_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VS_LOCAL_SHELL_PROBE", "from-parent")

    isolated = LocalShellRunner(
        tmp_path,
        env={"PATH": "/usr/bin:/bin", "ONLY": "1"},
        threads=OsThreads(),
        launcher=SubprocessPipedProcessLauncher(),
    )
    inherited = LocalShellRunner(
        tmp_path,
        env={"ONLY": "2"},
        inherit_env=True,
        threads=OsThreads(),
        launcher=SubprocessPipedProcessLauncher(),
    )

    assert isolated.execute(
        'echo "${VS_LOCAL_SHELL_PROBE:-unset}:$ONLY"', output=DISCARD
    ).stdout.strip() == ("unset:1")
    assert inherited.execute(
        'echo "$VS_LOCAL_SHELL_PROBE:$ONLY"', output=DISCARD
    ).stdout.strip() == ("from-parent:2")


def test_env_edits_apply_to_later_commands(tmp_path: Path) -> None:
    sandbox = LocalShellRunner(
        tmp_path, inherit_env=True, threads=OsThreads(), launcher=SubprocessPipedProcessLauncher()
    )
    sandbox.env["VS_DEVICE"] = "3"

    assert sandbox.execute("echo $VS_DEVICE", output=DISCARD).stdout.strip() == "3"


def test_non_positive_timeouts_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        LocalShellRunner(
            tmp_path, timeout=0, threads=OsThreads(), launcher=SubprocessPipedProcessLauncher()
        )
    with pytest.raises(ValueError, match="timeout must be positive"):
        LocalShellRunner(
            tmp_path, threads=OsThreads(), launcher=SubprocessPipedProcessLauncher()
        ).execute("true", timeout=-1, output=DISCARD)


def test_launch_failure_is_reported_not_raised(tmp_path: Path) -> None:
    result = LocalShellRunner(
        tmp_path / "missing", threads=OsThreads(), launcher=SubprocessPipedProcessLauncher()
    ).execute("true", output=DISCARD)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Error executing command")
