"""Per-user, node-local registry of live detached VibeSys servers.

A detached server outlives the shell that started it, so a client that later
reaches the host (over SSH, from the desktop app) needs to find it. This module
is that directory, and its one guarantee is that liveness is *proven*, never
read from file contents: each server holds an exclusive ``flock`` on its own
lock file for its whole lifetime (``LifetimeLock``), the kernel drops it on any
exit (``kill -9`` included), and a lister that observes the lock free knows
the owner is gone and removes its files. There are no pid checks (pids are
reused), no heartbeats, and no cleanup daemon.

The registry answers "what is running on this node now". Finished runs are
the per-project run history's business and are reopened read-only from it.

Layout under ``instance_root``::

    instances/<id>.lock   lifetime lock, published only once held
    instances/<id>.json   LiveInstanceRecord, written by the holder
    runs/<id>/control.sock, runs/<id>/server.log

Public surface: the record and CLI result models, ``instance_root``,
``checkout_root`` and ``running_checkout`` (the record's ``vibesys_root``),
``InstanceStore`` with its ``FileInstanceStore`` and ``FakeInstanceStore``
implementations, ``StopRequester`` with ``ControlSocketStopRequester`` and
the ``StopEffects`` a stop uses, the
pure ``judge`` and ``survey`` decisions, and ``LiveRegistry``, which composes
them.
"""

from __future__ import annotations

import os
import platform
import re
import secrets
import stat
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, PositiveInt, StringConstraints, ValidationError

from server.api.protocol import PROTOCOL_VERSION, Response, StopCommand
from server.transport.discovery import LifetimeLock, LockState, probe_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from vs_sim.api import Clock, Connection, Network, ProcessSignaller

INSTANCE_ID_PATTERN = r"^[0-9a-f]{12}$"
_INSTANCE_ID = re.compile(INSTANCE_ID_PATTERN)
InstanceId = Annotated[str, StringConstraints(pattern=INSTANCE_ID_PATTERN)]
"""A registry key: 12 lowercase hex digits, so it is never a path fragment."""

_RECORD_SUFFIX = ".json"
_LOCK_SUFFIX = ".lock"
_PRIVATE_DIRECTORY = 0o700
_GROUP_OR_OTHER = 0o077
_FALLBACK_PARENT = Path("/tmp")  # noqa: S108  # lint-waiver: LW-178202 [S108]; the per-user runtime fallback must be a short, node-local, session-independent path; `tempfile.gettempdir()` follows `$TMPDIR`, which differs between macOS GUI and SSH sessions and can be long enough to break the Unix socket path limit
"""Parent of the fallback root when ``$XDG_RUNTIME_DIR`` is unset."""
_STOP_POLL_SECONDS = 0.05
STOP_TIMEOUT_SECONDS = 10.0
"""How long ``stop`` waits for a stopped server to finish its run teardown."""
CONTROL_REPLY_TIMEOUT_SECONDS = 5.0
"""How long a stop request waits to connect and for the server's acknowledgment."""
_MAX_REPLY_BYTES = 1 << 20
_CHECKOUT_DEPTH = 2
"""``src/<package>/<module>.py``: the checkout is this many parents above the module."""


class InstanceStatus(StrEnum):
    """How far a live server has come; run status itself is on its socket."""

    STARTING = "starting"
    """The server holds its lock; its control socket is not accepting yet."""
    SERVING = "serving"
    """The control socket accepts connections."""


class LiveInstanceRecord(BaseModel):
    """What a live detached server publishes about itself (version 1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    id: InstanceId
    status: InstanceStatus
    socket_path: str
    project_root: str
    run_id: str | None = None
    pid: PositiveInt
    started_at: float
    hostname: str
    protocol_version: Literal[1] = PROTOCOL_VERSION
    vibesys_version: str
    vibesys_root: str | None = None
    """Absolute path of the VibeSys source checkout this server runs from.

    ``None`` when the server runs from an installed distribution rather than a
    checkout, and in records written before the field existed (the field is
    additive, so the record stays version 1). A client uses it to suggest the
    checkout it should run ``vibesys`` from on this host.
    """


class InstanceList(BaseModel):
    """``vibesys instances list --json``: the live servers on this node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    instances: tuple[LiveInstanceRecord, ...]
    unverified: tuple[InstanceId, ...] = ()
    """Records whose lock this host could not read; neither live nor removed."""


class StopOutcome(StrEnum):
    """What ``vibesys instances stop`` observed."""

    STOPPED = "stopped"
    """The server was live, was asked or signalled to stop, and released its lock."""
    STOPPING = "stopping"
    """The server accepted a stop request but its run has not reached a safe boundary yet.

    Distinct from ``STILL_RUNNING``: this is the normal path for a run whose
    active agent call outlasts the wait, and the server exits on its own once
    the call ends. Poll ``instances list`` rather than stopping again.
    """
    NOT_RUNNING = "not_running"
    """No live server holds this id; nothing was signalled."""
    STILL_RUNNING = "still_running"
    """The server was signalled but still held its lock when the wait ended."""
    UNSUPPORTED = "unsupported"
    """The server did not answer its control socket, and this host cannot signal
    a process without risking a reused pid."""


class StopRoute(StrEnum):
    """How ``vibesys instances stop`` reached the server."""

    CONTROL_SOCKET = "control_socket"
    """A ``command.stop`` request, acknowledged on the server's control socket."""
    SIGNAL = "signal"
    """SIGTERM through a stable process reference, after the socket did not answer."""


class InstanceStopResult(BaseModel):
    """``vibesys instances stop --json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    id: InstanceId
    outcome: StopOutcome
    route: StopRoute | None = None
    """How the server was reached; ``None`` when nothing was sent."""


class DetachedLaunchErrorCode(StrEnum):
    """Failure codes ``vibesys --detach`` owns; configuration codes pass through as is."""

    REGISTRY_UNAVAILABLE = "registry_unavailable"
    """The per-user runtime root is missing, foreign, or open to other users."""
    RUN_ALREADY_LIVE = "run_already_live"
    """``--resume`` named a run a live detached server on this node is driving."""
    SERVER_START_FAILED = "server_start_failed"
    """The detached server exited, or did not serve, before it was ready."""


class DetachedLaunchFailure(BaseModel):
    """What ``vibesys --detach`` prints on stdout, as one line, when it starts nothing.

    On success the line is a ``LiveInstanceRecord`` instead; a reader tells them
    apart by ``outcome``, which a record never has. ``code`` is a
    ``DetachedLaunchErrorCode`` or the code of the run's configuration
    diagnostic (``invalid_arguments``, ``resume_not_found``, ...), and
    ``exit_code`` is the process's exit status.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    outcome: Literal["failed"] = "failed"
    code: str
    stage: str
    message: str
    exit_code: PositiveInt
    log_path: str | None = None
    """The detached server's log, when one was started."""
    live_instance: LiveInstanceRecord | None = None
    """The server already driving the run, for ``run_already_live``: attach to it."""


# --- pure core -------------------------------------------------------------


@dataclass(frozen=True)
class InstanceObservation:
    """One look at an id: its lock first, then its record (``None``: absent or invalid)."""

    id: str
    lock: LockState
    record: LiveInstanceRecord | None


class Verdict(StrEnum):
    """What a lister may conclude from one observation."""

    LIVE = "live"
    """The owner holds the lock and has published a valid record."""
    PENDING = "pending"
    """The owner holds the lock but has no valid record yet (or any more)."""
    DEAD = "dead"
    """No process holds the lock: the files are leftovers to remove."""
    UNVERIFIED = "unverified"
    """The lock could not be read; report it, and neither trust nor remove it."""


def judge(observation: InstanceObservation) -> Verdict:
    """Decide one id from its observed lock state and record."""
    match observation.lock:
        case LockState.HELD:
            return Verdict.LIVE if observation.record is not None else Verdict.PENDING
        case LockState.FREE | LockState.ABSENT:
            return Verdict.DEAD
        case LockState.UNKNOWN:
            return Verdict.UNVERIFIED


@dataclass(frozen=True)
class Survey:
    """A listing decision: what to report and what to remove."""

    live: tuple[LiveInstanceRecord, ...]
    unverified: tuple[str, ...]
    dead: tuple[str, ...]


def survey(observations: Iterable[InstanceObservation]) -> Survey:
    """Partition observations, ordered by start time then id for a stable listing."""
    live: list[LiveInstanceRecord] = []
    unverified: list[str] = []
    dead: list[str] = []
    for observation in observations:
        verdict = judge(observation)
        if verdict is Verdict.LIVE and observation.record is not None:
            live.append(observation.record)
        elif verdict is Verdict.UNVERIFIED:
            unverified.append(observation.id)
        elif verdict is Verdict.DEAD:
            dead.append(observation.id)
    return Survey(
        live=tuple(sorted(live, key=lambda record: (record.started_at, record.id))),
        unverified=tuple(sorted(unverified)),
        dead=tuple(sorted(dead)),
    )


def driving(listing: InstanceList, run_id: str) -> LiveInstanceRecord | None:
    """Return the live server already driving ``run_id``, if any.

    Run ids are generated per experiment, so a match on this node is the same
    run. A server still starting has not published its run id; the run's own
    write lease is what refuses a second writer in that window.
    """
    return next((record for record in listing.instances if record.run_id == run_id), None)


# --- interface and implementations -----------------------------------------


class InstanceHold(Protocol):
    """A server's lifetime claim on one id; ``release`` on every exit path."""

    def publish(self, record: LiveInstanceRecord) -> None:
        """Replace this id's record atomically."""
        ...

    def release(self) -> None:
        """Remove the record, then the lock; idempotent."""
        ...


class InstanceStore(Protocol):
    """The registry's storage: lifetime locks and records keyed by id.

    Contract: ``hold`` publishes an id's lock only once it is held, and fails
    with ``FileExistsError`` if the id was ever published and not reaped; a
    held lock observes ``HELD`` until the holder releases it or dies; ``reap``
    removes an id's files only when no live holder exists and reports whether
    it did.
    """

    def ids(self) -> tuple[str, ...]:
        """Every id with any file present, ascending."""
        ...

    def observe(self, instance_id: str) -> InstanceObservation:
        """Read the lock state, then the record."""
        ...

    def reap(self, instance_id: str) -> bool:
        """Remove a dead id's files; False (and nothing removed) if a holder lives."""
        ...

    def hold(self, instance_id: str) -> InstanceHold:
        """Claim ``instance_id`` for the caller's lifetime."""
        ...


class StopRequester(Protocol):
    """Ask a live server, over its control socket, to stop its run.

    Contract: ``request_stop`` returns True only when the server acknowledged
    the stop; a socket that refuses, times out, closes early or answers
    anything else is False, never an exception.
    """

    def request_stop(self, socket_path: str) -> bool:
        """Send one stop request and report whether it was acknowledged."""
        ...


def instance_root(environ: Mapping[str, str], uid: int) -> Path:
    """Return this user's node-local runtime root, created private.

    ``$XDG_RUNTIME_DIR/vibesys`` when set (systemd makes it per user, local and
    private), else ``/tmp/vibesys-<uid>``. Never under ``$HOME``, which may be
    on NFS where ``flock`` and sockets are unreliable. Both stay short enough
    for a socket path below the Unix limit.

    Raises ``PermissionError`` if the directory exists but is a symlink, is not
    owned by ``uid``, or is open to group or other: in a shared ``/tmp`` it may
    have been planted by another user.
    """
    runtime = environ.get("XDG_RUNTIME_DIR", "")
    root = (
        Path(runtime) / "vibesys"
        if runtime and Path(runtime).is_absolute()
        else _FALLBACK_PARENT / f"vibesys-{uid}"
    )
    root.mkdir(mode=_PRIVATE_DIRECTORY, parents=False, exist_ok=True)
    status = root.lstat()
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != uid or status.st_mode & _GROUP_OR_OTHER:
        message = (
            f"{root} must be a directory owned by uid {uid} with mode 0700; "
            "refusing to use it for the live-instance registry"
        )
        raise PermissionError(message)
    return root


def instance_run_directory(root: Path, instance_id: str) -> Path:
    """Return the private directory for one instance's socket and server log."""
    return root / "runs" / _checked_id(instance_id)


def instance_socket_path(root: Path, instance_id: str) -> Path:
    """Return the control socket path one instance binds."""
    return instance_run_directory(root, instance_id) / "control.sock"


def new_instance_id() -> str:
    """Return a fresh random registry id."""
    return secrets.token_hex(6)


class FileInstanceHold:
    """A ``LifetimeLock`` plus the record file it vouches for."""

    def __init__(self, lock: LifetimeLock, record_path: Path) -> None:
        """Wrap a held lock; ``FileInstanceStore.hold`` creates these."""
        self._lock = lock
        self._record_path = record_path

    def publish(self, record: LiveInstanceRecord) -> None:
        """Write the record through a private temporary file and rename it."""
        temporary = self._record_path.with_name(
            f".{self._record_path.name}.{secrets.token_hex(6)}.tmp"
        )
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(record.model_dump_json() + "\n")
            temporary.replace(self._record_path)
        finally:
            temporary.unlink(missing_ok=True)

    def release(self) -> None:
        """Remove the record first, so no observer sees it without its lock."""
        try:
            self._record_path.unlink(missing_ok=True)
        finally:
            self._lock.release()


class FileInstanceStore:
    """The registry on a node-local filesystem under ``root``."""

    def __init__(self, root: Path) -> None:
        """Use ``root/instances`` (created private) for locks and records."""
        self._root = root
        self._directory = root / "instances"
        self._directory.mkdir(mode=_PRIVATE_DIRECTORY, exist_ok=True)

    def ids(self) -> tuple[str, ...]:
        """List ids that have a lock or a record file."""
        found = {
            entry.name.removesuffix(entry.suffix)
            for entry in self._directory.iterdir()
            if entry.suffix in {_RECORD_SUFFIX, _LOCK_SUFFIX}
            and _INSTANCE_ID.match(entry.name.removesuffix(entry.suffix))
        }
        return tuple(sorted(found))

    def observe(self, instance_id: str) -> InstanceObservation:
        """Probe the lock, then read and validate the record."""
        lock = probe_lock(self._lock_path(instance_id))
        return InstanceObservation(instance_id, lock, self._read(instance_id))

    def reap(self, instance_id: str) -> bool:
        """Remove record, socket and lock under the lock, if nobody holds it."""
        return LifetimeLock.reap(
            self._lock_path(instance_id),
            (
                self._record_path(instance_id),
                instance_socket_path(self._root, instance_id),
            ),
        )

    def hold(self, instance_id: str) -> FileInstanceHold:
        """Publish a held lock for ``instance_id``."""
        lock = LifetimeLock.publish(self._lock_path(instance_id))
        return FileInstanceHold(lock, self._record_path(instance_id))

    def _read(self, instance_id: str) -> LiveInstanceRecord | None:
        try:
            raw = self._record_path(instance_id).read_text(encoding="utf-8")
            record = LiveInstanceRecord.model_validate_json(raw)
        except (OSError, ValidationError):
            return None
        return record if record.id == instance_id else None

    def _lock_path(self, instance_id: str) -> Path:
        return self._directory / f"{_checked_id(instance_id)}{_LOCK_SUFFIX}"

    def _record_path(self, instance_id: str) -> Path:
        return self._directory / f"{_checked_id(instance_id)}{_RECORD_SUFFIX}"


@dataclass
class _FakeEntry:
    lock: LockState
    record: LiveInstanceRecord | None = None


class FakeInstanceHold:
    """In-memory hold; ``crash`` drops the lock and leaves the files, like ``kill -9``."""

    def __init__(self, store: FakeInstanceStore, instance_id: str) -> None:
        """Bind to one held entry of ``store``."""
        self._store = store
        self._id = instance_id
        self._released = False

    def publish(self, record: LiveInstanceRecord) -> None:
        """Replace the record, as the holder only."""
        if self._released:
            message = f"instance {self._id} was released"
            raise RuntimeError(message)
        self._store.entries[self._id].record = record

    def release(self) -> None:
        """Remove record and lock; idempotent."""
        if self._released:
            return
        self._released = True
        self._store.entries.pop(self._id, None)

    def crash(self) -> None:
        """Die without cleanup: the kernel frees the lock, the files remain."""
        if self._released:
            return
        self._released = True
        self._store.entries[self._id].lock = LockState.FREE


class FakeInstanceStore:
    """In-memory ``InstanceStore`` with the same contract as the filesystem one."""

    def __init__(self) -> None:
        """Start empty; ``entries`` may be edited to plant unreadable locks."""
        self.entries: dict[str, _FakeEntry] = {}
        self._published: set[str] = set()

    def ids(self) -> tuple[str, ...]:
        """List ids with any state."""
        return tuple(sorted(self.entries))

    def observe(self, instance_id: str) -> InstanceObservation:
        """Return the entry's lock and record."""
        entry = self.entries.get(_checked_id(instance_id))
        if entry is None:
            return InstanceObservation(instance_id, LockState.ABSENT, None)
        return InstanceObservation(instance_id, entry.lock, entry.record)

    def reap(self, instance_id: str) -> bool:
        """Remove an entry unless its lock is held or unreadable."""
        entry = self.entries.get(_checked_id(instance_id))
        if entry is None:
            return True
        if entry.lock in {LockState.HELD, LockState.UNKNOWN}:
            return False
        del self.entries[instance_id]
        return True

    def hold(self, instance_id: str) -> FakeInstanceHold:
        """Claim a never-published id."""
        if _checked_id(instance_id) in self._published:
            raise FileExistsError(instance_id)
        self._published.add(instance_id)
        self.entries[instance_id] = _FakeEntry(LockState.HELD)
        return FakeInstanceHold(self, instance_id)


class ControlSocketStopRequester:
    """Send ``command.stop`` over the server's JSONL control socket.

    This is the same command an attached client sends, so the run stops at its
    next controlled boundary with its state persisted, exactly as a stop from
    the TUI does. Only the socket's owner can dial it: the socket is ``0600``
    inside the ``0700`` registry root.
    """

    def __init__(self, network: Network, *, timeout: float = CONTROL_REPLY_TIMEOUT_SECONDS) -> None:
        """Dial through ``network``; wait at most ``timeout`` to connect and for the reply."""
        self._network = network
        self._timeout = timeout

    def request_stop(self, socket_path: str) -> bool:
        """Send the stop and read one response line."""
        try:
            connection = self._network.connect(socket_path, self._timeout)
        except OSError:
            return False
        try:
            connection.send(StopCommand().model_dump_json().encode() + b"\n")
            line = _read_line(connection, self._timeout)
        except OSError:
            return False
        finally:
            connection.close()
        try:
            response = Response.model_validate_json(line)
        except ValidationError:
            return False
        return response.ok and response.ack is not None and response.ack.action == "stop"


def _read_line(connection: Connection, timeout: float) -> bytes:
    """Read through the first newline, or to EOF; ``TimeoutError`` if the peer stalls."""
    buffer = b""
    while b"\n" not in buffer:
        if len(buffer) > _MAX_REPLY_BYTES:
            break
        chunk = connection.recv(4096, timeout)
        if not chunk:
            break
        buffer += chunk
    return buffer.split(b"\n", 1)[0]


# --- shell ------------------------------------------------------------------


@dataclass(frozen=True)
class StopEffects:
    """The I/O one ``LiveRegistry.stop`` uses: ask, signal, and wait."""

    requester: StopRequester
    signaller: ProcessSignaller
    clock: Clock
    pause: Callable[[float], None]


class LiveRegistry:
    """List, find, register and stop live servers over one ``InstanceStore``."""

    def __init__(self, store: InstanceStore) -> None:
        """Compose the pure decisions with ``store``."""
        self._store = store

    def list(self) -> InstanceList:
        """Report live servers and remove the files of dead ones."""
        decision = survey(self._store.observe(instance_id) for instance_id in self._store.ids())
        for instance_id in decision.dead:
            self._store.reap(instance_id)
        return InstanceList(instances=decision.live, unverified=decision.unverified)

    def find(self, instance_id: str) -> LiveInstanceRecord | None:
        """Return the record of a live server, else ``None``."""
        observation = self._store.observe(instance_id)
        return observation.record if judge(observation) is Verdict.LIVE else None

    @contextmanager
    def register(self, instance_id: str) -> Iterator[InstanceHold]:
        """Hold ``instance_id`` for the block; release on every exit path."""
        hold = self._store.hold(instance_id)
        try:
            yield hold
        finally:
            hold.release()

    def stop(
        self, instance_id: str, effects: StopEffects, *, force: bool = False
    ) -> InstanceStopResult:
        """Stop a live server and wait, at most ``STOP_TIMEOUT_SECONDS``, for its lock to drop.

        The server is first asked over its control socket, which stops the run
        at its next safe boundary on every platform. Only a server that does
        not acknowledge (wedged, or still binding), or a ``force`` stop, is
        sent SIGTERM; the signal goes only while the lock is still held,
        through a stable process reference, so a reused pid is never hit.
        """
        record = self.find(instance_id)
        if record is None:
            self._store.reap(instance_id)
            return InstanceStopResult(id=instance_id, outcome=StopOutcome.NOT_RUNNING)
        if not force and effects.requester.request_stop(record.socket_path):
            released = self._await_release(instance_id, effects)
            return InstanceStopResult(
                id=instance_id,
                outcome=StopOutcome.STOPPED if released else StopOutcome.STOPPING,
                route=StopRoute.CONTROL_SOCKET,
            )
        try:
            sent = effects.signaller.terminate_if_current(
                record.pid, lambda: self._held(instance_id)
            )
        except ProcessLookupError:
            sent = False
        except NotImplementedError:
            return InstanceStopResult(id=instance_id, outcome=StopOutcome.UNSUPPORTED)
        if not sent:
            self._store.reap(instance_id)
            return InstanceStopResult(id=instance_id, outcome=StopOutcome.NOT_RUNNING)
        released = self._await_release(instance_id, effects)
        return InstanceStopResult(
            id=instance_id,
            outcome=StopOutcome.STOPPED if released else StopOutcome.STILL_RUNNING,
            route=StopRoute.SIGNAL,
        )

    def _await_release(self, instance_id: str, effects: StopEffects) -> bool:
        """Wait, bounded, for the holder to drop its lock; reap its files if it did."""
        deadline = effects.clock.now() + STOP_TIMEOUT_SECONDS
        while self._held(instance_id):
            if effects.clock.now() >= deadline:
                return False
            effects.pause(_STOP_POLL_SECONDS)
        self._store.reap(instance_id)
        return True

    def _held(self, instance_id: str) -> bool:
        return self._store.observe(instance_id).lock is LockState.HELD


def host_facts() -> tuple[str, str]:
    """Return this node's hostname and the installed VibeSys version."""
    try:
        version = distribution_version("vibesys")
    except PackageNotFoundError:
        version = "0+unknown"
    return platform.node(), version


def checkout_root(module_file: Path) -> Path | None:
    """Return the VibeSys source checkout that holds ``module_file``, if any.

    ``module_file`` is a module of the ``src/<package>/`` layout; its checkout
    is two directories above the package, and counts only when that
    directory's ``pyproject.toml`` declares the ``vibesys`` project. An
    installed distribution has no such file, so it yields ``None``.
    """
    resolved = module_file.resolve()
    if len(resolved.parents) < _CHECKOUT_DEPTH + 1:
        return None
    candidate = resolved.parents[_CHECKOUT_DEPTH]
    try:
        manifest = tomllib.loads((candidate / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    project = manifest.get("project")
    if isinstance(project, dict) and project.get("name") == "vibesys":
        return candidate
    return None


def running_checkout() -> str | None:
    """Return the checkout this process runs VibeSys from, as a string, or ``None``."""
    root = checkout_root(Path(__file__))
    return None if root is None else str(root)


def parse_instance_id(value: str) -> str:
    """Return ``value`` if it is a registry id; ``ValueError`` naming the pattern otherwise."""
    return _checked_id(value)


def _checked_id(instance_id: str) -> str:
    if not _INSTANCE_ID.match(instance_id):
        message = f"instance id {instance_id!r} must match {INSTANCE_ID_PATTERN}"
        raise ValueError(message)
    return instance_id
