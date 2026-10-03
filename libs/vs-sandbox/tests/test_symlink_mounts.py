"""Public contract tests for external-symlink sandbox preparation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api import BeforeReadyContext, SandboxExecutionResult
from vs_sandbox.api.symlink_mounts import (
    collect_symlink_mounts,
    find_mount_root,
    symlink_lifecycle_hooks,
)

if TYPE_CHECKING:
    import threading
    from pathlib import Path


@dataclass
class _RecordingSandbox:
    result: SandboxExecutionResult
    commands: list[str] = field(default_factory=list)
    saved_symlink_commands: list[str] | None = None

    @property
    def id(self) -> str:
        return "recording-sandbox"

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: threading.Event | None = None,
    ) -> SandboxExecutionResult:
        assert timeout is None
        assert cancel is None
        self.commands.append(command)
        return self.result

    def save_symlink_commands(self, commands: list[str]) -> None:
        self.saved_symlink_commands = commands


def test_lifecycle_hook_installs_and_records_quoted_commands() -> None:
    sandbox = _RecordingSandbox(SandboxExecutionResult(output="", exit_code=0))
    hooks = symlink_lifecycle_hooks([("/workspace/model link", "/workspace/_mounts/model target")])

    hooks[0].before_ready(BeforeReadyContext(sandbox=sandbox))

    command = "ln -sfn '/workspace/_mounts/model target' '/workspace/model link'"
    assert sandbox.commands == [command]
    assert sandbox.saved_symlink_commands == [command]


def test_lifecycle_hook_rejects_failed_setup() -> None:
    sandbox = _RecordingSandbox(SandboxExecutionResult(output="permission denied", exit_code=17))
    hooks = symlink_lifecycle_hooks([("/workspace/model", "/mount/model")])

    with pytest.raises(RuntimeError, match="permission denied"):
        hooks[0].before_ready(BeforeReadyContext(sandbox=sandbox))

    assert sandbox.saved_symlink_commands is None


def test_empty_symlink_plan_needs_no_lifecycle_hook() -> None:
    assert symlink_lifecycle_hooks([]) == []


def test_collect_external_file_symlink_as_read_only_mount(tmp_path: Path) -> None:
    scan_dir = tmp_path / "reference"
    scan_dir.mkdir()
    target = tmp_path / "outside" / "model.bin"
    target.parent.mkdir()
    target.write_text("weights", encoding="utf-8")
    (scan_dir / "model").symlink_to(target)
    mounts: list[tuple[str, str, bool]] = []
    links: list[tuple[str, str]] = []

    collect_symlink_mounts(
        scan_dir,
        "/workspace/reference",
        bind_mounts=mounts,
        symlinks=links,
    )

    assert mounts == [(str(target), "/workspace/reference/model", True)]
    assert links == []


def test_collect_ignores_internal_and_explicitly_skipped_symlinks(tmp_path: Path) -> None:
    scan_dir = tmp_path / "reference"
    scan_dir.mkdir()
    internal = scan_dir / "internal"
    internal.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (scan_dir / "inside-link").symlink_to(internal)
    (scan_dir / "skip-me").symlink_to(outside)
    mounts: list[tuple[str, str, bool]] = []
    links: list[tuple[str, str]] = []

    collect_symlink_mounts(
        scan_dir,
        "/workspace/reference",
        bind_mounts=mounts,
        symlinks=links,
        skip={"skip-me"},
    )

    assert mounts == []
    assert links == []


def test_find_mount_root_includes_nested_external_symlink_target(tmp_path: Path) -> None:
    mount = tmp_path / "mount"
    target = mount / "tree"
    target.mkdir(parents=True)
    sibling = mount / "shared"
    sibling.mkdir()
    (target / "shared").symlink_to(sibling)

    assert find_mount_root(target) == mount
