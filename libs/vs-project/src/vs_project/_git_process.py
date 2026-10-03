"""The one way VibeSys starts Git subprocesses on repositories it manages.

Git runs auto maintenance after commits, fetches, and merges, and from Git 2.47
it detaches that process by default. The detached process outlives the command
and keeps writing under ``.git`` while VibeSys removes, moves, or reuses the
repository ("Directory not empty: .git"). Every Git command VibeSys starts
therefore carries ``maintenance.auto=false`` and ``gc.auto=0``; the user's own
Git commands still maintain their repositories.

``tests/architecture/test_git_invocation_boundary.py`` fails when code under
``src/`` or ``libs/`` starts Git without going through this module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING, Literal, overload

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

# Variables that make Git operate on a repository other than the one at ``cwd``.
_SELECTION_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

_NO_BACKGROUND_MAINTENANCE: tuple[tuple[str, str], ...] = (
    ("maintenance.auto", "false"),
    ("gc.auto", "0"),
)


def git_config_env(
    extra_config: Sequence[tuple[str, str]] = (),
) -> dict[str, str]:
    """Return ``GIT_CONFIG_*`` variables for ``extra_config`` plus the no-maintenance pair."""
    config = (*extra_config, *_NO_BACKGROUND_MAINTENANCE)
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

    Starts from ``os.environ`` without the repository-selection variables, then
    applies ``overrides`` (which may set them again on purpose), then the Git
    config that trusts ``safe_directory`` and disables background maintenance.
    ``overrides`` cannot set ``GIT_CONFIG_*``: the config is owned here.
    """
    env = {key: value for key, value in os.environ.items() if key not in _SELECTION_VARIABLES}
    env.update(overrides or {})
    env = {key: value for key, value in env.items() if not key.startswith("GIT_CONFIG_")}
    safe = (("safe.directory", str(safe_directory)),) if safe_directory is not None else ()
    env.update(git_config_env(safe))
    return env


@overload
def run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    text: Literal[True],
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]: ...


@overload
def run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    text: Literal[False] = False,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]: ...


def run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    text: bool = False,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run ``git <args>`` in ``cwd`` with output captured and no exit-code check.

    ``args`` excludes the ``git`` executable. ``env`` comes from
    ``git_environment`` and defaults to ``git_environment()``. Raises ``FileNotFoundError`` when
    Git is not installed and ``subprocess.TimeoutExpired`` past ``timeout``.
    """
    git = shutil.which("git") or "git"
    return subprocess.run(  # noqa: S603  # lint-waiver: LW-010301 [S603]; the argv is an internally built Git command run without a shell, and this is the single place VibeSys starts Git.
        [git, *args],
        cwd=cwd,
        capture_output=True,
        text=text,
        env=git_environment() if env is None else env,
        timeout=timeout,
        check=False,
    )
