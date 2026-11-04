"""The writer's lifetime lock, shared with the gateway's nonblocking probe."""

from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vs_proto.api import RunProcess


class WriterIdentityState(StrEnum):
    """Observation of the lifetime lock for one published writer identity."""

    HELD = "held"
    RELEASED = "released"
    UNKNOWN = "unknown"


def writer_identity_path(log_path: Path, process: RunProcess) -> Path:
    """The canonical lifetime-lock inode for one published writer identity."""
    identity = hashlib.sha256(process.to_binary()).hexdigest()
    return log_path.parent / f"writer-{identity}.lock"


def hold_writer_identity(log_path: Path, process: RunProcess) -> RunLock:
    """Pin this published identity until its writer releases all resources."""
    return RunLock(writer_identity_path(log_path, process))


def observe_writer_identity(log_path: Path, process: RunProcess) -> WriterIdentityState:
    """Probe only this writer's lock; a successor cannot prove its predecessor alive.

    Older writers lack the lock, which is unknown rather than evidence of exit.
    Lock files stay in place, so probing never replaces a held inode.
    """
    if not process.pid or not process.epoch or process.started_at is None:
        return WriterIdentityState.UNKNOWN
    path = writer_identity_path(log_path, process)
    if not path.is_file():
        return WriterIdentityState.UNKNOWN
    try:
        lock = RunLock(path)
    except BlockingIOError:
        return WriterIdentityState.HELD
    lock.close()
    return WriterIdentityState.RELEASED


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
