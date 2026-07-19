"""Workspace materialization for experiment runs.

``Workspace`` owns the unified workspace directory and all of its setup
logic: seed/input/evaluator/skills/profiler-harness copies, exclusion
rules, external-symlink handling, gitignore-respecting copies, collision
rejection, and resume behavior.  The per-source copy policies are built as
declarative :class:`CopySpec` / :class:`InputProjectSpec` records first
(``plan_setup``) and executed afterwards (``setup``), so tests can assert
on the plan without materializing anything.
"""

import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from vibesys.input_project import materialize_input_project

# Dirs excluded from workspace copy, git tracking, and the
# Modal-side tar download. ``_auth`` and ``_opt_vibesys`` are
# our own "bind-mount redirect" dirs under --modal (host auth +
# vibesys pkg uploaded into /workspace/_auth and
# /workspace/_opt_vibesys respectively) — not implementer
# output, and we never want them in git history. ``_mounts`` is
# the Docker ancestor-mount redirect dir for the same reason.
# ``.cache`` holds any HF-download fallback (drafter, etc.).
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

# Skill destinations mirrored by _materialize_skills inside cli_runner.
_CLI_SKILL_DIRS: tuple[str, ...] = (
    ".agents/skills",
    ".claude/skills",
    ".gemini/skills",
    ".cursor/skills",
    ".opencode/skills",
)


@dataclass(frozen=True)
class CopySpec:
    """One planned directory copy into the workspace."""

    src: Path
    dest: Path
    respect_gitignore: bool = False
    reject_collisions: bool = False
    extra_excludes: frozenset[str] = frozenset()
    # When set, the copy is refused (ValueError with ``require_absent_message``)
    # if this path already exists at execution time.  Used to keep the
    # ``_evaluator`` mount point reserved for the manifest-declared source.
    require_absent: Path | None = None
    require_absent_message: str = ""


@dataclass(frozen=True)
class InputProjectSpec:
    """Materialize an input ``pyproject.toml`` and its local path deps."""

    project_dir: Path


WorkspaceStep = CopySpec | InputProjectSpec


class Workspace:
    """The unified run workspace and every rule for populating it."""

    def __init__(
        self,
        root: Path,
        *,
        run_environment,
        backend,
        log: Callable[[str], None],
        project_root: Path,
        excluded_dirs: Iterable[str] = EXCLUDED_WORKSPACE_DIRS,
    ) -> None:
        self.root = root
        self.excluded_dirs = set(excluded_dirs)
        self._run_environment = run_environment
        self._backend = backend
        self._log = log
        self._project_root = project_root

    def create(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def repair(self) -> None:
        """Fix ownership of files a previous root-running sandbox left behind.

        Used when resuming an existing run so the agent can write to
        workspace files that may have been created as root by Docker.
        """
        self._run_environment.repair_workspace(
            self.root,
            backend=self._backend,
            log=self._log,
        )

    # -- setup planning -------------------------------------------------------

    def plan_setup(
        self,
        *,
        existing: bool,
        seed: Path | None,
        input_dir: Path,
        evaluator_source: Path | None,
        skill_sources: list[Path],
        input_project_dir: Path | None,
        profiler_support_path: str | None,
        profiler_support_name: str | None,
        extra_input_excludes: frozenset[str] = frozenset(),
    ) -> tuple[WorkspaceStep, ...]:
        """Build the ordered copy plan for ``setup``.

        On resume (``existing=True``) the workspace already contains
        reference files, skills, etc. from the previous run, so the full
        copy is skipped: only the always-refresh and ensure-present steps
        below are planned.
        """
        steps: list[WorkspaceStep] = []

        # Always refresh skills into the workspace (even on --resume). Skill
        # source is tiny (MB) and copying is cheap; without this, an
        # interrupted run leaves stale skills from the previous CLI version
        # in the host workspace, which Modal then uploads verbatim into the
        # fresh sandbox volume at start, and codex-cli fails to load them
        # (e.g. skill description exceeds a newer CLI's length limit).
        # Mirrors _materialize_skills destinations inside cli_runner.
        for src in skill_sources:
            rel = src.name
            if (self.root / rel).exists():
                steps.append(CopySpec(src=src, dest=self.root / rel))
            for cli_rel in _CLI_SKILL_DIRS:
                cli_target = self.root / cli_rel / rel
                if cli_target.exists():
                    steps.append(CopySpec(src=src, dest=cli_target))

        if not existing:
            if seed is not None:
                steps.append(CopySpec(src=seed, dest=self.root, respect_gitignore=True))
                steps.append(
                    CopySpec(
                        src=input_dir,
                        dest=self.root,
                        extra_excludes=extra_input_excludes,
                        reject_collisions=True,
                    )
                )
            else:
                steps.append(
                    CopySpec(src=input_dir, dest=self.root, extra_excludes=extra_input_excludes)
                )

            if evaluator_source is not None:
                evaluator_root = self.root / "_evaluator"
                steps.append(
                    CopySpec(
                        src=evaluator_source,
                        dest=evaluator_root / evaluator_source.name,
                        respect_gitignore=True,
                        require_absent=evaluator_root,
                        require_absent_message=(
                            "_evaluator is reserved for the manifest-declared evaluator source"
                        ),
                    )
                )

            for src in skill_sources:
                steps.append(CopySpec(src=src, dest=self.root / src.name))

            if input_project_dir is not None:
                steps.append(InputProjectSpec(project_dir=input_project_dir))

            if profiler_support_path and profiler_support_name:
                steps.append(
                    CopySpec(
                        src=Path(profiler_support_path), dest=self.root / profiler_support_name
                    )
                )

        # Always ensure profiler harnesses are present in the workspace, even
        # when resuming — the original run may not have had them.
        if existing and profiler_support_path and profiler_support_name:
            destination = self.root / profiler_support_name
            if not destination.exists():
                steps.append(CopySpec(src=Path(profiler_support_path), dest=destination))

        return tuple(steps)

    def setup(self, plan: tuple[WorkspaceStep, ...], *, existing: bool) -> None:
        """Execute a plan built by ``plan_setup``."""
        if not existing:
            for excluded in self.excluded_dirs:
                d = self.root / excluded
                if d.exists():
                    shutil.rmtree(d)

        for step in plan:
            if isinstance(step, InputProjectSpec):
                materialize_input_project(
                    step.project_dir,
                    self.root,
                    project_root=self._project_root,
                    copy_dir=self.copy_dir,
                    log=self._log,
                )
                continue
            if step.require_absent is not None and (
                step.require_absent.exists() or step.require_absent.is_symlink()
            ):
                raise ValueError(step.require_absent_message)
            self.copy_dir(
                step.src,
                step.dest,
                extra_excludes=step.extra_excludes,
                respect_source_gitignore=step.respect_gitignore,
                reject_collisions=step.reject_collisions,
            )

    # -- copy machinery -------------------------------------------------------

    @staticmethod
    def _remove_external_symlinks(root: Path) -> None:
        """Remove symlinks pointing outside *root* (Docker bind mounts replace them)."""
        resolved_root = root.resolve()
        for path in list(root.rglob("*")):
            if path.is_symlink():
                target = path.resolve()
                try:
                    target.relative_to(resolved_root)
                except ValueError:
                    path.unlink()

    @staticmethod
    def replace_external_symlinks(root: Path) -> None:
        """Replace symlinks pointing outside *root* with `<name>.symlink_target` files."""
        resolved_root = root.resolve()
        for path in list(root.rglob("*")):
            if path.is_symlink():
                target = path.resolve()
                try:
                    target.relative_to(resolved_root)
                except ValueError:
                    # Symlink points outside root — replace it
                    marker = path.parent / f"{path.name}.symlink_target"
                    path.unlink()
                    marker.write_text(str(target))

    def copy_dir(
        self,
        src: Path,
        dst: Path,
        *,
        extra_excludes: frozenset[str] = frozenset(),
        respect_source_gitignore: bool = False,
        reject_collisions: bool = False,
    ) -> None:
        skip = self.excluded_dirs | {"_mounts"} | set(extra_excludes)
        ignored_paths = (
            self._source_gitignored_paths(src) if respect_source_gitignore else frozenset()
        )
        resolved_src = src.resolve()

        def _is_ignored(path: Path) -> bool:
            if not ignored_paths:
                return False
            relative_parts = path.absolute().relative_to(resolved_src).parts
            return any(
                relative_parts[:index] in ignored_paths
                for index in range(1, len(relative_parts) + 1)
            )

        def _ignore(directory: str, names: list[str]) -> list[str]:
            parent = Path(directory)
            return [name for name in names if name in skip or _is_ignored(parent / name)]

        children = [
            child for child in src.iterdir() if child.name not in skip and not _is_ignored(child)
        ]

        if reject_collisions:
            collisions = sorted(
                child.name
                for child in children
                if (dst / child.name).exists() or (dst / child.name).is_symlink()
            )
            if collisions:
                paths = ", ".join(collisions)
                raise ValueError(f"workspace seed and input bundle contain the same paths: {paths}")

        if dst.exists() and not reject_collisions:
            # Remove children individually so we can skip mount points and
            # tolerate permission errors (e.g. root-owned dirs left by Docker).
            for child in list(dst.iterdir()):
                if child.name in skip:
                    continue
                try:
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                except PermissionError:
                    if not self._run_environment.remove_workspace_child(
                        dst,
                        child.name,
                        backend=self._backend,
                    ):
                        self._log(f"[warn] copy_dir: could not remove {child.name} from {dst}")
        dst.mkdir(parents=True, exist_ok=True)
        for child in children:
            child_dst = dst / child.name
            if child_dst.exists() or child_dst.is_symlink():
                # Stale leftover — try once more to remove before copying
                try:
                    if child_dst.is_dir() and not child_dst.is_symlink():
                        shutil.rmtree(child_dst)
                    else:
                        child_dst.unlink()
                except PermissionError:
                    self._log(
                        f"[warn] copy_dir: {child.name} in {dst} is stale and could not be replaced"
                    )
                    continue
            try:
                if child.is_symlink():
                    os.symlink(os.readlink(child), child_dst)
                elif child.is_dir():
                    shutil.copytree(child, child_dst, symlinks=True, ignore=_ignore)
                else:
                    shutil.copy2(child, child_dst)
            except PermissionError:
                self._log(f"[warn] copy_dir: could not copy {child.name} to {dst}")
        if self._run_environment.isolated:
            # In containerized mode, external symlinks become bind mounts
            # (Docker) or volume uploads (Modal). Remove the broken symlinks
            # so the mount point / volume path can host the resolved contents.
            self._remove_external_symlinks(dst)
        else:
            self.replace_external_symlinks(dst)

    @staticmethod
    def _source_gitignored_paths(src: Path) -> frozenset[tuple[str, ...]]:
        """Return untracked paths ignored by Git below ``src``."""

        result = subprocess.run(
            [
                "git",
                "-C",
                str(src),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
                "-z",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"could not evaluate Git ignores for workspace.seed: {detail}")
        return frozenset(
            Path(os.fsdecode(raw).rstrip("/")).parts for raw in result.stdout.split(b"\0") if raw
        )
