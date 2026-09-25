"""Typed filesystem boundary for repository-native VibeSys configuration.

This module owns the physical layout below ``.vibesys``. Callers discover and
select tasks through semantic values instead of constructing configuration or
state paths themselves. Manifest parsing, evaluator resolution, and run-state
contents remain outside this package.
"""

# These boundary errors deliberately include offending paths and task names.

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import vs_project._paths as project_paths
from vs_project.errors import ProjectError

_CONFIGURATION_DIRECTORY_NAME = project_paths.CONFIGURATION_DIRECTORY_NAME
_TASKS_DIRECTORY_NAME = project_paths.TASKS_DIRECTORY_NAME
_OBJECTIVE_FILE_NAME = project_paths.OBJECTIVE_FILE_NAME
_MANIFEST_FILE_NAME = project_paths.MANIFEST_FILE_NAME
_DOCKERFILE_NAME = "Dockerfile"
_TASK_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class ProjectLayoutError(ProjectError):
    """Base class for invalid or unavailable VibeSys project layouts."""

    @classmethod
    def tasks_unreadable(cls, path: Path) -> Self:
        """Describe a task directory that could not be inspected."""
        return cls(f"Could not inspect VibeSys tasks: {path}")

    @classmethod
    def path_resolution_failed(cls, label: str, path: Path) -> Self:
        """Describe a managed path that could not be resolved."""
        return cls(f"Could not resolve {label}: {path}")


class ProjectRootNotFoundError(ProjectLayoutError):
    """Raised when a requested project root does not exist as a directory."""

    @classmethod
    def missing(cls, path: Path | str) -> Self:
        """Describe a missing project root."""
        return cls(f"Project root does not exist: {path}")

    @classmethod
    def not_directory(cls, path: Path) -> Self:
        """Describe a project root that resolves to a non-directory."""
        return cls(f"Project root is not a directory: {path}")

    @classmethod
    def discovery_path_missing(cls, path: Path) -> Self:
        """Describe a missing starting path for upward discovery."""
        return cls(f"Project discovery path does not exist: {path}")


class ProjectNotInitializedError(ProjectLayoutError):
    """Raised when an existing directory has no repository-native task layout."""

    @classmethod
    def not_found_at_or_above(cls, path: Path) -> Self:
        """Describe a project search that found no initialized layout."""
        return cls(
            f"No VibeSys project containing {_CONFIGURATION_DIRECTORY_NAME}/"
            f"{_TASKS_DIRECTORY_NAME} was found at or above {path}"
        )

    @classmethod
    def directory_missing(cls, description: str, path: Path) -> Self:
        """Describe a missing required project directory."""
        return cls(f"{description} does not exist: {path}")

    @classmethod
    def not_directory(cls, description: str, path: Path) -> Self:
        """Describe a project path that is not a directory."""
        return cls(f"{description} is not a directory: {path}")


class UnsafeProjectPathError(ProjectLayoutError):
    """Raised when a managed path escapes its semantic root."""

    @classmethod
    def symlink(cls, description: str, path: Path) -> Self:
        """Describe a managed path that must not be a symlink."""
        return cls(f"{description} must not be a symlink: {path}")

    @classmethod
    def optional_file_symlink(cls, filename: str, path: Path) -> Self:
        """Describe an optional task file that is a symlink."""
        return cls(f"optional file {filename} must not be a symlink: {path}")

    @classmethod
    def invalid_relative(cls, label: str, path: Path) -> Self:
        """Describe an invalid relative path."""
        return cls(f"{label} must be a non-empty safe relative path: {path}")

    @classmethod
    def escapes(cls, description: str, root: Path, path: Path) -> Self:
        """Describe a path outside its allowed root."""
        return cls(f"{description} escapes {root}: {path}")


class InvalidTaskNameError(ProjectLayoutError, ValueError):
    """Raised when a task name cannot safely name one task directory."""

    @classmethod
    def invalid(cls, value: str) -> Self:
        """Describe a task name outside the accepted character set."""
        return cls(
            f"Invalid VibeSys task name {value!r}; expected lowercase letters, "
            "digits, dots, underscores, or hyphens"
        )


class InvalidTaskDefinitionError(ProjectLayoutError):
    """Raised when a discovered task is missing a required file or directory."""

    @classmethod
    def missing_file(cls, task_name: TaskName, filename: str) -> Self:
        """Describe a required task file that is absent."""
        return cls(f"VibeSys task {task_name.value!r} is missing required file {filename}")

    @classmethod
    def path_not_file(cls, task_name: TaskName, path: Path) -> Self:
        """Describe a task path that exists but is not a regular file."""
        return cls(f"VibeSys task {task_name.value!r} required path is not a file: {path}")

    @classmethod
    def optional_resolution_failed(cls, task_name: TaskName, filename: str) -> Self:
        """Describe an optional task file that could not be resolved."""
        return cls(f"VibeSys task {task_name.value!r} could not resolve optional file {filename}")

    @classmethod
    def optional_path_not_file(cls, task_name: TaskName, path: Path) -> Self:
        """Describe an optional task path that is not a regular file."""
        return cls(f"VibeSys task {task_name.value!r} optional path is not a file: {path}")


class TaskNotFoundError(ProjectLayoutError):
    """Raised when a requested task is not present in a project."""

    def __init__(self, task_name: str | None, available: tuple[TaskName, ...]) -> None:
        """Report the requested name and deterministic available names."""
        self.task_name = task_name
        self.available = available
        available_text = _format_task_names(available)
        if task_name is None:
            message = f"No VibeSys tasks were found. Available tasks: {available_text}"
        else:
            message = f"VibeSys task {task_name!r} was not found. Available tasks: {available_text}"
        super().__init__(message)


class AmbiguousTaskError(ProjectLayoutError):
    """Raised when implicit selection finds more than one task."""

    def __init__(self, available: tuple[TaskName, ...]) -> None:
        """Report the names that require explicit selection."""
        self.available = available
        super().__init__(
            "A task name is required because this project defines multiple VibeSys tasks: "
            f"{_format_task_names(available)}"
        )


@dataclass(frozen=True, order=True)
class TaskName:
    """Validated stable name of one repository-native VibeSys task."""

    value: str

    def __post_init__(self) -> None:
        """Reject names that could address more than one directory component."""
        if not _TASK_NAME_PATTERN.fullmatch(self.value):
            raise InvalidTaskNameError.invalid(self.value)

    def __str__(self) -> str:
        """Return the manifest-facing task name."""
        return self.value


@dataclass(frozen=True)
class ProjectRoot:
    """Absolute, resolved root of the candidate project."""

    path: Path


@dataclass(frozen=True)
class ConfigurationRoot:
    """Absolute, resolved root of human-authored VibeSys configuration."""

    path: Path

    def resolve(self, relative_path: Path | str, *, must_exist: bool = True) -> Path:
        """Resolve a safe configuration-relative path within this root."""
        return _resolve_relative(
            self.path,
            relative_path,
            label="VibeSys configuration path",
            must_exist=must_exist,
        )


@dataclass(frozen=True)
class TasksRoot:
    """Absolute, resolved root containing repository-native task directories."""

    path: Path


@dataclass(frozen=True)
class TaskDirectory:
    """Validated task directory and its conventional trusted inputs."""

    name: TaskName
    path: Path
    objective_path: Path
    manifest_path: Path
    dockerfile_path: Path | None

    def resolve(self, relative_path: Path | str, *, must_exist: bool = True) -> Path:
        """Resolve a safe task-relative resource without leaving this task."""
        return _resolve_relative(
            self.path,
            relative_path,
            label=f"resource for VibeSys task {self.name.value!r}",
            must_exist=must_exist,
        )


class ProjectLayout:
    """Discover and validate one project's repository-native VibeSys layout."""

    def __init__(self, project_root: ProjectRoot) -> None:
        """Bind to a normalized project root created by this package."""
        self._project_root = project_root

    @classmethod
    def open(cls, project_root: Path | str) -> Self:
        """Bind to an existing project directory without requiring initialization."""
        lexical_root = Path(project_root).expanduser()
        try:
            resolved_root = lexical_root.resolve(strict=True)
        except OSError as exc:
            raise ProjectRootNotFoundError.missing(lexical_root) from exc
        if not resolved_root.is_dir():
            raise ProjectRootNotFoundError.not_directory(resolved_root)
        return cls(ProjectRoot(resolved_root))

    @classmethod
    def discover(cls, start: Path | str) -> Self:
        """Find the closest initialized project at or above an existing path."""
        lexical_start = Path(start).expanduser()
        try:
            resolved_start = lexical_start.resolve(strict=True)
        except OSError as exc:
            raise ProjectRootNotFoundError.discovery_path_missing(lexical_start) from exc
        current = resolved_start if resolved_start.is_dir() else resolved_start.parent
        for candidate in (current, *current.parents):
            layout = cls(ProjectRoot(candidate))
            if layout.is_initialized():
                return layout
        raise ProjectNotInitializedError.not_found_at_or_above(resolved_start)

    @property
    def project_root(self) -> ProjectRoot:
        """Return the normalized candidate project root."""
        return self._project_root

    def is_initialized(self) -> bool:
        """Return whether authored task configuration exists, independent of state."""
        configuration_path = self._configuration_path()
        if not configuration_path.exists() and not configuration_path.is_symlink():
            return False
        configuration = self.configuration_root()
        tasks_path = configuration.path / _TASKS_DIRECTORY_NAME
        if not tasks_path.exists() and not tasks_path.is_symlink():
            return False
        self.tasks_root()
        return True

    def configuration_root(self) -> ConfigurationRoot:
        """Return the validated authored VibeSys configuration root."""
        path = self._validate_directory(
            self._configuration_path(),
            description="VibeSys configuration root",
        )
        return ConfigurationRoot(path)

    def tasks_root(self) -> TasksRoot:
        """Return the validated root of all task definitions."""
        configuration = self.configuration_root()
        path = self._validate_directory(
            configuration.path / _TASKS_DIRECTORY_NAME,
            description="VibeSys tasks root",
            boundary=configuration.path,
        )
        return TasksRoot(path)

    def discover_tasks(self) -> tuple[TaskDirectory, ...]:
        """Return all validated tasks ordered by task name."""
        tasks_root = self.tasks_root()
        try:
            children = tuple(tasks_root.path.iterdir())
        except OSError as exc:
            raise ProjectLayoutError.tasks_unreadable(tasks_root.path) from exc

        tasks: list[TaskDirectory] = []
        for child in sorted(children, key=lambda item: item.name):
            if not child.is_dir() and not child.is_symlink():
                continue
            name = TaskName(child.name)
            tasks.append(self._load_task(tasks_root, name, child))
        return tuple(tasks)

    def select_task(self, task_name: TaskName | str | None = None) -> TaskDirectory:
        """Select one task explicitly, or implicitly when exactly one exists."""
        selected_name = TaskName(task_name) if isinstance(task_name, str) else task_name
        tasks = self.discover_tasks()
        available = tuple(task.name for task in tasks)
        if selected_name is None:
            if len(tasks) == 1:
                return tasks[0]
            if not tasks:
                raise TaskNotFoundError(None, available)
            raise AmbiguousTaskError(available)
        for task in tasks:
            if task.name == selected_name:
                return task
        raise TaskNotFoundError(selected_name.value, available)

    def _load_task(
        self,
        tasks_root: TasksRoot,
        name: TaskName,
        lexical_path: Path,
    ) -> TaskDirectory:
        path = self._validate_directory(
            lexical_path,
            description=f"VibeSys task {name.value!r}",
            boundary=tasks_root.path,
        )
        objective_path = _resolve_required_file(path, _OBJECTIVE_FILE_NAME, name)
        manifest_path = _resolve_required_file(path, _MANIFEST_FILE_NAME, name)
        dockerfile_path = _resolve_optional_file(path, _DOCKERFILE_NAME, name)
        return TaskDirectory(
            name=name,
            path=path,
            objective_path=objective_path,
            manifest_path=manifest_path,
            dockerfile_path=dockerfile_path,
        )

    def _configuration_path(self) -> Path:
        return self._project_root.path / _CONFIGURATION_DIRECTORY_NAME

    def _validate_directory(
        self,
        lexical_path: Path,
        *,
        description: str,
        boundary: Path | None = None,
    ) -> Path:
        try:
            if lexical_path.is_symlink():
                raise UnsafeProjectPathError.symlink(description, lexical_path)
            path = lexical_path.resolve(strict=True)
        except OSError as exc:
            raise ProjectNotInitializedError.directory_missing(description, lexical_path) from exc
        containment_root = boundary or self._project_root.path
        _require_contained(path, containment_root, description)
        if not path.is_dir():
            raise ProjectNotInitializedError.not_directory(description, path)
        return path


def _resolve_required_file(task_root: Path, filename: str, task_name: TaskName) -> Path:
    lexical_path = task_root / filename
    try:
        path = lexical_path.resolve(strict=True)
    except OSError as exc:
        raise InvalidTaskDefinitionError.missing_file(task_name, filename) from exc
    _require_contained(path, task_root, f"required file {filename}")
    if not path.is_file():
        raise InvalidTaskDefinitionError.path_not_file(task_name, lexical_path)
    return path


def _resolve_optional_file(
    task_root: Path,
    filename: str,
    task_name: TaskName,
) -> Path | None:
    lexical_path = task_root / filename
    if lexical_path.is_symlink():
        raise UnsafeProjectPathError.optional_file_symlink(filename, lexical_path)
    if not lexical_path.exists():
        return None
    try:
        path = lexical_path.resolve(strict=True)
    except OSError as exc:
        raise InvalidTaskDefinitionError.optional_resolution_failed(task_name, filename) from exc
    _require_contained(path, task_root, f"optional file {filename}")
    if not path.is_file():
        raise InvalidTaskDefinitionError.optional_path_not_file(task_name, lexical_path)
    return path


def _resolve_relative(
    root: Path,
    relative_path: Path | str,
    *,
    label: str,
    must_exist: bool,
) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or relative == Path() or ".." in relative.parts:
        raise UnsafeProjectPathError.invalid_relative(label, relative)
    lexical_path = root / relative
    try:
        path = lexical_path.resolve(strict=must_exist)
    except OSError as exc:
        raise ProjectLayoutError.path_resolution_failed(label, lexical_path) from exc
    _require_contained(path, root, label)
    return path


def _require_contained(path: Path, root: Path, description: str) -> None:
    if not path.is_relative_to(root):
        raise UnsafeProjectPathError.escapes(description, root, path)


def _format_task_names(names: tuple[TaskName, ...]) -> str:
    return ", ".join(name.value for name in names) if names else "none"
