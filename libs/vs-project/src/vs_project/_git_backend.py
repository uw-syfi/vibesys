"""Which ``GitRepository`` implementation serves a project: the one place that decides.

``GitTracker`` asks :func:`open_git_repository` for its repository unless the
caller injects one. The choice is a value, not an ``except ImportError``:

* ``VIBESYS_GIT_BACKEND`` (``cli`` or ``pygit2``) selects explicitly. An
  unknown value, or ``pygit2`` when the package is not installed on this
  platform, is an error that names the variable, never a quiet switch.
* Unset, the default is :data:`DEFAULT_GIT_BACKEND` when it is available and
  the CLI implementation when ``pygit2`` is not installed. That fallback is
  deliberate wiring: the CLI implementation is complete on its own and passes
  the same contract suite.

``pygit2`` is imported only when the libgit2 implementation is built, so a
platform without a wheel can still import ``vs_project``.
"""

from __future__ import annotations

import importlib.util
import os
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_project._cli_git_repository import CliGitRepository
from vs_project.api.git_repository import GitError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from vs_project.api.git_repository import GitFaultSink, GitRepository

GIT_BACKEND_ENV = "VIBESYS_GIT_BACKEND"


class GitBackend(StrEnum):
    """The ``GitRepository`` implementations a project can run on."""

    CLI = "cli"
    """``CliGitRepository``: every operation runs ``git``."""

    PYGIT2 = "pygit2"
    """``Pygit2GitRepository``: libgit2 in process, the CLI for the cli-only operations."""


DEFAULT_GIT_BACKEND = GitBackend.PYGIT2
"""The implementation used when ``VIBESYS_GIT_BACKEND`` is unset and it is installed."""


class GitBackendError(GitError):
    """``VIBESYS_GIT_BACKEND`` names an unknown or unavailable implementation."""


def pygit2_installed() -> bool:
    """Whether the ``pygit2`` package can be imported on this platform."""
    return importlib.util.find_spec("pygit2") is not None


def select_git_backend(requested: str | None, *, pygit2_available: bool) -> GitBackend:
    """Resolve the configured name (``None`` or empty for "use the default") to a backend."""
    if not requested:
        return DEFAULT_GIT_BACKEND if pygit2_available else GitBackend.CLI
    try:
        backend = GitBackend(requested)
    except ValueError:
        choices = ", ".join(member.value for member in GitBackend)
        message = f"{GIT_BACKEND_ENV}={requested!r} is not one of: {choices}"
        raise GitBackendError(message) from None
    if backend is GitBackend.PYGIT2 and not pygit2_available:
        message = f"{GIT_BACKEND_ENV}=pygit2 requires the pygit2 package, which is not installed"
        raise GitBackendError(message)
    return backend


def open_git_repository(
    root: Path,
    *,
    faults: GitFaultSink,
    environ: Mapping[str, str] | None = None,
) -> GitRepository:
    """Build the configured ``GitRepository`` over the worktree top level ``root``."""
    configured = (os.environ if environ is None else environ).get(GIT_BACKEND_ENV)
    backend = select_git_backend(configured, pygit2_available=pygit2_installed())
    if backend is GitBackend.CLI:
        return CliGitRepository(root, faults=faults)
    from vs_project._pygit2_git_repository import (  # noqa: PLC0415  # lint-waiver: LW-415570 [PLC0415]; importing pygit2 only when this backend is chosen is what keeps a platform without the wheel importable.
        Pygit2GitRepository,
    )

    return Pygit2GitRepository(root, faults=faults)
