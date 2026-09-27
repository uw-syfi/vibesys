"""External-symlink mount discovery and sandbox lifecycle preparation."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_sandbox.lifecycle import BeforeReadyContext, SandboxLifecycleHooks

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class _SymlinkLifecycleHooks(SandboxLifecycleHooks):
    commands: tuple[str, ...]

    def before_ready(self, context: BeforeReadyContext) -> None:
        for command in self.commands:
            result = context.sandbox.execute(command)
            if result.exit_code != 0:
                message = f"failed to create sandbox symlink with {command!r}: {result.output}"
                raise RuntimeError(message)
        save_symlink_commands = getattr(context.sandbox, "save_symlink_commands", None)
        if callable(save_symlink_commands):
            save_symlink_commands(list(self.commands))


def symlink_lifecycle_hooks(
    symlinks: list[tuple[str, str]],
) -> list[SandboxLifecycleHooks]:
    """Create idempotent startup hooks for container-visible symlinks."""
    if not symlinks:
        return []
    commands = tuple(
        f"ln -sfn {shlex.quote(target)} {shlex.quote(link)}" for link, target in symlinks
    )
    return [_SymlinkLifecycleHooks(commands)]


def collect_symlink_mounts(
    scan_dir: Path,
    container_prefix: str,
    *,
    bind_mounts: list[tuple[str, str, bool]],
    symlinks: list[tuple[str, str]],
    skip: set[str] | None = None,
) -> None:
    """Append mounts and links required for external symlinks in a directory."""
    for child in scan_dir.iterdir():
        if not child.is_symlink():
            continue
        if skip and child.name in skip:
            continue
        target = child.resolve()
        try:
            target.relative_to(scan_dir.resolve())
        except ValueError:
            pass
        else:
            continue

        host_path = find_mount_root(target)
        if host_path == target:
            bind_mounts.append((str(host_path), f"{container_prefix}/{child.name}", True))
        else:
            rel = target.relative_to(host_path)
            ancestor_mount = f"/workspace/_mounts/{child.name}"
            bind_mounts.append((str(host_path), ancestor_mount, True))
            symlinks.append((f"{container_prefix}/{child.name}", f"{ancestor_mount}/{rel}"))


def find_mount_root(target: Path) -> Path:
    """Return the shallowest host root needed to preserve nested external links."""
    if not target.is_dir():
        return target
    needs_ancestor = False
    for path in target.rglob("*"):
        if path.is_symlink():
            link_target = path.parent / path.readlink()
            try:
                link_target.resolve().relative_to(target.resolve())
            except ValueError:
                needs_ancestor = True
                break
    if not needs_ancestor:
        return target
    root = target
    for path in target.rglob("*"):
        if path.is_symlink():
            resolved = (path.parent / path.readlink()).resolve()
            while not str(resolved).startswith(str(root)):
                root = root.parent
    return root


__all__ = ["collect_symlink_mounts", "find_mount_root", "symlink_lifecycle_hooks"]
