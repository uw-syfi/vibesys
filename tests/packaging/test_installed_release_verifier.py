"""Contracts for installed-release isolation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import verify_installed_release as verifier


def test_runtime_root_uses_the_resolved_filesystem_path(tmp_path: Path) -> None:
    apparent = tmp_path / "tmp"
    actual = tmp_path / "private" / "tmp"
    actual.mkdir(parents=True)
    apparent.symlink_to(actual, target_is_directory=True)

    assert verifier.resolved_runtime_root(apparent) == actual.resolve()


def test_console_entry_points_run_installed_commands_with_help(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vibesys_marker = tmp_path / "vibesys-args"
    vibesys = tmp_path / "vibesys"
    vibesys.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$VIBESYS_TEST_ARGS"\n')
    vibesys.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("VIBESYS_TEST_ARGS", str(vibesys_marker))

    verifier.verify_console_entry_point()

    assert vibesys_marker.read_text().splitlines() == ["--help"]


def test_sdk_sync_uses_the_running_isolated_interpreter(tmp_path: Path) -> None:
    command = verifier.build_sdk_sync_command(tmp_path / "workspace")

    assert command == [
        "uv",
        "sync",
        "--no-cache",
        "--no-config",
        "--python",
        sys.executable,
        "--project",
        str(tmp_path / "workspace"),
    ]


def test_required_system_tools_reject_missing_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(verifier.shutil, "which", lambda _executable: None)
    monkeypatch.setattr(verifier.site, "ENABLE_USER_SITE", False)
    monkeypatch.setattr(verifier, "_RUNTIME_ROOT", Path.cwd().resolve())
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(verifier.InstalledReleaseError, match="git"):
        verifier.verify_installed_release()
