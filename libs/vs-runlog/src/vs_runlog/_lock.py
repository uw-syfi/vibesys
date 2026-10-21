"""The writer's lifetime lock, shared with the gateway's nonblocking probe."""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def assert_run_unlocked(log_path: Path) -> None:
    """Probe a run without blocking; the writer must still acquire its lifetime lock."""
    if not log_path.parent.exists():
        return
    try:
        lock = RunLock(log_path.parent / "run.lock")
    except BlockingIOError as error:
        message = "cannot relaunch: run.lock is held by a live writer"
        raise RuntimeError(message) from error
    lock.close()


class RunLock:
    """Own an exclusive advisory lock without replacing or deleting its inode."""

    def __init__(self, path: Path) -> None:
        self._stream = path.open("ab")
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._stream.close()
            raise

    def close(self) -> None:
        """Close releases the lock, including after partial writer initialization."""
        self._stream.close()


class RunLaunchLockedError(RuntimeError):
    """Another launcher is still admitting this run."""


@contextmanager
def exclusive_run_launch(log_path: Path) -> Iterator[None]:
    """Serialize launch admission until a writer publishes its new epoch.

    Distinct from run.lock: the child must acquire the writer lock while its
    launcher still owns admission. Contention raises RunLaunchLockedError; close or
    process exit releases ownership, including an interrupted launch.
    """
    try:
        lock = RunLock(log_path.parent / "launch.lock")
    except BlockingIOError as error:
        message = "another launch holds launch.lock"
        raise RunLaunchLockedError(message) from error
    try:
        yield
    finally:
        lock.close()
