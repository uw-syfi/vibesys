"""Namespace-scoped workspace inspection during policy resume setup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.run.git_tracker import GitTracker
    from vs_project.api import Project, StateNamespace

_PORCELAIN_MIN_RECORD_LENGTH = 4


@dataclass(frozen=True, slots=True)
class RecoveryWorkspace:
    """Inspect and restore a policy's portable files before its run opens."""

    _project: Project
    _git: GitTracker
    _namespace: StateNamespace

    def _git_path(self, relative_path: str) -> str:
        relative = PurePosixPath(relative_path)
        if not relative.parts or relative.is_absolute():
            raise ValueError(f"invalid recovery path {relative_path!r}")  # noqa: TRY003
        file = self._namespace.equivalent_external_file(self._project.root, relative)
        return file.relative_to(self._project.root).as_posix()

    def dirty_paths(self, *, exclude: tuple[str, ...] = ()) -> tuple[str, ...]:
        """List changed tracked, untracked, and ignored files in this namespace."""
        root = self._namespace.agent_visible_path()
        command = [
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--ignored",
            "--untracked-files=all",
            "--",
            root,
        ]
        command.extend(f":(exclude){self._git_path(path)}" for path in exclude)
        result = self._git.run(command)
        prefix = f"{root}/"
        paths: list[str] = []
        records = iter(result.stdout.split(b"\0"))
        for record in records:
            if len(record) < _PORCELAIN_MIN_RECORD_LENGTH:
                continue
            path = record[3:].decode(errors="replace")
            paths.append(path.removeprefix(prefix))
            if b"R" in record[:2] or b"C" in record[:2]:
                previous = next(records, b"").decode(errors="replace")
                if previous:
                    paths.append(previous.removeprefix(prefix))
        return tuple(sorted(paths))

    def committed_bytes(self, relative_path: str) -> bytes | None:
        """Read a file from HEAD, returning None when it did not exist there."""
        path = self._git_path(relative_path)
        result = self._git.run(["git", "show", f"HEAD:{path}"], check=False)
        return result.stdout if result.returncode == 0 else None

    def restore_file(self, relative_path: str) -> None:
        """Restore exactly one policy-owned file from HEAD in index and worktree."""
        path = self._git_path(relative_path)
        self._git.run(["git", "reset", "--quiet", "HEAD", "--", path])
        self._git.run(["git", "restore", "--source=HEAD", "--worktree", "--", path])
