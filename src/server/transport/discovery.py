"""Crash-safe discovery for one project-local web gateway."""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - VibeSys currently targets Unix hosts.
    fcntl = None  # type: ignore[assignment]

_PROCESS_TABLE = Path("/proc")
"""Where per-process open descriptors are readable, when the host exposes them."""

CAPABILITY_ROTATION_HEADER = "X-VibeSys-Rotate-Capability"
"""Header that makes a capability-authenticated rotation request explicit."""

CAPABILITY_ROTATION_PATH = "/_vibesys/rotate-capability"
"""Private HTTP route used by the local lifecycle command."""


@dataclass(frozen=True)
class WebInstanceRecord:
    """The capability and liveness facts for one local gateway."""

    pid: int
    port: int
    token: str
    url: str
    project_root: str
    started_at: float

    @classmethod
    def discover(cls, path: Path, *, cleanup_stale: bool = True) -> WebInstanceRecord | None:
        """Return a live record, removing malformed or dead state if requested."""
        record = _read_record(path)
        if record is None:
            return None
        if _pid_alive(record.pid) and _probe(record):
            return record
        if cleanup_stale:
            record.remove_if_owner(path)
        return None

    @classmethod
    def read(cls, path: Path) -> WebInstanceRecord | None:
        """Return the published record without probing the gateway it names.

        `discover` answers "can I hand this URL to a browser", which needs a
        health probe and so is both slower and load-sensitive. This answers the
        weaker "which process published this instance", which is what a caller
        that only wants to signal or identify the gateway needs, and unlike
        `discover` it still answers that while the gateway is starting up or
        shutting down. It says nothing about liveness: a record outlives a
        gateway that was killed, and a `None` return only means no record is
        readable right now, not that nothing is running.
        """
        return _read_record(path)

    @classmethod
    def from_gateway(
        cls, *, pid: int, port: int, token: str, project_root: Path
    ) -> WebInstanceRecord:
        """Build a record only after the gateway has successfully bound."""
        return cls(
            pid=pid,
            port=port,
            token=token,
            url=f"http://127.0.0.1:{port}/?token={token}",
            project_root=str(project_root.resolve()),
            started_at=time.time(),
        )

    def write(self, path: Path) -> None:
        """Publish this record atomically with owner-only permissions."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
        try:
            temporary.write_text(
                json.dumps({"version": 1, **self.__dict__}, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def with_token(self, token: str) -> WebInstanceRecord:
        """Return this gateway record with a replacement launch capability."""
        return WebInstanceRecord(
            pid=self.pid,
            port=self.port,
            token=token,
            url=f"http://127.0.0.1:{self.port}/?token={token}",
            project_root=self.project_root,
            started_at=self.started_at,
        )

    @property
    def capability_rotation_url(self) -> str:
        """Return the authenticated local endpoint that rotates this record."""
        return f"http://127.0.0.1:{self.port}{CAPABILITY_ROTATION_PATH}?token={self.token}"

    def remove_if_owner(self, path: Path) -> None:
        """Remove only the record that still points at this gateway."""
        current = _read_record(path)
        if current == self:
            path.unlink(missing_ok=True)


@dataclass(frozen=True)
class WebInstanceHold:
    """What this host can observe about the processes using one instance directory.

    The launcher opens the gateway's startup log, takes an exclusive lock on
    that descriptor with ``take``, and hands the same descriptor to the
    detached child as its stdout and stderr. Lock and log are then one open
    file description, so the release of the lock *is* the last close of that
    description, and that close is the exit of the gateway or of any descendant
    that inherited it. ``log_locked`` therefore outlives the instance record,
    which the gateway unlinks as the first step of teardown, and it covers a
    descendant that a check on the gateway's own pid would miss.

    ``log_locked`` alone does not answer "is this directory free". It says
    nothing about a gateway started without ``--detach``, which writes no
    startup log at all; nothing about a log left open by a launcher that
    predates the lock; and nothing about an unrelated process that opened a
    file in the directory. ``holders`` covers those by listing the processes
    that have a file open under the directory, and ``free`` requires both
    observations to say "nothing".

    Neither observation is always available, so both are three-valued:

    - ``holders is None``: this host does not expose per-process descriptors,
      so ``log_locked`` is the only evidence available.
    - ``holders == ()``: no process this user can inspect has a file open under
      the directory. Processes owned by another user are not inspectable, which
      is why ``log_locked`` is kept as an independent witness.
    - ``log_locked is None``: the lock state could not be read, because the log
      could not be opened or the filesystem refused the lock.

    The lock is a BSD ``flock``, not POSIX record locking, and inheritance
    across ``fork`` is what the whole guarantee rests on. That holds on a local
    filesystem and on an NFS mount with ``local_lock``. An NFS client without
    it emulates ``flock`` with whole-file POSIX locks, which are owned per
    process, are not inherited, and drop when the owner closes any descriptor
    for the file, so ``take`` would not cover the child there. ``observe``
    answers ``log_locked=None`` rather than guessing whenever the lock call
    fails, and a caller that cannot establish ``free`` must not treat the
    directory as idle.
    """

    holders: tuple[int, ...] | None
    log_locked: bool | None

    @staticmethod
    def log_path(instance_path: Path) -> Path:
        """Return the startup log path the launcher opens for this instance."""
        return instance_path.with_name(f"{instance_path.name}.log")

    @staticmethod
    def take(descriptor: int) -> bool:
        """Lock ``descriptor`` exclusively, or report that something else holds it."""
        if fcntl is None:  # pragma: no cover - defensive for non-Unix packaging.
            return True
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    @classmethod
    def observe(cls, instance_path: Path) -> WebInstanceHold:
        """Read both observations for ``instance_path`` without changing either."""
        return cls(
            holders=_processes_with_files_under(instance_path.parent),
            log_locked=_log_lock_state(cls.log_path(instance_path)),
        )

    @property
    def free(self) -> bool:
        """Report that no process this host can observe is using the directory.

        False whenever there is positive evidence of use *or* the lock state
        could not be read, so a directory this host cannot inspect is never
        mistaken for an idle one.
        """
        return self.log_locked is False and not self.holders


class WebInstanceClaim:
    """A project-local startup lock held for the gateway lifetime."""

    def __init__(self, path: Path) -> None:
        """Initialize the lock path beside the instance record."""
        self.path = path.with_name(f"{path.name}.lock")
        self._stream: Any = None

    def try_acquire(self) -> bool:
        """Acquire the non-blocking claim, or report that another launch owns it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+")
        if fcntl is None:  # pragma: no cover - defensive for non-Unix packaging.
            self._stream = stream
            return True
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return False
        self._stream = stream
        return True

    @classmethod
    def is_held(cls, path: Path) -> bool:
        """Return whether another process currently owns the startup claim."""
        if fcntl is None:  # pragma: no cover - defensive for non-Unix packaging.
            return False
        try:
            stream = path.with_name(f"{path.name}.lock").open("a+")
        except OSError:
            return False
        try:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            stream.close()

    def close(self) -> None:
        """Release the startup claim without deleting the reusable lock path."""
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _log_lock_state(log_path: Path) -> bool | None:
    """Report whether something holds the startup log's exclusive lock.

    The probe takes a *shared* lock. That is enough to detect an exclusive one,
    because the two conflict, and it does not exclude another reader, so asking
    the question does not make the asker a holder and two callers asking at
    once cannot report each other. It also needs only read access, which an
    exclusive POSIX record lock (what an NFS client without ``local_lock`` uses
    to emulate ``flock``) would refuse on a read-only descriptor.

    The residual is that a launcher's ``take`` can still lose to a probe that
    holds the shared lock at that instant. The probe holds it for the duration
    of two syscalls, so the window is microseconds per observation.
    """
    if fcntl is None:  # pragma: no cover - defensive for non-Unix packaging.
        return None
    try:
        descriptor = os.open(log_path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return False


def _processes_with_files_under(directory: Path) -> tuple[int, ...] | None:
    """List the processes with a file open under ``directory``, ascending.

    ``None`` means this host does not expose per-process descriptors, so the
    question cannot be answered here at all. The calling process is excluded:
    it is asking precisely because it wants to take the directory.
    """
    if not (_PROCESS_TABLE / "self" / "fd").is_dir():
        return None
    try:
        entries = list(_PROCESS_TABLE.iterdir())
    except OSError:  # pragma: no cover - the process table vanishing mid-scan.
        return None
    prefix = f"{directory.resolve()}{os.sep}"
    own = os.getpid()
    return tuple(
        sorted(
            pid
            for pid in (int(entry.name) for entry in entries if entry.name.isdigit())
            if pid != own and _has_file_under(_PROCESS_TABLE / str(pid) / "fd", prefix)
        )
    )


def _has_file_under(descriptor_directory: Path, prefix: str) -> bool:
    try:
        descriptors = list(descriptor_directory.iterdir())
    except OSError:
        # The process exited mid-scan, or it belongs to another user. Either
        # way this scan cannot see it, and `log_locked` is the witness that can.
        return False
    for descriptor in descriptors:
        try:
            target = descriptor.readlink()
        except OSError:
            continue
        # An unlinked file reads back as "<path> (deleted)", which still keeps
        # the directory non-empty on a filesystem that silly-renames it.
        if str(target).startswith(prefix):
            return True
    return False


def _read_record(path: Path) -> WebInstanceRecord | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return None
        return WebInstanceRecord(
            pid=_positive_int(raw["pid"]),
            port=_port(raw["port"]),
            token=_nonempty_string(raw["token"]),
            url=_nonempty_string(raw["url"]),
            project_root=_nonempty_string(raw["project_root"]),
            started_at=float(raw["started_at"]),
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _probe(record: WebInstanceRecord) -> bool:
    health = f"http://127.0.0.1:{record.port}/health?token={record.token}"
    try:
        with urllib.request.urlopen(health, timeout=0.4) as response:  # noqa: S310  # lint-waiver: LW-101049 [S310]; probe only the loopback health URL recorded by this process
            return response.status == 200 and response.read() == b"vibesys-ok\n"  # noqa: PLR2004  # lint-waiver: LW-101050 [PLR2004]; compare the fixed health response status required by the protocol
    except (OSError, urllib.error.URLError):
        return False


def _positive_int(value: object) -> int:
    if not isinstance(value, (int, str)) or isinstance(value, bool):
        raise TypeError("expected a positive integer")  # noqa: TRY003  # lint-waiver: LW-101051 [TRY003]; report malformed persisted instance metadata
    result = int(value)
    if result <= 0:
        raise ValueError("expected a positive integer")  # noqa: TRY003  # lint-waiver: LW-101052 [TRY003]; report invalid persisted instance metadata
    return result


def _port(value: object) -> int:
    if not isinstance(value, (int, str)) or isinstance(value, bool):
        raise TypeError("expected a TCP port")  # noqa: TRY003  # lint-waiver: LW-101053 [TRY003]; report malformed persisted port metadata
    result = int(value)
    if not 0 < result <= 65_535:  # noqa: PLR2004  # lint-waiver: LW-101054 [PLR2004]; enforce the protocol's fixed TCP port range
        raise ValueError("expected a TCP port")  # noqa: TRY003  # lint-waiver: LW-101055 [TRY003]; report an out-of-range persisted port
    return result


def _nonempty_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("expected a non-empty string")  # noqa: TRY003  # lint-waiver: LW-101056 [TRY003]; report malformed persisted instance metadata
    return value
