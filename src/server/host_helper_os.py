"""The host helper's interfaces on a real host: projects, directories, launches, the checkout.

Each class implements one interface of ``server.host_helper``; the helper's entrypoint
wires them, and ``vibesys tasks`` reads tasks through ``FileProjects``. Processes start
through ``vs_sim.api``'s launchers, so this module names no interpreter of its own: a
launch runs the command line the ``vibesys`` launcher would. Like the helper, the module
is light (``tests/architecture/test_light_entrypoints.py``).

Public surface: ``FileProjects``, ``OsDirectories``, ``SubprocessRunLauncher``, and
``GitCheckoutProbe``.
"""

from __future__ import annotations

import asyncio
import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from server.host_api import (
    Checkout,
    DetachedLaunchErrorCode,
    DetachedLaunchFailure,
    HelperErrorCode,
    LiveInstanceRecord,
    TaskList,
    TaskSummary,
)
from server.host_helper import PathHints, ProjectLookupError, ProjectRun, Scan, ScannedEntry
from vs_project.api import Project, ProjectError
from vs_sim.api import ProcessSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_sim.api import CommandProbe, ProcessLauncher

_PYPROJECT_LIMIT_BYTES = 1 << 20
_GIT_TIMEOUT_SECONDS = 2.0
_LAUNCH_OUTPUT_TAIL = 2000


class FileProjects:
    """Projects read through ``vs_project``'s ``Project`` on this filesystem."""

    def tasks(self, project: Path) -> TaskList:
        """The project's tasks, ordered by name."""
        opened = self._initialized(project)
        try:
            tasks = opened.discover_tasks()
        except ProjectError as exc:
            raise ProjectLookupError(
                HelperErrorCode.TASK_INVALID, str(opened.root), str(exc)
            ) from exc
        return TaskList(
            project_root=str(opened.root),
            tasks=tuple(TaskSummary(name=task.name.value) for task in tasks),
        )

    def runs(self, project: Path) -> tuple[Path, tuple[ProjectRun, ...]]:
        """The project's root and its runs, oldest first; runs this VibeSys cannot read are left out."""
        opened = self._open(project)
        try:
            manifests = opened.state.list_runs()
        except ProjectError as exc:
            raise ProjectLookupError(
                HelperErrorCode.TASK_INVALID, str(opened.root), str(exc)
            ) from exc
        return opened.root, tuple(
            ProjectRun(
                run_id=manifest.run_id,
                task=manifest.task_name,
                display_name=manifest.display_name,
                created_at=manifest.created_at,
            )
            for manifest in manifests
        )

    def require_initialized(self, project: Path) -> Path:
        """The project's root, once it is known to define tasks."""
        return self._initialized(project).root

    def _open(self, project: Path) -> Project:
        try:
            return Project.open(project)
        except ProjectError as exc:
            raise ProjectLookupError(
                HelperErrorCode.PROJECT_MISSING, str(project), str(exc)
            ) from exc

    def _initialized(self, project: Path) -> Project:
        opened = self._open(project)
        if not opened.is_initialized():
            message = (
                f"{opened.root} has no VibeSys tasks: "
                f"{opened.configuration_path()}/tasks/ does not exist"
            )
            raise ProjectLookupError(
                HelperErrorCode.PROJECT_UNINITIALIZED, str(opened.root), message
            )
        return opened


class OsDirectories:
    """Directories as this user's account lists them."""

    def home(self) -> str:
        """``$HOME``, or the account's home directory."""
        return str(Path.home())

    def expand(self, path: str) -> str:
        """``path`` with a leading ``~`` or ``~user`` expanded."""
        return str(Path(path).expanduser()) if path.startswith("~") else path

    def scan(self, path: str, limit: int) -> Scan:
        """Up to ``limit`` entries of ``path``; a link counts as a directory when it leads to one."""
        entries: list[ScannedEntry] = []
        with os.scandir(path) as listing:
            for entry in listing:
                if len(entries) >= limit:
                    return Scan(entries=tuple(entries), complete=False)
                entries.append(ScannedEntry(entry.name, _is_directory(entry)))
        return Scan(entries=tuple(entries), complete=True)

    def hints(self, path: str) -> PathHints:
        """What ``path`` holds, each hint False when it cannot be read."""
        directory = Path(path)
        return PathHints(
            vibesys_checkout=_declares_vibesys(directory / "pyproject.toml"),
            vibesys_project=_defines_tasks(directory),
            git_repository=_exists(directory / ".git"),
        )


def _is_directory(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _defines_tasks(directory: Path) -> bool:
    try:
        return Project.open(directory).is_initialized()
    except (ProjectError, OSError):
        return False


def _exists(path: Path) -> bool:
    try:
        return path.exists(follow_symlinks=False)
    except OSError:
        return False


def _declares_vibesys(pyproject: Path) -> bool:
    try:
        if pyproject.stat().st_size > _PYPROJECT_LIMIT_BYTES:
            return False
        with pyproject.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    project = document.get("project")
    return isinstance(project, dict) and project.get("name") == "vibesys"


class SubprocessRunLauncher:
    """Starts a detached run with the command line ``vibesys --detach`` runs.

    ``command`` maps ``vibesys`` arguments to that command line (the launcher's
    ``headless_command``), so a run started here and one started at a terminal run
    the same code under the same interpreter.
    """

    def __init__(
        self, processes: ProcessLauncher, command: Callable[[Sequence[str]], list[str]]
    ) -> None:
        """Start processes through ``processes``; build each command line with ``command``."""
        self._processes = processes
        self._command = command

    def start(
        self, project: Path, arguments: tuple[str, ...]
    ) -> LiveInstanceRecord | DetachedLaunchFailure:
        """Run the launch in ``project`` and read the one line it prints."""
        try:
            return asyncio.run(self._start(project, arguments))
        except OSError as error:
            return _launch_failure(f"could not start the launch: {error}", 1)

    async def _start(
        self, project: Path, arguments: tuple[str, ...]
    ) -> LiveInstanceRecord | DetachedLaunchFailure:
        process = await self._processes.start(
            ProcessSpec(argv=tuple(self._command(arguments)), cwd=project)
        )
        outcome = await process.wait()
        lines = outcome.stdout.decode("utf-8", errors="replace").strip().splitlines()
        last = lines[-1] if lines else ""
        for model in (LiveInstanceRecord, DetachedLaunchFailure):
            try:
                return model.model_validate_json(last)
            except ValidationError:
                continue
        tail = outcome.stderr.decode("utf-8", errors="replace").strip()[-_LAUNCH_OUTPUT_TAIL:]
        summary = f"the launch exited with status {outcome.returncode} and printed no record"
        return _launch_failure(f"{summary}: {tail}" if tail else summary, outcome.returncode)


def _launch_failure(message: str, status: int) -> DetachedLaunchFailure:
    return DetachedLaunchFailure(
        code=DetachedLaunchErrorCode.SERVER_START_FAILED,
        stage="server_start",
        message=message,
        exit_code=status if status > 0 else 1,
    )


class GitCheckoutProbe:
    """Describes a source checkout with ``git``; ``root`` is ``None`` for an installed distribution."""

    def __init__(self, root: Path | None, probe: CommandProbe) -> None:
        """Describe ``root`` by running ``git`` through ``probe``."""
        self._root = root
        self._probe = probe

    def describe(self) -> Checkout | None:
        """The checkout's root, ``HEAD`` commit and whether tracked files changed."""
        if self._root is None:
            return None
        root = str(self._root)
        head = self._probe.run(
            ("git", "-C", root, "rev-parse", "HEAD"), timeout_seconds=_GIT_TIMEOUT_SECONDS
        )
        status = self._probe.run(
            ("git", "-C", root, "status", "--porcelain", "--untracked-files=no"),
            timeout_seconds=_GIT_TIMEOUT_SECONDS,
        )
        commit = head.stdout.strip() if head is not None and head.returncode == 0 else None
        dirty = (
            bool(status.stdout.strip()) if status is not None and status.returncode == 0 else None
        )
        return Checkout(root=root, commit=commit or None, dirty=dirty)
