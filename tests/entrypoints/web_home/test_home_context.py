"""Unit tests for the git subprocess primitive in `entrypoints.web_home.context`."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from entrypoints.web_home import context
from entrypoints.web_home.contract import ApiError, ErrorCode

if TYPE_CHECKING:
    from pathlib import Path


def test_git_timeout_raises_api_error_instead_of_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _hung(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["git", "status"], timeout=60)

    # test-isolation: a hung git process is not reproducible without faking the syscall boundary.
    monkeypatch.setattr(context.subprocess, "run", _hung)

    with pytest.raises(ApiError) as excinfo:
        context.git(tmp_path, "status")

    assert excinfo.value.code == ErrorCode.INTERNAL
