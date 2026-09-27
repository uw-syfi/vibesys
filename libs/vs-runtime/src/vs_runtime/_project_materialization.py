"""Canonical project-tree materialization mechanics.

Product composition supplies an ordered materialization plan plus environment
effects. This module owns filesystem copies, pinned Git checkouts, SDK source
rewrites, symlink normalization, collision rejection, and cleanup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vs_runtime._input_project import materialize_input_project

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from vs_runtime._sdk_paths import SDKRoots


class ProjectMaterializationEffects(Protocol):
    """Environment-specific effects required while materializing a project."""

    @property
    def isolated(self) -> bool:
        """Whether external symlinks must be removed instead of represented."""
        ...

    def repair(self, workspace: Path) -> None:
        """Repair permissions in an existing workspace."""
        ...

    def remove_child(self, workspace: Path, name: str) -> bool:
        """Remove a child through a privileged environment, if available."""
        ...


class FreshProjectErrorKind(StrEnum):
    """Closed reasons a fresh project destination cannot be opened."""

    DESTINATION_INSIDE_SOURCE = "destination_inside_source"
    DESTINATION_EXISTS = "destination_exists"
    ROOT_MISMATCH = "root_mismatch"


class FreshProjectError(ValueError):
    """A fresh project destination violates the materializer contract."""

    def __init__(
        self,
        kind: FreshProjectErrorKind,
        *,
        source: Path,
        destination: Path,
        materializer_root: Path,
    ) -> None:
        self.kind = kind
        self.source = source
        self.destination = destination
        self.materializer_root = materializer_root
        super().__init__(kind.value)


@dataclass(frozen=True)
class ProjectTreeCopy:
    """One planned directory copy into the project root."""

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
    excluded_relative_paths: frozenset[Path] = frozenset()


@dataclass(frozen=True)
class InputProjectMaterialization:
    """Materialize an input ``pyproject.toml`` and its local path deps."""

    project_dir: Path


@dataclass(frozen=True)
class WorkspaceSourceValue:
    """Composition-only value for one pinned Git source."""

    repo: str
    commit: str
    dest: str
    strip_git: bool


@dataclass(frozen=True)
class GitSourceMaterialization:
    """Materialize a labelled pinned Git source into the candidate project."""

    name: str
    source: WorkspaceSourceValue


ProjectMaterializationStep = (
    ProjectTreeCopy | InputProjectMaterialization | GitSourceMaterialization
)


class ProjectMaterializer:
    """Apply canonical filesystem materialization to one project root."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-011124 [PLR0913]; The destination, environment effects, log, SDK roots, exclusions, and injectable Git effect are independent runtime inputs.
        self,
        root: Path,
        *,
        effects: ProjectMaterializationEffects,
        log: Callable[[str], None],
        sdk_roots: SDKRoots,
        excluded_dirs: Iterable[str],
        git_runner: Callable[[Sequence[str], Path], str] | None = None,
    ) -> None:
        """Configure workspace population and path policy for one run."""
        self.root = root
        self.excluded_dirs = set(excluded_dirs)
        self._effects = effects
        self._log = log
        self._sdk_roots = sdk_roots
        self._git_runner = git_runner or _run_git

    def create(self) -> None:
        """Create the workspace root directory if it does not exist."""
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def fresh_project(self, source: Path, destination: Path) -> Iterator[None]:
        """Own one fresh destination and remove it if provisioning fails.

        The caller owns authored-input validation and the ordered plan. This
        boundary owns destination safety, creation, and cleanup on every
        failure raised while the plan and any product finalization execute.
        """
        resolved_source = source.expanduser().resolve()
        resolved_destination = destination.expanduser().resolve()
        resolved_root = self.root.expanduser().resolve()
        if resolved_destination == resolved_source or resolved_destination.is_relative_to(
            resolved_source
        ):
            raise FreshProjectError(
                FreshProjectErrorKind.DESTINATION_INSIDE_SOURCE,
                source=resolved_source,
                destination=resolved_destination,
                materializer_root=resolved_root,
            )
        if resolved_destination.exists() or resolved_destination.is_symlink():
            raise FreshProjectError(
                FreshProjectErrorKind.DESTINATION_EXISTS,
                source=resolved_source,
                destination=resolved_destination,
                materializer_root=resolved_root,
            )
        if resolved_root != resolved_destination:
            raise FreshProjectError(
                FreshProjectErrorKind.ROOT_MISMATCH,
                source=resolved_source,
                destination=resolved_destination,
                materializer_root=resolved_root,
            )

        resolved_destination.mkdir(parents=True)
        try:
            yield
        except BaseException as exc:
            try:
                self._remove_path(resolved_destination)
            except OSError as cleanup_error:
                exc.add_note(
                    "Failed to remove partial provisioned project "
                    f"{resolved_destination}: {cleanup_error}"
                )
            raise

    def repair(self) -> None:
        """Fix ownership of files a previous root-running sandbox left behind.

        Used when resuming an existing run so the agent can write project files
        that may have been created as root by Docker.
        """
        self._effects.repair(self.root)

    def materialize(
        self,
        plan: tuple[ProjectMaterializationStep, ...],
        *,
        existing: bool,
    ) -> None:
        """Execute an ordered materialization plan."""
        if not existing:
            for excluded in self.excluded_dirs:
                d = self.root / excluded
                if d.exists():
                    shutil.rmtree(d)

        for step in plan:
            if isinstance(step, InputProjectMaterialization):
                materialize_input_project(
                    step.project_dir,
                    self.root,
                    sdk_roots=self._sdk_roots,
                    copy_directory=lambda src, dst: self.copy_tree(
                        ProjectTreeCopy(src=src, dest=dst)
                    ),
                    log=self._log,
                )
                continue
            if isinstance(step, GitSourceMaterialization):
                self.materialize_git_source(step)
                continue
            if step.require_absent is not None and (
                step.require_absent.exists() or step.require_absent.is_symlink()
            ):
                raise ValueError(step.require_absent_message)
            self.copy_tree(step)

    def materialize_git_source(self, step: GitSourceMaterialization) -> None:
        """Clone a pinned git source into the workspace and optionally strip ``.git``."""
        source = step.source
        name = step.name
        dest = self.root / source.dest
        try:
            dest.resolve().relative_to(self.root.resolve())
        except ValueError as exc:
            message = f"workspace source {name!r} escapes workspace: {source.dest}"
            raise ValueError(message) from exc
        if dest.exists() or dest.is_symlink():
            message = f"workspace source destination already exists for {name!r}: {source.dest}"
            raise ValueError(message)
        # Excluded names match at any depth (copy ignores, git info/exclude,
        # Modal uploads), so a colliding dest would be silently dropped from
        # snapshots and sandboxes even though the clone succeeds.
        colliding = [part for part in Path(source.dest).parts if part in self.excluded_dirs]
        if colliding:
            message = (
                f"workspace source {name!r} dest {source.dest!r} contains excluded "
                f"path component(s) {colliding}: files under it would be invisible to "
                "workspace copies, git tracking, and sandbox uploads. Pick another dest."
            )
            raise ValueError(message)
        dest.parent.mkdir(parents=True, exist_ok=True)

        self._git_runner(["clone", "--no-checkout", source.repo, str(dest)], self.root)
        self._git_runner(["checkout", "--detach", source.commit], dest)
        actual = self._git_runner(["rev-parse", "HEAD"], dest).strip().lower()
        expected = source.commit.lower()
        if actual != expected and not actual.startswith(expected):
            message = f"workspace source {name!r} checked out {actual}, expected {expected}"
            raise RuntimeError(message)

        metadata_path = self.root / "_vibesys_sources.json"
        metadata = []
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text())
        metadata.append(
            {
                "name": name,
                "repo": source.repo,
                "commit": actual,
                "requested_commit": source.commit,
                "dest": source.dest,
                "strip_git": source.strip_git,
            }
        )
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")

        if source.strip_git:
            shutil.rmtree(dest / ".git")

    def relocate_copied_tree(self, spec: ProjectTreeCopy, *, copied_from: Path) -> None:
        """Move a source tree copied with its parent into its canonical location.

        If ``spec.src`` was already copied from ``copied_from``, its copied
        location is removed before the canonical copy. Git ignores are applied
        when the source itself is a Git worktree.
        """
        source_root = copied_from.expanduser().resolve()
        source = spec.src.expanduser().resolve()
        destination = spec.dest.expanduser().resolve()
        try:
            copied_relative = source.relative_to(source_root)
        except ValueError:
            copied_relative = None

        if copied_relative is not None:
            copied_path = self.root.expanduser().resolve() / copied_relative
            if copied_path == destination:
                return
            self.remove_paths((copied_path,))

        self.materialize(
            (replace(spec, respect_gitignore=self._is_git_worktree(source)),),
            existing=True,
        )

    def remove_paths(self, paths: Iterable[Path]) -> None:
        """Remove selected descendants of this project, deepest paths first."""
        root = self.root.expanduser().absolute()
        selected: list[Path] = []
        for path in paths:
            candidate = path.expanduser().absolute()
            if candidate == root or not candidate.is_relative_to(root):
                message = f"project cleanup path escapes project root: {path}"
                raise ValueError(message)
            selected.append(candidate)
        for path in sorted(selected, key=lambda item: len(item.parts), reverse=True):
            self._remove_path(path)

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
    def _replace_external_symlinks(root: Path) -> None:
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

    def _prepare_copy_destination(
        self,
        children: list[Path],
        dst: Path,
        skip: set[str],
        *,
        reject_collisions: bool,
    ) -> None:
        """Validate collisions or clear replaceable destination children."""
        if reject_collisions:
            collisions = sorted(
                child.name
                for child in children
                if (dst / child.name).exists() or (dst / child.name).is_symlink()
            )
            if collisions:
                paths = ", ".join(collisions)
                message = f"workspace source and input bundle contain the same paths: {paths}"
                raise ValueError(message)
        if not dst.exists() or reject_collisions:
            return
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
                if not self._effects.remove_child(dst, child.name):
                    self._log(f"[warn] copy_dir: could not remove {child.name} from {dst}")

    def _copy_workspace_child(
        self,
        child: Path,
        dst: Path,
        ignore: Callable[[str, list[str]], list[str]],
    ) -> None:
        """Replace one destination child with its source counterpart."""
        child_dst = dst / child.name
        if child_dst.exists() or child_dst.is_symlink():
            # Stale leftover — try once more to remove before copying.
            try:
                if child_dst.is_dir() and not child_dst.is_symlink():
                    shutil.rmtree(child_dst)
                else:
                    child_dst.unlink()
            except PermissionError:
                self._log(
                    f"[warn] copy_dir: {child.name} in {dst} is stale and could not be replaced"
                )
                return
        try:
            if child.is_symlink():
                child_dst.symlink_to(child.readlink())
            elif child.is_dir():
                shutil.copytree(child, child_dst, symlinks=True, ignore=ignore)
            else:
                shutil.copy2(child, child_dst)
        except PermissionError:
            self._log(f"[warn] copy_dir: could not copy {child.name} to {dst}")

    def copy_tree(
        self,
        spec: ProjectTreeCopy,
    ) -> None:
        """Copy a source tree into the workspace under configured exclusions."""
        src = spec.src
        dst = spec.dest
        extra_excludes = spec.extra_excludes
        respect_source_gitignore = spec.respect_gitignore
        reject_collisions = spec.reject_collisions
        skip = self.excluded_dirs | {"_mounts"} | set(extra_excludes)
        excluded_relative_paths = {path.parts for path in spec.excluded_relative_paths}
        ignored_paths = (
            self._source_gitignored_paths(src) if respect_source_gitignore else frozenset()
        )
        resolved_src = src.resolve()

        def _is_ignored(path: Path) -> bool:
            relative_parts = path.absolute().relative_to(resolved_src).parts
            ignored_by_git = any(
                relative_parts[:index] in ignored_paths
                for index in range(1, len(relative_parts) + 1)
            )
            return ignored_by_git or relative_parts in excluded_relative_paths

        def _ignore(directory: str, names: list[str]) -> list[str]:
            parent = Path(directory)
            return [name for name in names if name in skip or _is_ignored(parent / name)]

        children = [
            child for child in src.iterdir() if child.name not in skip and not _is_ignored(child)
        ]

        self._prepare_copy_destination(
            children,
            dst,
            skip,
            reject_collisions=reject_collisions,
        )
        dst.mkdir(parents=True, exist_ok=True)
        for child in children:
            self._copy_workspace_child(child, dst, _ignore)
        if self._effects.isolated:
            # In containerized mode, external symlinks become bind mounts
            # (Docker) or volume uploads (Modal). Remove the broken symlinks
            # so the mount point / volume path can host the resolved contents.
            self._remove_external_symlinks(dst)
        else:
            self._replace_external_symlinks(dst)

    @staticmethod
    def _source_gitignored_paths(src: Path) -> frozenset[tuple[str, ...]]:
        """Return untracked paths ignored by Git below ``src``."""
        git = shutil.which("git") or "git"
        result = subprocess.run(  # noqa: S603  # lint-waiver: LW-010238 [S603]; this fixed Git query inspects ignored workspace paths without a shell.
            [
                git,
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
            message = f"could not evaluate source Git ignores: {detail}"
            raise RuntimeError(message)
        return frozenset(
            Path(os.fsdecode(raw).rstrip("/")).parts for raw in result.stdout.split(b"\0") if raw
        )

    @staticmethod
    def _is_git_worktree(path: Path) -> bool:
        git = shutil.which("git") or "git"
        result = subprocess.run(  # noqa: S603  # lint-waiver: LW-010239 [S603]; this checks the supplied project path using a fixed non-shell Git command.
            [git, "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()


def _run_git(args: Sequence[str], cwd: Path) -> str:
    git = shutil.which("git") or "git"
    result = subprocess.run(  # noqa: S603  # lint-waiver: LW-010237 [S603]; project Git arguments are built by internal repository operations and use no shell.
        [git, *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        message = f"git {' '.join(args)} failed: {detail}"
        raise RuntimeError(message)
    return result.stdout


__all__ = [
    "FreshProjectError",
    "FreshProjectErrorKind",
    "GitSourceMaterialization",
    "InputProjectMaterialization",
    "ProjectMaterializationEffects",
    "ProjectMaterializationStep",
    "ProjectMaterializer",
    "ProjectTreeCopy",
    "WorkspaceSourceValue",
]
