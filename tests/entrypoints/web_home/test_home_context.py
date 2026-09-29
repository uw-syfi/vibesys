"""Unit tests for the git subprocess primitive in `entrypoints.web_home.context`."""

from __future__ import annotations

import stat
import subprocess
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from entrypoints.web_home import context
from entrypoints.web_home.contract import ApiError, ErrorCode

if TYPE_CHECKING:
    from pathlib import Path

_SHORT_TIMEOUT_SECONDS = 1
_HOOK_SLEEP_SECONDS = 5


class _HungProcess:
    """A fake `Popen` whose first `communicate` times out, then settles once signaled."""

    args = ("git", "status")
    returncode = -15

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self._signaled = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if not self._signaled:
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout or 0)
        return "", ""

    def terminate(self) -> None:
        self._signaled = True

    def kill(self) -> None:
        self._signaled = True


def test_git_timeout_raises_api_error_instead_of_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # test-isolation: a hung git process is not reproducible without faking the syscall boundary.
    monkeypatch.setattr(context.subprocess, "Popen", _HungProcess)

    with pytest.raises(ApiError) as excinfo:
        context.git(tmp_path, "status")

    assert excinfo.value.code == ErrorCode.INTERNAL


def test_a_hung_hook_is_terminated_without_leaving_index_lock(tmp_path: Path) -> None:
    """A SIGKILL would skip git's lockfile cleanup; SIGTERM must not."""
    run_test_command(["git", "init", "-q"], cwd=tmp_path, check=True)
    run_test_command(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    run_test_command(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    (tmp_path / "file.txt").write_text("content\n")
    run_test_command(["git", "add", "file.txt"], cwd=tmp_path, check=True)
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\nsleep {_HOOK_SLEEP_SECONDS}\n")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

    with pytest.raises(ApiError) as excinfo:
        context.git(tmp_path, "commit", "-m", "hooked", timeout=_SHORT_TIMEOUT_SECONDS)

    assert excinfo.value.code == ErrorCode.INTERNAL
    assert not (tmp_path / ".git" / "index.lock").exists()
    # The repository must still be usable: a later git call succeeds.
    status = run_test_command(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert status.stdout == "A  file.txt\n"
