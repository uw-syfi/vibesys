"""The implementations of ``RunHistory``: run branches with the checkout, and in memory."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from vs_project._run_branches import RUN_BRANCH_PREFIX, run_of_branch
from vs_project._state import (
    StateFile,
    _validate_state_relative_path,
    parse_run_manifest,
    validate_run_id,
)
from vs_project.api.git_repository import GitCommandError
from vs_project.api.run_history import RecordedRuns
from vs_project.errors import ProjectStateError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from vs_project._manifests import OrchestrationRunManifest
    from vs_project.api.git_repository import GitRepository
    from vs_project.project import Project

_MANIFEST = PurePosixPath("run.json")


def _recorded(
    candidates: Iterable[str], manifest_bytes: Mapping[str, tuple[bytes, Path]]
) -> RecordedRuns:
    runs: list[OrchestrationRunManifest] = []
    unreadable: list[tuple[str, ProjectStateError]] = []
    for run_id in sorted(candidates):
        found = manifest_bytes.get(run_id)
        if found is None:
            continue
        try:
            runs.append(parse_run_manifest(found[0], source=found[1]))
        except ProjectStateError as error:
            unreadable.append((run_id, error))
    runs.sort(key=lambda manifest: (manifest.created_at, manifest.run_id))
    return RecordedRuns(runs=tuple(runs), unreadable=tuple(unreadable))


def _relative(relative_path: str | PurePosixPath) -> PurePosixPath:
    return _validate_state_relative_path(relative_path)


class GitRunHistory:
    """``RunHistory`` over a project's run branches and its checkout.

    Each file of a run's directory is read from one place. When the run's own branch
    (``vibesys-runs/<run-id>``) exists and is not checked out, a file committed on it is
    read from its tip; every other file, and every file of the run whose branch is
    checked out, is read from the checkout. The checked-out run's checkout holds what it
    has not committed yet, and the state a run never commits (its store, artifacts) lives
    only in the checkout. A branch's tip wins over the copy of the same file another
    branch may carry (a run branch cut from an earlier run's branch holds that run's
    files as they were at the cut), and a run with no branch (no repository, a deleted
    branch) is read from the checkout.
    """

    def __init__(self, project: Project, repository: GitRepository) -> None:
        """Read ``project``'s runs, with ``repository`` serving the project root."""
        self._project = project
        self._git = repository

    def recorded(self) -> RecordedRuns:
        branches = self._branches()
        candidates = {*self._project.state.checkout_run_ids(), *branches}
        found = {
            run_id: located
            for run_id in candidates
            if (located := self._manifest_bytes(run_id, branches)) is not None
        }
        return _recorded(candidates, found)

    def manifest(self, run_id: str) -> OrchestrationRunManifest:
        validate_run_id(run_id)
        located = self._manifest_bytes(run_id, self._branches())
        if located is None:
            raise ProjectStateError.metadata_file_missing(self._checkout_path(run_id, _MANIFEST))
        return parse_run_manifest(located[0], source=located[1])

    def document(self, run_id: str, relative_path: str | PurePosixPath) -> bytes | None:
        validate_run_id(run_id)
        relative = _relative(relative_path)
        committed = self._committed(run_id, relative, self._branches())
        if committed is not None:
            return committed
        return self._project.state.checkout_run_file(run_id, relative)

    def documents(self, run_id: str) -> tuple[StateFile, ...]:
        validate_run_id(run_id)
        files = {
            item.relative_path: item for item in self._project.state.checkout_run_files(run_id)
        }
        branch = self._branches().get(run_id)
        if branch is not None:
            directory = self._project.state.run_directory(run_id)
            for path in self._git.tree_files(branch, directory.as_posix()):
                contents = self._git.read_blob(branch, path)
                if contents is not None:
                    relative = PurePosixPath(path).relative_to(directory)
                    files[relative] = StateFile(relative_path=relative, contents=contents)
        return tuple(files[path] for path in sorted(files, key=PurePosixPath.as_posix))

    def _manifest_bytes(
        self, run_id: str, branches: Mapping[str, str]
    ) -> tuple[bytes, Path] | None:
        committed = self._committed(run_id, _MANIFEST, branches)
        if committed is not None:
            path = self._project.state.run_directory(run_id) / _MANIFEST
            return committed, Path(f"{branches[run_id]}:{path}")
        contents = self._project.state.checkout_run_file(run_id, _MANIFEST)
        if contents is None:
            return None
        return contents, self._checkout_path(run_id, _MANIFEST)

    def _committed(
        self, run_id: str, relative: PurePosixPath, branches: Mapping[str, str]
    ) -> bytes | None:
        branch = branches.get(run_id)
        if branch is None:
            return None
        path = self._project.state.run_directory(run_id) / relative
        return self._git.read_blob(branch, path.as_posix())

    def _branches(self) -> dict[str, str]:
        """The run branches read from their tips: by run id, all but the checked-out one."""
        if not self._is_project_repository():
            return {}
        current = self._git.current_branch()
        branches: dict[str, str] = {}
        for branch in self._git.branches(RUN_BRANCH_PREFIX):
            run_id = run_of_branch(branch)
            if branch == current or run_id is None:
                continue
            try:
                branches[validate_run_id(run_id)] = branch
            except ProjectStateError:
                continue
        return branches

    def _is_project_repository(self) -> bool:
        """Whether the project root is the top level of a repository (else no branch is its)."""
        if not self._git.is_inside_work_tree():
            return False
        try:
            return self._git.toplevel() == self._project.root
        except GitCommandError:
            return False

    def _checkout_path(self, run_id: str, relative: PurePosixPath) -> Path:
        return self._project.root / self._project.state.run_directory(run_id) / relative


class FakeRunHistory:
    """An in-memory ``RunHistory``: each run's portable files, as its branch would hold them.

    It parses manifests with the same decoder as ``GitRunHistory`` and validates run ids
    and paths the same way, so it is no more forgiving.
    """

    def __init__(self, runs: Mapping[str, Mapping[str, bytes]] | None = None) -> None:
        """Hold ``runs``: by run id, each portable file's bytes by path in the run's directory."""
        self._runs: dict[str, dict[PurePosixPath, bytes]] = {}
        for run_id, files in (runs or {}).items():
            for relative_path, contents in files.items():
                self.record(run_id, relative_path, contents)

    def record(self, run_id: str, relative_path: str | PurePosixPath, contents: bytes) -> None:
        """Hold ``contents`` as one portable file of run ``run_id``."""
        files = self._runs.setdefault(validate_run_id(run_id), {})
        files[_relative(relative_path)] = contents

    def recorded(self) -> RecordedRuns:
        found = {
            run_id: (files[_MANIFEST], self._source(run_id))
            for run_id, files in self._runs.items()
            if _MANIFEST in files
        }
        return _recorded(self._runs, found)

    def manifest(self, run_id: str) -> OrchestrationRunManifest:
        contents = self._runs.get(validate_run_id(run_id), {}).get(_MANIFEST)
        if contents is None:
            raise ProjectStateError.metadata_file_missing(self._source(run_id))
        return parse_run_manifest(contents, source=self._source(run_id))

    def document(self, run_id: str, relative_path: str | PurePosixPath) -> bytes | None:
        return self._runs.get(validate_run_id(run_id), {}).get(_relative(relative_path))

    def documents(self, run_id: str) -> tuple[StateFile, ...]:
        files = self._runs.get(validate_run_id(run_id), {})
        return tuple(
            StateFile(relative_path=path, contents=files[path])
            for path in sorted(files, key=PurePosixPath.as_posix)
        )

    @staticmethod
    def _source(run_id: str) -> Path:
        return Path(f"fake-run-history:{run_id}") / _MANIFEST
