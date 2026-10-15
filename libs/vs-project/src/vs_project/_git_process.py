"""The one way VibeSys starts Git subprocesses on repositories it manages.

Git runs auto maintenance after commits, fetches, and merges, and from Git 2.47
it detaches that process by default. The detached process outlives the command
and keeps writing under ``.git`` while VibeSys removes, moves, or reuses the
repository ("Directory not empty: .git"). Every Git command VibeSys starts
therefore carries ``maintenance.auto=false`` and ``gc.auto=0``; the user's own
Git commands still maintain their repositories.

Every command also runs no program the repository names: hooks (``core.hooksPath`` is
``/dev/null``), the file-system monitor (``core.fsmonitor``) and commit or tag signing
(``gpg.program``). The repository's config, hooks and worktree are agent-writable in a
workspace, so any of them would run agent-chosen code on the host, unconfined, at the
next snapshot. Git never enters a nested repository (``diff.ignoreSubmodules``,
``submodule.recurse``), whose config would name programs too; ``_nested_repositories``
refuses such a work tree outright, since ``git add`` checks a gitlink regardless. Only the
work tree's own ``.gitattributes`` and ``.git/info/attributes`` assign attributes: the
user's global and the system attributes files are off, so attributes that select a
driver come from the repository alone.

``tests/architecture/test_git_invocation_boundary.py`` fails when code under
``src/`` or ``libs/`` starts Git without going through this module.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_project.api.git_repository import GitCommandError
from vs_sim.api import (
    ProcessGroupUnreaped,
    ProcessOutcome,
    ProcessOutputTooLarge,
    ProcessSpec,
    ProcessTimedOut,
    ProcessTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from vs_sim.api import Processes

# The status a run stopped for too much output reports: its group was killed with SIGKILL.
_KILLED_STATUS = -9

# Variables that make Git operate on a repository other than the one at ``cwd``.
_SELECTION_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

_NO_BACKGROUND_MAINTENANCE: tuple[tuple[str, str], ...] = (
    ("maintenance.auto", "false"),
    ("gc.auto", "0"),
)

# Programs a repository can make Git run; none runs on the host (see the module docstring).
_NO_REPOSITORY_PROGRAMS: tuple[tuple[str, str], ...] = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("diff.ignoreSubmodules", "all"),
    ("submodule.recurse", "false"),
    ("core.attributesFile", "/dev/null"),
)


def git_config_env(
    extra_config: Sequence[tuple[str, str]] = (),
) -> dict[str, str]:
    """Return ``GIT_CONFIG_*`` variables for ``extra_config`` plus the host Git policy.

    The policy (no background maintenance, no repository programs) comes last, so
    ``extra_config`` cannot override it.
    """
    config = (*extra_config, *_NO_BACKGROUND_MAINTENANCE, *_NO_REPOSITORY_PROGRAMS)
    result = {"GIT_CONFIG_COUNT": str(len(config))}
    for index, (key, value) in enumerate(config):
        result[f"GIT_CONFIG_KEY_{index}"] = key
        result[f"GIT_CONFIG_VALUE_{index}"] = value
    return result


def git_environment(
    *,
    safe_directory: Path | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the process environment for one Git command.

    Starts from ``os.environ`` without the repository-selection variables and with terminal
    prompts off, then applies ``overrides`` (which may set them again on purpose), then turns
    off the system attributes file and adds the Git config that trusts ``safe_directory``
    and carries the host Git policy.
    ``overrides`` cannot set ``GIT_CONFIG_*``: the config is owned here.
    """
    env = {key: value for key, value in os.environ.items() if key not in _SELECTION_VARIABLES}
    # Git runs in a process group of its own, outside the terminal's foreground group, where
    # reading a credential prompt from the terminal would stop it; it fails instead.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update(overrides or {})
    env["GIT_ATTR_NOSYSTEM"] = "1"
    env = {key: value for key, value in env.items() if not key.startswith("GIT_CONFIG_")}
    safe = (("safe.directory", str(safe_directory)),) if safe_directory is not None else ()
    env.update(git_config_env(safe))
    return env


def _require_policy(env: Mapping[str, str]) -> None:
    """Refuse ``env`` unless its last ``GIT_CONFIG_*`` entries are the host Git policy."""
    policy = (*_NO_BACKGROUND_MAINTENANCE, *_NO_REPOSITORY_PROGRAMS)
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    first = count - len(policy)
    given = tuple(
        (env.get(f"GIT_CONFIG_KEY_{index}"), env.get(f"GIT_CONFIG_VALUE_{index}"))
        for index in range(max(first, 0), count)
    )
    if first < 0 or given != policy:
        message = "a Git environment must come from git_environment(), which owns its config"
        raise ValueError(message)


@dataclass(frozen=True)
class GitText:
    """How a Git command ended, with its output decoded as text."""

    args: tuple[str, ...]
    """The command's arguments, without the ``git`` executable."""
    returncode: int
    stdout: str
    stderr: str

    def check(self) -> GitText:
        """Return this result, or raise :class:`GitCommandError` when the command failed."""
        if self.returncode != 0:
            raise GitCommandError(["git", *self.args], self.returncode, self.stderr.strip())
        return self


class Git:
    """Starts Git commands through ``processes``, with the environment policy above.

    Each command runs to completion with its output captured; the caller judges the exit code.
    ``OSError`` (``FileNotFoundError`` without Git) comes from ``processes``. A command that
    outlives its timeout raises :class:`~vs_sim.api.ProcessTimeoutError`; when killing it left
    something running, the error carries a note that says what.
    """

    def __init__(self, processes: Processes) -> None:
        """Run every Git command through ``processes``."""
        self._processes = processes

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> ProcessOutcome:
        """Run ``git <args>`` in ``cwd``; ``args`` excludes the ``git`` executable.

        ``env`` comes from ``git_environment`` and defaults to ``git_environment()``; an
        ``env`` without the host Git policy raises ``ValueError`` before Git starts.
        """
        argv = (shutil.which("git") or "git", *args)
        environment = git_environment() if env is None else env
        _require_policy(environment)
        spec = ProcessSpec(argv, cwd=cwd, env=environment)
        match self._processes.run(spec, timeout=timeout):
            case ProcessOutcome() as outcome:
                return outcome
            case ProcessTimedOut(timeout=limit, stdout=stdout, stderr=stderr):
                raise ProcessTimeoutError(list(argv), limit, output=stdout, stderr=stderr)
            case ProcessGroupUnreaped(timeout=limit, detail=detail):
                error = ProcessTimeoutError(list(argv), limit)
                error.add_note(detail)
                raise error
            case ProcessOutputTooLarge(limit=limit, stream=stream, stderr=stderr, detail=detail):
                error = GitCommandError(
                    args,
                    _KILLED_STATUS,
                    f"git wrote more than {limit} bytes to {stream}; "
                    f"stderr began: {stderr.decode(errors='replace')}",
                )
                if detail:
                    error.add_note(detail)
                raise error

    def run_text(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> GitText:
        """Like :meth:`run`, with stdout and stderr decoded (undecodable bytes replaced)."""
        outcome = self.run(args, cwd=cwd, env=env, timeout=timeout)
        return GitText(
            tuple(args),
            outcome.returncode,
            outcome.stdout.decode(errors="replace"),
            outcome.stderr.decode(errors="replace"),
        )
