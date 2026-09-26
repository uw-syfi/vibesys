"""VibeSys-owned policy for canonical project inputs.

The reusable sandbox library enforces paths supplied by its caller. This
module is the single application-level owner of which project paths VibeSys
considers trusted, read-only, or secret.
"""

from pathlib import Path

from framework.api import Project, ProjectPathPolicy

LEGACY_TRUSTED_PROJECT_INPUT_PATHS: tuple[str, ...] = (
    "OBJECTIVE.md",
    "vibesys.input.toml",
    "objectives.toml",
    "reference",
    "accuracy_checker",
    "benchmark",
    "_input_libs",
    "_evaluator",
)


def _authored_project_input_paths(root: Path) -> tuple[Path, ...]:
    """Return trusted inputs for the project's active authoring model."""
    project = Project.open(root)
    if project.is_initialized():
        paths = {project.tasks_root().path.relative_to(root)}
        return _minimal_paths(paths)
    return tuple(Path(value) for value in LEGACY_TRUSTED_PROJECT_INPUT_PATHS)


def build_project_path_policy(
    project_root: Path,
    *,
    evaluator_source: Path | None,
) -> ProjectPathPolicy:
    """Return the agent sandbox policy for one canonical project."""
    root = project_root.resolve()
    state_paths = Project.open(root).state.sandbox_paths()
    read_only = {
        path
        for path in (
            Path(".git"),
            state_paths.read_only_path,
            *_authored_project_input_paths(root),
            _project_relative(root, evaluator_source),
        )
        if path is not None and (root / path).exists()
    }
    hidden = {
        path
        for path in (
            state_paths.hidden_path,
            Path("agent.toml"),
            *(path.relative_to(root) for path in root.glob(".env*")),
        )
        if path is not None and (root / path).exists()
    }
    return ProjectPathPolicy(
        hidden_paths=_minimal_paths(hidden),
        read_only_paths=_minimal_paths(read_only),
    )


def trusted_project_input_paths(
    project_root: Path,
    *,
    evaluator_source: Path | None,
) -> tuple[Path, ...]:
    """Return Git pathspec roots whose contents agents must not alter."""
    root = project_root.resolve()
    paths = set(_authored_project_input_paths(root))
    evaluator = _project_relative(root, evaluator_source)
    if evaluator is not None:
        paths.add(evaluator)
    return _minimal_paths(paths)


def _project_relative(root: Path, path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(root)
    except ValueError:
        return None


def _minimal_paths(paths: set[Path]) -> tuple[Path, ...]:
    selected: list[Path] = []
    for path in sorted(paths, key=lambda item: (len(item.parts), item.as_posix())):
        if not any(path == parent or path.is_relative_to(parent) for parent in selected):
            selected.append(path)
    return tuple(selected)
