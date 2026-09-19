"""Read-only unified-patch access to a run's workspace repository.

Patch text is a server-side concern: the loop's own tracker never needs it,
so the one extra git read lives here instead of widening the tracker's API.
The discipline is exactly ``GitTracker.diff_name_status``'s: both revisions
must be plain object names so nothing on the command line can read as an
option, paths ride behind ``--`` as literal pathspecs, external diff drivers
are disabled, the call is bounded by a timeout, and every failure is reported
to a warning sink and returned as None rather than raised.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

#: Abbreviated or full object name. Deliberately not a revision expression:
#: this reader accepts only values that cannot be read as a git option.
_OBJECT_NAME = re.compile(r"^[0-9a-f]{7,64}$")

#: Bound on the read, so a wedged git cannot hold a frontend request thread
#: open forever. Matches the tracker's read helpers.
_READ_TIMEOUT_SECONDS = 10.0

#: Identity and repository-trust settings, mirroring the tracker's git
#: environment. Identity is inert for a read but keeps the invocations
#: uniform; ``safe.directory`` is appended per repository root.
_GIT_ENV_STATIC = {
    "GIT_AUTHOR_NAME": "vibesys",
    "GIT_AUTHOR_EMAIL": "vibesys@local",
    "GIT_COMMITTER_NAME": "vibesys",
    "GIT_COMMITTER_EMAIL": "vibesys@local",
}


class PatchDiffWarnings(Protocol):
    """Sink for non-fatal git faults, shaped like ``GitTrackerEvents.warning``."""

    def __call__(self, summary: str, *, detail: str | None = None) -> None:
        """Report one fault an operator should see."""
        ...


class WorkspacePatchReader:
    """Read one bounded ``git diff`` patch from a workspace repository.

    The root is taken as-is: a missing or non-repository directory makes the
    git call fail, which reports through the warning sink and reads as
    "content unavailable" at the caller, exactly the degradation a recorded
    run whose workspace is gone should get.
    """

    def __init__(self, root: Path, *, warning: PatchDiffWarnings) -> None:
        """Bind the reader to one repository root and one warning sink."""
        self._root = root
        self._warning = warning

    def diff_patch(self, base: str, head: str, paths: Sequence[str]) -> str | None:
        """Return the unified diff between two commits, limited to *paths*.

        Read-only: nothing about the workspace, index, or refs changes.
        Rename detection is on, so a renamed file queried with both of its
        paths arrives as one patch. Returns None when the range does not
        resolve in this repository, when git is unavailable, or when the
        query outruns ``_READ_TIMEOUT_SECONDS``; each of those is reported
        rather than collapsing silently at the caller.

        Both revisions must be plain object names. A value that is not one
        raises ``ValueError`` so a caller cannot smuggle a flag onto the git
        command line; paths are literal pathspecs for the same reason.
        """
        for value in (base, head):
            if _OBJECT_NAME.fullmatch(value) is None:
                raise ValueError(f"not a commit object name: {value!r}")  # noqa: TRY003  # Names the rejected value.
        command = [
            "git",
            "diff",
            "--no-ext-diff",
            "--find-renames",
            base,
            head,
            "--",
            *(f":(literal){path}" for path in paths),
        ]
        try:
            result = subprocess.run(  # noqa: S603  # Fixed argv, validated revisions, literal pathspecs.
                command,
                cwd=self._root,
                capture_output=True,
                check=False,
                env=self._environment(),
                timeout=_READ_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._warning(f"read-only patch failed: {' '.join(command)}", detail=str(error))
            return None
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            self._warning(f"read-only patch exit {result.returncode}", detail=stderr)
            return None
        return result.stdout.decode("utf-8", errors="replace")

    def _environment(self) -> dict[str, str]:
        """Inherited environment with repository selection pinned to the root."""
        env = os.environ.copy()
        for variable in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(variable, None)
        env.update(_GIT_ENV_STATIC)
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(self._root),
            }
        )
        return env


__all__ = ["PatchDiffWarnings", "WorkspacePatchReader"]
