"""What answers on the socket before and after a session exists, and what says the process lives.

A process starts with nothing to run: it says who it is (``hello``) and waits for ``open``. ``open``
makes the one ``Engine`` of the process. A repeat for the same session answers the same and makes
none, so a host that lost track of a start can ask again.

The process is alive exactly while it holds ``HolderLock``. The host tries the same lock to find out,
because a dead process cannot answer, and the system frees the lock however the process ends. What the
host must end when it is gone is ``SessionProcessesFile``, written only while the lock is held.
"""

from __future__ import annotations

import fcntl
import os
import threading
from importlib import metadata
from typing import TYPE_CHECKING

import protovalidate

from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import TracebackType

    from vs_sidecar.engine import Engine

PROTOCOL = sc.SESSION_PROTOCOL_VERSION_CURRENT


class InvalidRequestError(ValueError):
    """A request that breaks the contract. The message names the field."""


class OtherSessionError(ValueError):
    """An open for a session other than the one this process holds."""


#: How many times a busy lock is tried, and how long to wait between tries. A look by the host lasts
#: microseconds; this is long enough to outlast many and short enough that a second process ends soon.
LOCK_ATTEMPTS = 25
LOCK_PAUSE_SECONDS = 0.02


def _pause() -> None:
    threading.Event().wait(LOCK_PAUSE_SECONDS)


class HolderLockedError(RuntimeError):
    """Another process holds the lock, so another process lives for this session."""


class HolderLock:
    """The exclusive lock on a file, held from ``take`` until ``release`` or the end of the process.

    The descriptor is not inherited by the processes this one starts, so an agent that outlives this
    process cannot keep the lock held and make the host believe this process still lives.
    """

    def __init__(self, descriptor: int) -> None:
        """Hold the lock *descriptor* already has."""
        self._descriptor: int | None = descriptor

    @classmethod
    def take(
        cls, path: Path, *, pause: Callable[[], None] = _pause, attempts: int = LOCK_ATTEMPTS
    ) -> HolderLock:
        """Take the lock on *path*. Raises ``HolderLockedError`` if another process keeps it.

        The host tries this lock to learn whether a process lives, and holds it for an instant while it
        does. A process that starts then must not mistake that look for a second process, so a busy lock
        is tried again up to *attempts* times, with *pause* between. A real second process holds it for
        its whole life, which is far longer than the tries last, so it still ends here.
        """
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        for attempt in range(attempts):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if attempt + 1 < attempts:
                    pause()
            except BaseException:
                os.close(descriptor)
                raise
            else:
                return cls(descriptor)
        os.close(descriptor)
        msg = f"another process holds {path}"
        raise HolderLockedError(msg)

    def release(self) -> None:
        """Free the lock. The system does the same when the process ends."""
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self) -> HolderLock:
        """Hold the lock for a block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Free the lock."""
        self.release()


def write_atomically(path: Path, data: bytes) -> None:
    """Replace *path* with *data* so a reader sees the old file or the new one."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


class SessionProcessesFile:
    """Keeps a ``SessionProcesses`` record of this process and its agents' groups at one path.

    Call it only while the lock is held: the host trusts the pid in it because of that. One thread
    calls ``record``; ``clear`` runs after that thread has stopped.
    """

    def __init__(
        self,
        path: Path,
        pid: int,
        groups: Callable[[], list[int]],
        started: Callable[[int], str],
    ) -> None:
        """Write to *path* the groups *groups* finds, for the process *pid*.

        *started* says when a group's leader started: an opaque token the host compares for equality
        before it ends the group, so that an id another program got later is not ended. It is read once
        for each group, when the group is first seen.
        """
        self._path = path
        self._pid = pid
        self._groups = groups
        self._started = started
        self._tokens: dict[int, str] = {}
        self._validator = protovalidate.Validator()
        self._written: bytes | None = None

    def record(self) -> None:
        """Rewrite the record if the groups changed."""
        self._write([group for group in self._groups() if group > 1])

    def clear(self) -> None:
        """Name no group: the agent is closed, so a later reap must end nothing."""
        self._write([])

    def _write(self, groups: list[int]) -> None:
        self._tokens = {group: self._tokens.get(group) or self._started(group) for group in groups}
        message = sc.SessionProcesses(
            holder_pid=self._pid,
            agent_process_groups=[
                sc.AgentProcessGroup(id=group, leader_started=self._tokens[group])
                for group in groups
            ],
        )
        self._validator.validate(message)
        data = message.SerializeToString(deterministic=True)
        if data != self._written:
            write_atomically(self._path, data)
            self._written = data


def versions() -> orch.BuildVersions:
    """What this build says it speaks."""
    try:
        release = metadata.version("vibesys-sidecar")
    except metadata.PackageNotFoundError:
        release = "dev"
    return orch.BuildVersions(
        binary_version=release, protocol_version=PROTOCOL, min_protocol_version=PROTOCOL
    )


def compatibility(client: orch.BuildVersions) -> orch.Compatibility:
    """Whether *client* and this build share a protocol."""
    if client.protocol_version < PROTOCOL:
        return orch.COMPATIBILITY_CLIENT_TOO_OLD
    if client.min_protocol_version > PROTOCOL:
        return orch.COMPATIBILITY_DAEMON_TOO_OLD
    return orch.COMPATIBILITY_COMPATIBLE


class SessionHolder:
    """The session of one process: none, and then the one ``open`` made."""

    def __init__(self, open_session: Callable[[sc.OpenSessionRequest], Engine]) -> None:
        """Make an engine with *open_session* on the first valid ``open``."""
        self._open_session = open_session
        self._validator = protovalidate.Validator()
        self._lock = threading.Lock()
        self._session: sc.OpenSessionResponse | None = None
        self._engine: Engine | None = None
        self._opened = threading.Event()

    def hello(self, request: sc.SessionHelloRequest) -> sc.SessionHelloResponse:
        """Say who answers here and whether they can work with the caller."""
        self._validate(request)
        response = sc.SessionHelloResponse(
            versions=versions(), compatibility=compatibility(request.client)
        )
        with self._lock:
            if self._session is not None:
                response.session.CopyFrom(self._session.session)
        return response

    def open(self, request: sc.OpenSessionRequest) -> sc.OpenSessionResponse:
        """Make the session *request* describes, or answer as the first open did.

        Raises ``InvalidRequestError`` before anything starts, and ``OtherSessionError`` for a
        request for a session this process does not hold.
        """
        self._validate(request)
        with self._lock:
            if self._session is None:
                self._engine = self._open_session(request)
                self._session = sc.OpenSessionResponse(session=request.session)
                self._opened.set()
            elif self._session.session != request.session:
                held = self._session.session
                msg = (
                    f"this process holds session {held.agent_id}/{held.session}, "
                    f"not {request.session.agent_id}/{request.session.session}"
                )
                raise OtherSessionError(msg)
            return self._session

    @property
    def engine(self) -> Engine | None:
        """The session's engine, once opened."""
        with self._lock:
            return self._engine

    def wait_opened(self, timeout: float) -> Engine | None:
        """The engine, once it exists, or ``None`` if none does after *timeout* seconds."""
        self._opened.wait(timeout)
        return self.engine

    def abandon_waiting(self) -> None:
        """Wake whoever waits in ``wait_opened``: the process is stopping."""
        self._opened.set()

    def _validate(self, request: sc.SessionHelloRequest | sc.OpenSessionRequest) -> None:
        try:
            self._validator.validate(request)
        except protovalidate.ValidationError as error:
            raise InvalidRequestError(str(error)) from error
