"""``GitRepository`` implemented by running the Git CLI.

This is the reference implementation of the contract in
``vs_project.api.git_repository``; every other implementation is checked
against it. All commands go through ``run_git``, so background maintenance stays
disabled (see ``_git_process``). After ``bind`` they are pinned with
``GIT_DIR`` and ``GIT_WORK_TREE`` to the repository selected at that moment.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from vs_project._git_process import git_environment, run_git
from vs_project._head_reader import Commit, Unborn, locate_git_dir, read_head
from vs_project.api.git_repository import (
    COMMIT_IDENTITY_EMAIL,
    COMMIT_IDENTITY_NAME,
    CommitSubject,
    GitCommandError,
    GitTimeoutError,
    PatchStyle,
    RepositoryLocation,
    StagingError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_project.api.git_repository import GitFaultSink, Pathspec, Revision


def _text(data: bytes) -> str:
    return data.decode(errors="replace")


class CliGitRepository:
    """Run ``git`` against the repository whose worktree top level is ``root``."""

    _ENVIRONMENT: ClassVar[dict[str, str]] = {
        "GIT_AUTHOR_NAME": COMMIT_IDENTITY_NAME,
        "GIT_AUTHOR_EMAIL": COMMIT_IDENTITY_EMAIL,
        "GIT_COMMITTER_NAME": COMMIT_IDENTITY_NAME,
        "GIT_COMMITTER_EMAIL": COMMIT_IDENTITY_EMAIL,
        # Read-only queries (``git status``, ``git diff``) otherwise try to
        # write a refreshed index back under ``.git/index.lock``. That races
        # with a concurrent ``git add``/``reset``/``commit`` and fails it with
        # "Unable to create index.lock". Commands that must write the index
        # still take the lock; only the opportunistic refresh is skipped.
        "GIT_OPTIONAL_LOCKS": "0",
    }

    def __init__(self, root: Path, *, faults: GitFaultSink) -> None:
        self._root = root
        self._faults = faults
        self._git_dir: Path | None = None
        self._work_tree: Path | None = None
        self._exclude_file = root / ".git" / "info" / "exclude"

    # -- process plumbing ----------------------------------------------------

    def _pins(self) -> dict[str, str]:
        """Env overrides pinning Git to the repository selected by ``bind``."""
        result = dict(self._ENVIRONMENT)
        if self._git_dir is not None and self._work_tree is not None:
            result["GIT_DIR"] = str(self._git_dir)
            result["GIT_WORK_TREE"] = str(self._work_tree)
        return result

    def _run(
        self,
        args: Sequence[str],
        *,
        timeout: float | None = None,
        index_file: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run ``git <args>`` at the root; the caller judges the exit code."""
        overrides = self._pins()
        if index_file is not None:
            overrides["GIT_INDEX_FILE"] = str(index_file)
        try:
            return run_git(
                args,
                cwd=self._root,
                env=git_environment(
                    safe_directory=self._work_tree or self._root, overrides=overrides
                ),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise GitTimeoutError(["git", *args], timeout or 0.0) from error

    @staticmethod
    def _failure(
        args: Sequence[str], result: subprocess.CompletedProcess[bytes]
    ) -> GitCommandError:
        return GitCommandError(["git", *args], result.returncode, _text(result.stderr).strip())

    def _run_checked(self, args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        """Run ``git <args>``; a nonzero exit is reported to the fault sink and raised."""
        result = self._run(args)
        if result.returncode != 0:
            error = self._failure(args, result)
            self._faults.warning(
                f"git command failed: git {' '.join(args)}",
                detail=f"exit code {result.returncode}: {error.stderr}",
            )
            raise error
        return result

    def _prefix(self) -> str:
        return _text(self._run_checked(["rev-parse", "--show-prefix"]).stdout).strip()

    # -- location ------------------------------------------------------------

    def is_inside_work_tree(self) -> bool:
        result = self._run(["rev-parse", "--is-inside-work-tree"])
        return result.returncode == 0 and result.stdout.decode().strip() == "true"

    def toplevel(self) -> Path:
        output = self._run_checked(["rev-parse", "--show-toplevel"]).stdout
        return Path(_text(output).strip()).resolve()

    def initialize(self, *, initial_branch: str) -> None:
        self._run_checked(["init", "-q", "-b", initial_branch])

    def bind(self) -> RepositoryLocation:
        located = self._run_checked(
            [
                "rev-parse",
                "--absolute-git-dir",
                "--show-toplevel",
                "--git-path",
                "info/exclude",
            ]
        )
        git_dir, work_tree, exclude_file = _text(located.stdout).splitlines()
        self._git_dir = Path(git_dir.strip()).resolve()
        self._work_tree = Path(work_tree.strip()).resolve()
        exclude_path = Path(exclude_file.strip())
        if not exclude_path.is_absolute():
            exclude_path = self._root / exclude_path
        self._exclude_file = exclude_path.resolve()
        return RepositoryLocation(git_dir=self._git_dir, work_tree=self._work_tree)

    # -- names ---------------------------------------------------------------

    def is_valid_branch_name(self, name: str) -> bool:
        return self._run(["check-ref-format", "--branch", name]).returncode == 0

    def is_valid_ref_name(self, name: str) -> bool:
        return self._run(["check-ref-format", name]).returncode == 0

    # -- reading history -----------------------------------------------------

    def head(self) -> str | None:
        git_dir = self._git_dir or locate_git_dir(self._root)
        if git_dir is not None:
            state = read_head(git_dir)
            if isinstance(state, Commit):
                return state.sha
            if isinstance(state, Unborn):
                return None
        try:
            result = self._run(["rev-parse", "HEAD"])
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return _text(result.stdout).strip()

    def current_branch(self) -> str | None:
        result = self._run(["symbolic-ref", "--quiet", "--short", "HEAD"])
        if result.returncode != 0:
            return None
        return _text(result.stdout).strip()

    def branch_exists(self, branch: str) -> bool:
        result = self._run(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        return result.returncode == 0

    def resolve_commit(self, revision: Revision) -> str | None:
        result = self._run(["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"])
        if result.returncode != 0:
            return None
        return _text(result.stdout).strip()

    def is_ancestor(self, ancestor: Revision, descendant: Revision) -> bool:
        return self._run(["merge-base", "--is-ancestor", ancestor, descendant]).returncode == 0

    def root_commit(self, revision: Revision) -> str | None:
        output = self._run_checked(["rev-list", "--max-parents=0", "--reverse", revision]).stdout
        roots = _text(output).splitlines()
        return roots[0] if roots else None

    def first_commit_adding(self, pathspecs: Sequence[Pathspec]) -> str | None:
        output = self._run_checked(
            ["log", "--diff-filter=A", "--format=%H", "--reverse", "--", *pathspecs]
        ).stdout
        commits = output.decode().splitlines()[0:1]
        return commits[0] if commits else None

    def recent_subjects(self, limit: int) -> tuple[CommitSubject, ...]:
        args = ["log", f"--max-count={limit}", "--format=%H%x1f%s"]
        result = self._run(args)
        if result.returncode != 0:
            raise self._failure(args, result)
        subjects = []
        for line in _text(result.stdout).splitlines():
            commit, _, subject = line.partition("\x1f")
            subjects.append(CommitSubject(sha=commit, subject=subject))
        return tuple(subjects)

    def reachable_paths(self) -> frozenset[str]:
        args = ["rev-list", "--objects", "--all", "--reflog"]
        result = self._run(args)
        if result.returncode != 0:
            raise self._failure(args, result)
        return frozenset(
            path for line in _text(result.stdout).splitlines() if (path := line.partition(" ")[2])
        )

    def read_blob(self, revision: Revision, path: str) -> bytes | None:
        result = self._run(["show", f"{revision}:{path}"])
        return None if result.returncode != 0 else result.stdout

    def has_ref_containing(self, commit: str, prefix: str) -> bool:
        result = self._run(
            [
                "for-each-ref",
                "--count=1",
                f"--contains={commit}",
                "--format=%(refname)",
                prefix,
            ]
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    # -- changing refs -------------------------------------------------------

    def update_ref(self, ref: str, commit: str) -> None:
        self._run_checked(["update-ref", ref, commit])

    def switch_branch(self, branch: str, *, create: bool = False) -> None:
        self._run_checked(["switch", *(["-c"] if create else []), branch])

    # -- comparing -----------------------------------------------------------

    def diff_patch(
        self,
        base: Revision,
        head: Revision,
        pathspecs: Sequence[Pathspec] = (),
        *,
        style: PatchStyle = PatchStyle.REVIEW,
        timeout: float | None = None,
    ) -> str:
        args = [
            "diff",
            "--no-ext-diff",
            *(
                ["--no-renames", "--full-index"]
                if style is PatchStyle.EXACT
                else ["--find-renames"]
            ),
            base,
            head,
            "--",
            *pathspecs,
        ]
        result = self._run(args, timeout=timeout)
        if result.returncode != 0:
            raise self._failure(args, result)
        return _text(result.stdout)

    def diff_name_status(
        self, base: Revision, head: Revision, *, timeout: float | None = None
    ) -> str:
        args = ["diff", "--no-ext-diff", "--name-status", "--find-renames", "-z", base, head]
        result = self._run(args, timeout=timeout)
        if result.returncode != 0:
            raise self._failure(args, result)
        return _text(result.stdout)

    def tracked_changes_since_head(self, pathspecs: Sequence[Pathspec]) -> tuple[str, ...]:
        output = self._run_checked(["diff", "--name-only", "HEAD", "--", *pathspecs]).stdout
        return tuple(sorted(path for path in _text(output).splitlines() if path))

    def uncommitted_paths(self, pathspecs: Sequence[Pathspec]) -> tuple[str, ...]:
        status = self._run_checked(
            ["status", "--porcelain=v1", "--untracked-files=all", "--", *pathspecs]
        )
        prefix = self._prefix()
        return tuple(sorted(self._status_paths(status.stdout, prefix)))

    def changed_since(self, commit: Revision, pathspecs: Sequence[Pathspec]) -> tuple[str, ...]:
        committed = self._run_checked(["diff", "--name-only", f"{commit}..HEAD", "--", *pathspecs])
        pending = self._run_checked(
            ["status", "--porcelain=v1", "--untracked-files=all", "--", *pathspecs]
        )
        prefix = self._prefix()
        changes = {
            line.removeprefix(prefix) if prefix else line
            for line in _text(committed.stdout).splitlines()
            if line
        }
        changes.update(self._status_paths(pending.stdout, prefix))
        return tuple(sorted(changes))

    @staticmethod
    def _status_paths(output: bytes, prefix: str) -> list[str]:
        return [
            line[3:].removeprefix(prefix) if prefix else line[3:]
            for line in _text(output).splitlines()
            if line[3:]
        ]

    def has_staged_changes(self, pathspecs: Sequence[Pathspec] = ()) -> bool:
        args = ["diff", "--cached", "--quiet", *(["--", *pathspecs] if pathspecs else [])]
        return self._run(args).returncode != 0

    def has_tracked_files(self, pathspec: Pathspec) -> bool:
        return bool(self._run_checked(["ls-files", "--", pathspec]).stdout.strip())

    def worktree_matches(
        self,
        revision: Revision,
        pathspecs: Sequence[Pathspec],
        *,
        include_ignored: bool,
    ) -> bool:
        # Compare through a scratch index: stage every file into a copy of the
        # revision's tree, then ask Git whether anything differs. A plain
        # ``git diff <revision>`` cannot see files that are untracked here.
        with tempfile.TemporaryDirectory() as scratch:
            index = Path(scratch) / "index"
            if self._run(["read-tree", revision], index_file=index).returncode != 0:
                return False
            add = ["add", "--all", *(["--force"] if include_ignored else []), "--", *pathspecs]
            if self._run(add, index_file=index).returncode != 0:
                return False
            diff = ["diff", "--cached", "--quiet", revision, "--", *pathspecs]
            return self._run(diff, index_file=index).returncode == 0

    # -- index and commits ---------------------------------------------------

    def stage_all(self, pathspecs: Sequence[Pathspec], *, force: bool = False) -> None:
        args = ["add", *(["--force"] if force else []), "-A", "--", *pathspecs]
        result = self._run(args)
        if result.returncode == 0:
            return
        stderr = _text(result.stderr)
        offenders = self._unreadable_from_stderr(stderr)
        error = StagingError(["git", *args], result.returncode, stderr.strip(), offenders)
        if not offenders:
            self._faults.warning(
                f"git command failed: git {' '.join(args)}",
                detail=f"exit code {result.returncode}: {error.stderr}",
            )
        raise error

    @staticmethod
    def _unreadable_from_stderr(stderr: str) -> list[str]:
        """Parse paths git reported it could not index.

        Git prints e.g. ``error: open("foo"): Permission denied`` and
        ``error: unable to index file 'foo'``.
        """
        return [
            match.group(1)
            for match in re.finditer(r'(?:open\("|unable to index file \')([^"\']+)', stderr)
        ]

    def unstage(self, pathspecs: Sequence[Pathspec]) -> None:
        if self.head() is not None:
            self._run_checked(["reset", "--quiet", "HEAD", "--", *pathspecs])
        else:
            self._run_checked(["rm", "--cached", "-r", "--ignore-unmatch", "--", *pathspecs])

    def commit(
        self, message: str, *, only: Sequence[Pathspec] = (), allow_empty: bool = False
    ) -> None:
        args = [
            "commit",
            *(["--allow-empty"] if allow_empty else []),
            *(["--only"] if only else []),
            "-m",
            message,
            *(["--", *only] if only else []),
        ]
        self._run_checked(args)

    # -- restoring the worktree ----------------------------------------------

    def reset_index(self) -> None:
        self._run_checked(["reset", "--mixed", "HEAD"])

    def clean_untracked(self, *, include_ignored: bool, protect: Pathspec) -> bool:
        args = ["clean", "-fdx" if include_ignored else "-fd", "-e", protect, "--", "."]
        return self._run(args).returncode == 0

    def restore_worktree(self, revision: Revision, exclude: Sequence[Pathspec] = ()) -> None:
        self._run_checked(["restore", f"--source={revision}", "--worktree", "--", ".", *exclude])

    # -- linked worktrees ----------------------------------------------------

    def add_worktree(self, destination: Path, commit: str) -> None:
        self._run_checked(["worktree", "add", "--detach", str(destination), commit])

    def remove_worktree(self, destination: Path) -> None:
        self._run(["worktree", "remove", "--force", str(destination)])

    def prune_worktrees(self) -> None:
        self._run(["worktree", "prune"])

    def worktree_head(self, worktree: Path) -> str:
        args = ["rev-parse", "HEAD"]
        result = run_git(
            args,
            cwd=worktree,
            env=git_environment(safe_directory=worktree, overrides=self._ENVIRONMENT),
        )
        if result.returncode != 0:
            raise self._failure(args, result)
        return _text(result.stdout).strip()

    # -- repository-local ignore rules ---------------------------------------

    def add_excludes(self, patterns: Sequence[str]) -> tuple[str, ...]:
        exclude_file = self._exclude_file
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_file.read_text() if exclude_file.exists() else ""
        have = set(existing.splitlines())
        new = tuple(pattern for pattern in dict.fromkeys(patterns) if pattern not in have)
        if not new:
            return ()
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        exclude_file.write_text(existing + prefix + "\n".join(new) + "\n")
        return new
