"""Running an external harness (a load generator another project ships) as a child process.

The harness runs in a session of its own and is given a deadline. Whatever
way the call ends (return, failure, the deadline, ``Stopped`` from a signal),
the whole process group is killed, so services the harness started do not
outlive the sample. It knows nothing of what the harness measures.
"""

from __future__ import annotations

import os
import signal
import subprocess
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol

from vibesys_evaluator._private.sample import SampleError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


class HarnessError(SampleError):
    """The harness could not start, ran past its deadline, or exited non-zero."""


class Runner(Protocol):
    """Runs one harness command to completion."""

    def __call__(self, argv: Sequence[str], *, timeout_secs: float, cwd: Path) -> None:
        """Run ``argv`` in ``cwd``; raise ``HarnessError`` unless it exits 0 within the deadline."""
        ...


def run_harness(
    argv: Sequence[str], *, timeout_secs: float, cwd: Path, env: Mapping[str, str] | None = None
) -> None:
    """Run ``argv`` to completion in its own process group; kill the group on every exit.

    It runs with exactly ``env``, or this process's environment when None.

    Raises:
        HarnessError: It could not start, ran past ``timeout_secs``, or exited non-zero.
    """
    try:
        process = subprocess.Popen(list(argv), cwd=cwd, env=None if env is None else dict(env), start_new_session=True)
    except OSError as error:
        msg = f"cannot start {argv[0]!r}: {error.strerror}"
        raise HarnessError(msg) from error
    try:
        code = process.wait(timeout=timeout_secs)
    except subprocess.TimeoutExpired as error:
        msg = f"{' '.join(argv[:4])} ... ran past its deadline of {timeout_secs:.0f} s"
        raise HarnessError(msg) from error
    finally:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    if code != 0:
        msg = f"{' '.join(argv[:4])} ... exited {code}"
        raise HarnessError(msg)
