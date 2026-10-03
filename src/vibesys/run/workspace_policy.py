"""Application policy for canonical project materialization plans."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.constants import PROJECT_ROOT
from vs_agent.api import cli_mcp_config_files, cli_skill_dirs
from vs_runtime.api.infrastructure import (
    GitSourceMaterialization,
    InputProjectMaterialization,
    ProjectMaterializationStep,
    ProjectMaterializer,
    ProjectTreeCopy,
    SDKRoots,
    WorkspaceSourceValue,
    discover_skill_dirs,
    resolve_packaged_tree,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from vibesys.inputs import WorkspaceSource
    from vs_runtime.api.infrastructure import RunEnvironment
    from vs_sandbox.api import ComputeBackendImpl


EXCLUDED_WORKSPACE_DIRS: frozenset[str] = frozenset(
    {
        ".claude",
        "__pycache__",
        ".git",
        "repos",
        "_auth",
        "_opt_vibesys",
        "_mounts",
        ".cache",
        ".venv",
        "exp_env",
        "target",
    }
)

_CLI_SKILL_DIRS: tuple[str, ...] = cli_skill_dirs()

# Drivers write each turn's MCP server config, including the role's evaluation
# capability token, into the workspace. Committing it would leak the token
# into candidate history and make a mid-turn snapshot differ from the
# end-of-turn snapshot of the same candidate content.
AGENT_CONFIG_FILES: frozenset[str] = frozenset(cli_mcp_config_files())


def materialized_skill_dirs(skill_sources: Iterable[Path]) -> frozenset[str]:
    """Return workspace directories that agent drivers refill with skill copies.

    Drivers copy every configured skill into the workspace root (by skill
    name) and into each CLI's skill-discovery directory before a turn. These
    copies are framework inputs, not candidate content, so the run keeps them
    out of Git: otherwise a read-only role's turn reports them as unauthorized
    edits and a writer's snapshot commits them into its candidate.
    """
    names = {
        skill_dir.name for source in skill_sources for skill_dir in discover_skill_dirs(source)
    }
    return frozenset({*names, *_CLI_SKILL_DIRS})


@dataclass(frozen=True)
class RunEnvironmentMaterializationEffects:
    """Bind VibeSys run-environment effects to runtime materialization."""

    environment: RunEnvironment
    backend: ComputeBackendImpl
    log: Callable[[str], None]

    @property
    def isolated(self) -> bool:
        """Return whether the selected environment isolates the workspace."""
        return self.environment.isolated

    def repair(self, workspace: Path) -> None:
        """Repair permissions through the selected run environment."""
        self.environment.repair_workspace(workspace, backend=self.backend, log=self.log)

    def remove_child(self, workspace: Path, name: str) -> bool:
        """Remove a root-owned child through the selected run environment."""
        return self.environment.remove_workspace_child(
            workspace,
            name,
            backend=self.backend,
        )


def create_project_materializer(
    root: Path,
    *,
    environment: RunEnvironment,
    backend: ComputeBackendImpl,
    log: Callable[[str], None],
    git_runner: Callable[[Sequence[str], Path], str] | None = None,
) -> ProjectMaterializer:
    """Compose the runtime mechanism with VibeSys-owned environment effects."""
    return ProjectMaterializer(
        root,
        effects=RunEnvironmentMaterializationEffects(environment, backend, log),
        log=log,
        sdk_roots=SDKRoots(
            checkout=PROJECT_ROOT / "sdk",
            packaged=resolve_packaged_tree(
                package="vibesys",
                packaged_subdir="_sdk",
                package_files=files,
            ),
        ),
        excluded_dirs=EXCLUDED_WORKSPACE_DIRS,
        git_runner=git_runner,
    )


def materialization_source(source: WorkspaceSource) -> GitSourceMaterialization:
    """Lower one validated manifest source into the runtime value contract."""
    return GitSourceMaterialization(
        name=source.name,
        source=WorkspaceSourceValue(
            repo=source.repo,
            commit=source.commit,
            dest=source.dest,
            strip_git=source.strip_git,
        ),
    )


def skill_copy(
    source: Path,
    destination: Path,
    excluded_relative_paths: frozenset[Path],
) -> ProjectTreeCopy:
    """Plan one skill copy with application-selected platform visibility."""
    return ProjectTreeCopy(
        src=source,
        dest=destination,
        excluded_relative_paths=excluded_relative_paths,
    )


def _profiler_materializations(
    root: Path,
    support_path: str | None,
    support_name: str | None,
    extra: tuple[tuple[str, str], ...],
    *,
    only_missing: bool,
) -> tuple[ProjectTreeCopy, ...]:
    """Plan profiler support copies, optionally preserving existing destinations."""
    if not support_path or not support_name:
        return ()
    sources = ((support_path, support_name), *extra)
    return tuple(
        ProjectTreeCopy(src=Path(path), dest=root / name)
        for path, name in sources
        if not only_missing or not (root / name).exists()
    )


def build_workspace_materialization_plan(  # noqa: PLR0913  # lint-waiver: LW-011125 [PLR0913]; the application plan joins independently selected input, evaluator, skill, profiler, resume, and environment policy values.
    root: Path,
    *,
    existing: bool,
    input_dir: Path,
    evaluator_source: Path | None,
    skill_sources: list[Path],
    input_project_dir: Path | None,
    profiler_support_path: str | None,
    profiler_support_name: str | None,
    skill_excluded_relative_paths: frozenset[Path],
    workspace_sources: tuple[WorkspaceSource, ...] = (),
    extra_input_excludes: frozenset[str] = frozenset(),
    profiler_support_extra: tuple[tuple[str, str], ...] = (),
) -> tuple[ProjectMaterializationStep, ...]:
    """Build the ordered plan for a fresh or resumed VibeSys workspace."""
    steps: list[ProjectMaterializationStep] = []
    for source in skill_sources:
        if (root / source.name).exists():
            steps.append(skill_copy(source, root / source.name, skill_excluded_relative_paths))
        for cli_relative in _CLI_SKILL_DIRS:
            destination = root / cli_relative / source.name
            if destination.exists():
                steps.append(skill_copy(source, destination, skill_excluded_relative_paths))

    if not existing:
        steps.extend(materialization_source(source) for source in workspace_sources)
        steps.append(
            ProjectTreeCopy(
                src=input_dir,
                dest=root,
                extra_excludes=extra_input_excludes,
                reject_collisions=bool(workspace_sources),
            )
        )
        if evaluator_source is not None:
            evaluator_root = root / "_evaluator"
            steps.append(
                ProjectTreeCopy(
                    src=evaluator_source,
                    dest=evaluator_root / evaluator_source.name,
                    respect_gitignore=True,
                    require_absent=evaluator_root,
                    require_absent_message=(
                        "_evaluator is reserved for the manifest-declared evaluator source"
                    ),
                )
            )
        steps.extend(
            skill_copy(source, root / source.name, skill_excluded_relative_paths)
            for source in skill_sources
        )
        if input_project_dir is not None:
            steps.append(InputProjectMaterialization(project_dir=input_project_dir))
        steps.extend(
            _profiler_materializations(
                root,
                profiler_support_path,
                profiler_support_name,
                profiler_support_extra,
                only_missing=False,
            )
        )

    if existing:
        steps.extend(
            _profiler_materializations(
                root,
                profiler_support_path,
                profiler_support_name,
                profiler_support_extra,
                only_missing=True,
            )
        )

    return tuple(steps)


__all__ = [
    "AGENT_CONFIG_FILES",
    "EXCLUDED_WORKSPACE_DIRS",
    "RunEnvironmentMaterializationEffects",
    "build_workspace_materialization_plan",
    "create_project_materializer",
    "materialization_source",
    "materialized_skill_dirs",
    "skill_copy",
]
