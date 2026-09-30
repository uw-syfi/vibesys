"""Crash-safe discovery for one project-local web gateway."""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import (
    Path,  # noqa: TC003  # lint-waiver: LW-101048 [TC003]; retain the runtime annotation type used by the discovery record API
)
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - VibeSys currently targets Unix hosts.
    fcntl = None  # type: ignore[assignment]


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
        that only wants to signal or identify the gateway needs, and it stays
        correct while the gateway is starting up or shutting down.
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

    def remove_if_owner(self, path: Path) -> None:
        """Remove only the record that still points at this gateway."""
        current = _read_record(path)
        if current == self:
            path.unlink(missing_ok=True)


class WebInstanceHold:
    """A detached gateway's observable hold on its instance directory.

    The hold is an exclusive lock on the gateway's startup log, which the
    launcher opens before spawning and the detached child then keeps as its
    stdout and stderr until its interpreter exits. Locking that exact
    descriptor makes the hold and the last open file under the instance
    directory one open file description, so the kernel releases the hold in the
    same operation that closes the log. A caller that observes ``is_held`` go
    false may therefore reuse or remove the directory, including on a
    filesystem where unlinking a file another process still has open leaves the
    directory non-empty.

    Nothing weaker carries that guarantee. The instance record is unlinked as
    the *first* step of teardown, so its absence says nothing about the files.
    A lock on a separate file is released in an earlier ``__fput`` than the
    log's, so it reads free while the log is still open. The hold also covers
    any descendant that inherited the log, which a check on the gateway's own
    pid would miss.
    """

    @staticmethod
    def log_path(instance_path: Path) -> Path:
        """Return the startup log path the launcher opens for this instance."""
        return instance_path.with_name(f"{instance_path.name}.log")

    @staticmethod
    def take(descriptor: int) -> bool:
        """Hold ``descriptor``, or report that another gateway already holds it."""
        if fcntl is None:  # pragma: no cover - defensive for non-Unix packaging.
            return True
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    @classmethod
    def is_held(cls, instance_path: Path) -> bool:
        """Return whether any process still has this instance's files open."""
        if fcntl is None:  # pragma: no cover - defensive for non-Unix packaging.
            return False
        try:
            descriptor = os.open(cls.log_path(instance_path), os.O_RDONLY)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)


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
