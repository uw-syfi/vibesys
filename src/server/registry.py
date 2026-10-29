"""Per-user, node-local registry of live detached VibeSys servers.

A detached server outlives the shell that started it, so a client that later
reaches the host (over SSH, from the desktop app) needs to find it. This module
is that directory, and its one guarantee is that liveness is *proven*, never
read from file contents: each server holds an exclusive ``flock`` on its own
lock file for its whole lifetime (``LifetimeLock``), the kernel drops it on any
exit (``kill -9`` included), and a lister that observes the lock free knows
the owner is gone and removes its files. There are no pid checks (pids are
reused), no heartbeats, and no cleanup daemon.

The registry answers "what is running on this node now, and what ended here
in the last day". A server that exits leaves its last record, with the phase
its run ended in, as a tombstone; a server that died without writing one is
reported as crashed. A writing listing (``LiveRegistry.list``) buries the
crashed and removes tombstones older than ``TOMBSTONE_RETENTION_SECONDS``; a
watch reads with ``LiveRegistry.read``, which writes nothing. Older runs are
the per-project run history's business and are reopened read-only from it.

Layout under ``instance_root``::

    instances/<id>.lock   lifetime lock, published only once held
    instances/<id>.json   LiveInstanceRecord, written by the holder; its tombstone after
    readers/<id>.lock, readers/<id>.json   the same for a read-only server (``READERS_DIRECTORY``)
    runs/<id>/control.sock, runs/<id>/server.log, runs/<id>/inspection.json

The module is light: it imports the standard library, Pydantic, the record
contracts in ``server.host_api``, and ``vs_sim.api`` types, never the run
protocol or engine, so ``vibesys instances list`` starts fast. Asking a server
to stop over its control socket needs the run protocol; that requester lives
in ``server.instances`` and reaches ``LiveRegistry.stop`` through the
``StopRequester`` interface.

Public surface: the lock primitives (``LockState``, ``probe_lock``,
``LifetimeLock``, ``try_lock``, ``unlock``, ``FLOCK_AVAILABLE``), which the web
gateway's claim in ``server.transport.discovery`` also uses; ``instance_root``
and the per-instance paths; ``INSTANCES_DIRECTORY`` and ``READERS_DIRECTORY``; ``InstanceStore`` with its ``FileInstanceStore``
and ``FakeInstanceStore`` implementations; ID-scoped log and immutable
inspection-metadata stores; ``InstanceInspector``; the ``StopRequester``
interface and the ``StopEffects`` a stop uses; the pure ``judge`` and
``survey`` decisions; and ``LiveRegistry``, which composes them.
"""

from __future__ import annotations

import os
import platform
import re
import stat
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from server.host_api import (
    INSPECTION_LOG_TAIL_BYTES,
    RECORD_VERSION,
    AvailableLogTail,
    EndRecord,
    FailureReason,
    InspectionState,
    InstanceInspection,
    InstanceInspectionMetadata,
    InstanceList,
    InstanceLogTail,
    InstanceStopResult,
    LiveInstanceRecord,
    MissingLogTail,
    RunEndOutcome,
    RunPhase,
    RunStatus,
    StopOutcome,
    StopRoute,
    UnreadableRecord,
    parse_record,
)
from server.host_envelope import INSTANCE_ID_PATTERN
from vs_decode.api import Unreadable, ValidationOptions, decode_json
from vs_project.api import ensure_private_directory, user_runtime_root
from vs_sim.api import sync_descriptor

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from vs_sim.api import Clock, ProcessSignaller

try:
    import fcntl
except ImportError:  # pragma: no cover - VibeSys currently targets Unix hosts.
    fcntl = None  # type: ignore[assignment]

FLOCK_AVAILABLE = fcntl is not None
"""Whether this platform has BSD ``flock``; every lock here needs it."""

_INSTANCE_ID = re.compile(INSTANCE_ID_PATTERN)

INSTANCES_DIRECTORY: Final = "instances"
"""Where live runs register under the registry root."""
READERS_DIRECTORY: Final = "readers"
"""Where read-only servers register under the registry root, apart from the runs."""
_RECORD_SUFFIX = ".json"
_LOCK_SUFFIX = ".lock"
_STOP_POLL_SECONDS = 0.05
STOP_TIMEOUT_SECONDS = 10.0
"""How long ``stop`` waits for a stopped server to finish its run teardown."""
EXIT_GRACE_SECONDS = 10.0
"""How long a holder may keep its lock after writing its tombstone and still list as ended.

A server writes its tombstone as its last act and releases its lock at once,
so a holder past this is wedged in its exit: it lists as live again, so an operator sees a process still holding the instance and can stop it.
"""
TOMBSTONE_RETENTION_SECONDS = 24 * 60 * 60
"""How long an ended server's record stays listed; the next listing after removes it."""
CRASHED = FailureReason(
    code="server_crashed",
    stage="server",
    message="The server exited without recording how its run ended; see its log.",
)
"""The reason a listing records for a server that died without a tombstone."""
CRASHED_END = EndRecord(outcome=RunEndOutcome.FAILED, reason=CRASHED.message, resumable=True)
"""A crashed server's ``end``: its durable run state is where the server left it, so a
resume is offered; the round it continues from is unknown, since the server said nothing."""
_INSPECTION_METADATA_BYTES = 32 * 1024
_INSPECTION_METADATA_NAME = "inspection.json"


# --- lock primitives -------------------------------------------------------


class LockState(StrEnum):
    """What one probe observed about a lifetime lock file."""

    HELD = "held"
    """A live process holds the exclusive lock."""
    FREE = "free"
    """The file exists and nothing holds it: every holder has exited."""
    ABSENT = "absent"
    """No file exists at the path."""
    UNKNOWN = "unknown"
    """The file could not be opened or the filesystem refused the lock."""


def probe_lock(path: Path) -> LockState:
    """Observe ``path``'s exclusive lock without becoming a holder.

    The probe takes a *shared* lock. That is enough to detect an exclusive one,
    because the two conflict, and it does not exclude another reader, so asking
    the question does not make the asker a holder and two callers asking at
    once cannot report each other. It also needs only read access, which an
    exclusive POSIX record lock (what an NFS client without ``local_lock`` uses
    to emulate ``flock``) would refuse on a read-only descriptor.

    The residual is that an owner's exclusive lock can still lose to a probe
    that holds the shared lock at that instant. The probe holds it for the
    duration of two syscalls, so the window is microseconds per observation.
    """
    if fcntl is None:  # pragma: no cover - defensive for non-Unix packaging.
        return LockState.UNKNOWN
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return LockState.ABSENT
    except OSError:
        return LockState.UNKNOWN
    try:
        return LockState.FREE if try_lock(descriptor, shared=True) else LockState.HELD
    except OSError:
        return LockState.UNKNOWN
    finally:
        os.close(descriptor)


class LifetimeLock:
    """An exclusive ``flock`` that its owner holds for its whole lifetime.

    ``publish`` takes the lock on a fresh private file and only then renames it
    into place, so no observer ever sees the published path unlocked while its
    owner lives. The kernel drops the lock on every exit, ``kill -9`` included,
    so a ``probe_lock`` answer of ``FREE`` proves the owner is gone and, because
    a published path is never locked again, stays true. ``reap`` acts on that.

    The guarantee needs a BSD ``flock`` on a node-local filesystem; see
    ``WebInstanceHold`` for why an NFS mount without ``local_lock`` breaks it.
    """

    def __init__(self, path: Path, descriptor: int) -> None:
        """Wrap an already-held lock; use ``publish`` to create one."""
        self.path = path
        self._descriptor: int | None = descriptor

    @classmethod
    def publish(cls, path: Path) -> LifetimeLock:
        """Create, lock, and then publish ``path``; fail if it already exists."""
        temporary = path.with_name(f".{path.name}.{os.urandom(6).hex()}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            _lock(descriptor)
            # `link` fails rather than replacing a published lock, so two
            # owners can never both believe they hold one path.
            os.link(temporary, path)
        except BaseException:
            os.close(descriptor)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return cls(path, descriptor)

    def release(self) -> None:
        """Unpublish ``path`` and drop the lock; idempotent."""
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            self.path.unlink(missing_ok=True)
        finally:
            unlock(descriptor)
            os.close(descriptor)

    @staticmethod
    def reap(path: Path, companions: tuple[Path, ...]) -> bool:
        """Remove ``companions`` and then ``path`` iff no live owner holds ``path``.

        Removal happens while this caller holds the lock, so it cannot race the
        owner (who would still hold it) or another reaper (who waits on the same
        file and then finds the companions gone). A missing ``path`` has no
        owner, so its companions are removed too. Returns whether the owner was
        proven gone.
        """
        try:
            descriptor = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            for companion in companions:
                companion.unlink(missing_ok=True)
            return True
        try:
            if not try_lock(descriptor):
                return False
            for companion in companions:
                companion.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            return True
        finally:
            os.close(descriptor)


def _lock(descriptor: int, *, shared: bool = False) -> None:
    """Take a non-blocking ``flock``; ``BlockingIOError`` when another holds it."""
    if fcntl is None:  # pragma: no cover - defensive for non-Unix packaging.
        return
    fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)


def try_lock(descriptor: int, *, shared: bool = False) -> bool:
    """Take a non-blocking ``flock`` on ``descriptor``; False when another holds it."""
    try:
        _lock(descriptor, shared=shared)
    except BlockingIOError:
        return False
    return True


def unlock(descriptor: int) -> None:
    """Drop this descriptor's ``flock``."""
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


# --- pure core -------------------------------------------------------------


@dataclass(frozen=True)
class InstanceObservation:
    """One look at an id: its lock first, then its record (``None``: absent)."""

    id: str
    lock: LockState
    record: LiveInstanceRecord | UnreadableRecord | None


class Verdict(StrEnum):
    """What a lister may conclude from one observation."""

    LIVE = "live"
    """The owner holds the lock and has published a valid record that is not its tombstone."""
    UNREADABLE = "unreadable"
    """A record this reader cannot interpret, of a live owner or a newer checkout."""
    PENDING = "pending"
    """The owner holds the lock but has no record yet."""
    EXITING = "exiting"
    """The owner holds the lock and has written its tombstone: it is ending, or wedged in its exit."""
    ENDED = "ended"
    """The owner left a tombstone younger than a day and no longer holds the lock."""
    CRASHED = "crashed"
    """No process holds the lock, and the owner died without leaving a tombstone."""
    DEAD = "dead"
    """No process holds the lock, and nothing left is worth reporting: remove it."""
    UNVERIFIED = "unverified"
    """The lock could not be read; report it, and neither trust nor remove it."""


def judge(observation: InstanceObservation, now: float) -> Verdict:
    """Decide one id from its observed lock state and record, at time ``now``.

    A held lock with a tombstone is exiting, not live. The owner writes its
    tombstone as its last act before it releases the lock, and an observation
    reads the lock before the record, so an owner that ends between the two
    reads is seen holding its lock with its tombstone published. The tombstone
    is the later fact, but the process is still there: ``stop`` acts on an
    exiting owner, and a listing reports it ended only within
    ``EXIT_GRACE_SECONDS`` of its tombstone (``exited_in_grace``). Nothing of
    a holder may be removed; once the lock drops it ages like any tombstone.
    """
    record = observation.record
    match observation.lock:
        case LockState.HELD:
            match record:
                case LiveInstanceRecord(ended_at=None):
                    return Verdict.LIVE
                case LiveInstanceRecord():
                    return Verdict.EXITING
                case UnreadableRecord():
                    return Verdict.UNREADABLE
                case None:
                    return Verdict.PENDING
        case LockState.UNKNOWN:
            return Verdict.UNVERIFIED
        case LockState.FREE | LockState.ABSENT:
            return _judge_left_behind(record, now)


def exited_in_grace(record: LiveInstanceRecord, now: float) -> bool:
    """Whether an exiting holder's tombstone is recent enough to report it ended."""
    return record.ended_at is not None and now - record.ended_at < EXIT_GRACE_SECONDS


def listed_live(observation: InstanceObservation, now: float) -> LiveInstanceRecord | None:
    """The record of a server every reader reports as running, else ``None``.

    That is a live holder, or one wedged in its exit: still holding its lock
    ``EXIT_GRACE_SECONDS`` after its tombstone. ``survey`` and
    ``InstanceInspector`` decide through here, so a listing and an inspection
    agree on whether a server is running. ``LiveRegistry.find`` answers a
    narrower question, whether a client can still reach the server.
    """
    record = observation.record
    if not isinstance(record, LiveInstanceRecord):
        return None
    match judge(observation, now):
        case Verdict.LIVE:
            return record
        case Verdict.EXITING if not exited_in_grace(record, now):
            return record
        case _:
            return None


def _judge_left_behind(record: LiveInstanceRecord | UnreadableRecord | None, now: float) -> Verdict:
    """Decide what an owner that no longer holds its lock left behind."""
    match record:
        case LiveInstanceRecord(ended_at=None):
            return Verdict.CRASHED
        case LiveInstanceRecord(ended_at=float() as ended_at):
            fresh = now - ended_at < TOMBSTONE_RETENTION_SECONDS
            return Verdict.ENDED if fresh else Verdict.DEAD
        case UnreadableRecord(version=int() as version) if version > RECORD_VERSION:
            # A newer checkout's tombstone; that checkout's lister ages it.
            return Verdict.UNREADABLE
        case _:
            return Verdict.DEAD


def crashed(record: LiveInstanceRecord, ended_at: float | None = None) -> LiveInstanceRecord:
    """The record of a server that died without writing its own tombstone.

    ``ended_at`` is when a writing listing found it dead, and the tombstone it
    writes carries it. A read-only listing reports the crash without one, so
    reading the same registry twice reports the same record.
    """
    return LiveInstanceRecord.model_validate(
        {
            **record.model_dump(),
            "phase": RunPhase(status=RunStatus.FAILED, reason=CRASHED),
            "ended_at": ended_at,
            "paused": None,
            "end": CRASHED_END,
        }
    )


@dataclass(frozen=True)
class Survey:
    """A listing decision: what to report, what tombstones to write, and what to remove."""

    live: tuple[LiveInstanceRecord, ...]
    ended: tuple[LiveInstanceRecord, ...]
    """Tombstones younger than a day."""
    crashed: tuple[LiveInstanceRecord, ...]
    """Servers that died without a tombstone, as :func:`crashed` reports them before burial."""
    unreadable: tuple[UnreadableRecord, ...]
    unverified: tuple[str, ...]
    dead: tuple[str, ...]


def survey(observations: Iterable[InstanceObservation], now: float) -> Survey:
    """Partition observations, ordered by start time then id for a stable listing."""
    live: list[LiveInstanceRecord] = []
    ended: list[LiveInstanceRecord] = []
    dead_without_tombstone: list[LiveInstanceRecord] = []
    unreadable: list[UnreadableRecord] = []
    unverified: list[str] = []
    dead: list[str] = []
    for observation in observations:
        record = observation.record
        match judge(observation, now), record:
            case Verdict.LIVE, LiveInstanceRecord():
                live.append(record)
            case Verdict.EXITING, LiveInstanceRecord():
                (live if listed_live(observation, now) else ended).append(record)
            case Verdict.ENDED, LiveInstanceRecord():
                ended.append(record)
            case Verdict.CRASHED, LiveInstanceRecord():
                dead_without_tombstone.append(crashed(record))
            case Verdict.UNREADABLE, UnreadableRecord():
                unreadable.append(record)
            case Verdict.UNVERIFIED, _:
                unverified.append(observation.id)
            case Verdict.DEAD, _:
                dead.append(observation.id)
            case _:
                pass
    return Survey(
        live=_by_start(live),
        ended=_by_start(ended),
        crashed=_by_start(dead_without_tombstone),
        unreadable=tuple(sorted(unreadable, key=lambda record: record.id)),
        unverified=tuple(sorted(unverified)),
        dead=tuple(sorted(dead)),
    )


def _by_start(records: Iterable[LiveInstanceRecord]) -> tuple[LiveInstanceRecord, ...]:
    return tuple(sorted(records, key=lambda record: (record.started_at, record.id)))


def driving(listing: InstanceList, run_id: str) -> LiveInstanceRecord | None:
    """Return the live server already driving ``run_id``, if any.

    Run ids are generated per experiment, so a match on this node is the same
    run. Every record names its run from the moment it is published.
    """
    return next((record for record in listing.instances if record.run_id == run_id), None)


# --- interface and implementations -----------------------------------------


class InstanceHold(Protocol):
    """A server's lifetime claim on one id; ``release`` on every exit path."""

    def publish(self, record: LiveInstanceRecord) -> None:
        """Replace this id's record atomically."""
        ...

    def release(self) -> None:
        """Drop the lock, leaving the last record as the tombstone; idempotent."""
        ...


class InstanceStore(Protocol):
    """The registry's storage: lifetime locks and records keyed by id.

    Contract: ``hold`` publishes an id's lock only once it is held, and fails
    with ``FileExistsError`` while that lock is published; a held lock observes
    ``HELD`` until the holder releases it (then ``ABSENT``, its record kept) or
    dies (then ``FREE``); ``reap`` removes an id's files and ``bury`` replaces
    its record, each only when no live holder exists, reporting whether it did.
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

    def bury(self, record: LiveInstanceRecord) -> bool:
        """Write a dead holder's tombstone; False (and nothing written) if a holder lives."""
        ...

    def hold(self, instance_id: str) -> InstanceHold:
        """Claim ``instance_id`` for the caller's lifetime."""
        ...


@dataclass(frozen=True)
class InstancePaths:
    """The two operator-visible paths derived from one validated registry id."""

    control_socket: Path
    server_log: Path


class InstanceLogs(Protocol):
    """Resolve and read one instance's diagnostics without accepting a filesystem path."""

    def paths(self, instance_id: str) -> InstancePaths:
        """Return the instance-owned paths derived from ``instance_id``."""
        ...

    def tail(self, instance_id: str) -> InstanceLogTail:
        """Return a bounded log tail, or ``missing``; reject unsafe file types."""
        ...


class InstanceMetadataStore(Protocol):
    """Immutable inspection snapshots keyed only by validated registry ids."""

    def load(self, instance_id: str) -> InstanceInspectionMetadata | None:
        """Load one retained snapshot, or ``None`` when it was never published."""
        ...

    def publish(self, metadata: InstanceInspectionMetadata) -> None:
        """Publish one snapshot atomically; an unlike existing value is a conflict."""
        ...


class StopIntents(Protocol):
    """Durably records the operator's stop of a run, before its server is signalled.

    Contract: never raises. A server is signalled whether or not the record could be
    written; a run whose project could not take it resumes as after a crash.
    """

    def record(self, record: LiveInstanceRecord) -> None:
        """Note that ``record``'s run was stopped by the operator."""
        ...


@dataclass
class FakeStopIntents:
    """A ``StopIntents`` that keeps the run ids it was told, in order."""

    recorded: list[str] = field(default_factory=list)

    def record(self, record: LiveInstanceRecord) -> None:
        """Keep ``record``'s run id."""
        self.recorded.append(record.run_id)


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

    ``vs_project.api.user_runtime_root`` says where it is and
    ``ensure_private_directory`` creates it and refuses one another user planted.

    Raises ``PermissionError`` if the directory exists but is a symlink, is not
    owned by ``uid``, or is writable by group or other.
    """
    return ensure_private_directory(user_runtime_root(environ, uid), uid)


def instance_run_directory(root: Path, instance_id: str) -> Path:
    """Return the private directory for one instance's socket and server log."""
    return root / "runs" / _checked_id(instance_id)


def instance_socket_path(root: Path, instance_id: str) -> Path:
    """Return the control socket path one instance binds."""
    return instance_run_directory(root, instance_id) / "control.sock"


def instance_log_path(root: Path, instance_id: str) -> Path:
    """Return the file one instance's stdout and stderr go to."""
    return instance_run_directory(root, instance_id) / "server.log"


def new_instance_id() -> str:
    """Return a fresh random registry id."""
    return os.urandom(6).hex()


class InstanceLogError(RuntimeError):
    """An instance log exists but cannot be read safely."""

    @classmethod
    def unreadable(cls, path: Path, detail: OSError) -> InstanceLogError:
        """Build an I/O failure without exposing log contents."""
        return cls(f"cannot safely inspect {path}: {detail}")

    @classmethod
    def unsafe(cls, path: Path, reason: str = "non-regular") -> InstanceLogError:
        """Build a refusal for a path that is not the regular file expected."""
        return cls(f"refusing to inspect {reason} log file {path}")


class InstanceMetadataError(RuntimeError):
    """A retained inspection snapshot is unsafe, malformed, or conflicting."""

    @classmethod
    def unreadable(cls, path: Path, detail: Exception) -> InstanceMetadataError:
        """Build an I/O or validation failure naming only the snapshot path."""
        return cls(f"cannot safely read inspection metadata {path}: {detail}")

    @classmethod
    def unsafe(cls, path: Path, reason: str) -> InstanceMetadataError:
        """Build a refusal for an unsafe snapshot or parent component."""
        return cls(f"refusing unsafe inspection metadata {path}: {reason}")


class _InstanceDirectoryError(OSError):
    """Descriptor-relative traversal could not safely reach an instance directory."""


@contextmanager
def _instance_directory(root: Path, instance_id: str) -> Iterator[int]:
    """Open ``root/runs/<id>`` from ``/`` without following any directory symlink."""
    instance_id = _checked_id(instance_id)
    if not root.is_absolute() or any(part in {".", ".."} for part in root.parts[1:]):
        message = f"registry root must be an absolute normalized path: {root}"
        raise _InstanceDirectoryError(message)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        try:
            parent = os.open(os.sep, flags)
        except OSError as exc:  # pragma: no cover - a usable POSIX host always opens its root.
            message = f"cannot safely open filesystem root: {exc}"
            raise _InstanceDirectoryError(message) from exc
        descriptors.append(parent)
        for component in (*root.parts[1:], "runs", instance_id):
            try:
                descriptor = os.open(component, flags, dir_fd=parent)
            except OSError as exc:
                message = f"cannot safely open directory component {component!r}: {exc}"
                raise _InstanceDirectoryError(message) from exc
            descriptors.append(descriptor)
            parent = descriptor
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class FileInstanceLogs:
    """Bounded, no-follow access to ``server.log`` files under one registry root."""

    def __init__(self, root: Path, *, limit: int = INSPECTION_LOG_TAIL_BYTES) -> None:
        """Read logs under ``root``, returning no more than ``limit`` UTF-8 bytes."""
        if not 0 < limit <= INSPECTION_LOG_TAIL_BYTES:
            message = (
                "the instance log tail limit must be between 1 and "
                f"{INSPECTION_LOG_TAIL_BYTES} bytes"
            )
            raise ValueError(message)
        self._root = root
        self._limit = limit

    def paths(self, instance_id: str) -> InstancePaths:
        """Derive the two fixed paths from a validated id."""
        directory = instance_run_directory(self._root, instance_id)
        return InstancePaths(directory / "control.sock", directory / "server.log")

    def tail(self, instance_id: str) -> InstanceLogTail:
        """Read a bounded suffix through no-follow directory descriptors."""
        path = self.paths(instance_id).server_log
        try:
            with _instance_directory(self._root, instance_id) as directory:
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0)
                )
                try:
                    descriptor = os.open("server.log", flags, dir_fd=directory)
                except FileNotFoundError:
                    return MissingLogTail()
                except OSError as exc:
                    raise InstanceLogError.unreadable(path, exc) from exc
                try:
                    opened = os.fstat(descriptor)
                    if not stat.S_ISREG(opened.st_mode):
                        raise InstanceLogError.unsafe(path)
                    extra = 3
                    start = max(0, opened.st_size - self._limit - extra)
                    os.lseek(descriptor, start, os.SEEK_SET)
                    raw = _read_at_most(descriptor, self._limit + extra)
                finally:
                    os.close(descriptor)
        except _InstanceDirectoryError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return MissingLogTail()
            raise InstanceLogError.unreadable(path, exc) from exc
        text = _bounded_utf8_tail(raw, self._limit)
        return AvailableLogTail(
            text=text,
            truncated=opened.st_size > self._limit
            or len(raw.decode("utf-8", errors="replace").encode("utf-8")) > self._limit,
        )


class FakeInstanceLogs:
    """In-memory ``InstanceLogs`` with byte-for-byte tail semantics."""

    def __init__(self, *, limit: int = INSPECTION_LOG_TAIL_BYTES) -> None:
        """Keep in-memory log bytes and apply ``limit`` like the file implementation."""
        if not 0 < limit <= INSPECTION_LOG_TAIL_BYTES:
            message = (
                "the instance log tail limit must be between 1 and "
                f"{INSPECTION_LOG_TAIL_BYTES} bytes"
            )
            raise ValueError(message)
        self.limit = limit
        self.contents: dict[str, bytes] = {}

    def paths(self, instance_id: str) -> InstancePaths:
        """Derive stable fake paths from a validated id."""
        directory = Path("/run/vibesys/runs") / _checked_id(instance_id)
        return InstancePaths(directory / "control.sock", directory / "server.log")

    def tail(self, instance_id: str) -> InstanceLogTail:
        """Return the same bounded UTF-8 projection as ``FileInstanceLogs``."""
        raw = self.contents.get(_checked_id(instance_id))
        if raw is None:
            return MissingLogTail()
        return AvailableLogTail(
            text=_bounded_utf8_tail(raw, self.limit),
            truncated=len(raw) > self.limit
            or len(raw.decode("utf-8", errors="replace").encode("utf-8")) > self.limit,
        )


class FileInstanceMetadataStore:
    """Atomic immutable inspection snapshots inside each retained instance directory."""

    def __init__(self, root: Path) -> None:
        """Keep snapshots under the validated node-local registry ``root``."""
        self._root = root

    def load(self, instance_id: str) -> InstanceInspectionMetadata | None:
        """Load a bounded regular snapshot through no-follow directory descriptors."""
        instance_id = _checked_id(instance_id)
        path = instance_run_directory(self._root, instance_id) / _INSPECTION_METADATA_NAME
        try:
            with _instance_directory(self._root, instance_id) as directory:
                return self._load_from(directory, path)
        except _InstanceDirectoryError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return None
            raise InstanceMetadataError.unreadable(path, exc) from exc

    def publish(self, metadata: InstanceInspectionMetadata) -> None:
        """Create one crash-atomic snapshot without ever replacing an existing value."""
        path = instance_run_directory(self._root, metadata.id) / _INSPECTION_METADATA_NAME
        contents = metadata.model_dump_json().encode("utf-8") + b"\n"
        if len(contents) > _INSPECTION_METADATA_BYTES:
            raise InstanceMetadataError.unsafe(path, "serialized snapshot exceeds its byte bound")
        try:
            with _instance_directory(self._root, metadata.id) as directory:
                existing = self._load_from(directory, path)
                if existing is not None:
                    self._require_same(path, existing, metadata)
                    return
                temporary = f".{_INSPECTION_METADATA_NAME}.{os.urandom(6).hex()}.tmp"
                descriptor = os.open(
                    temporary,
                    os.O_CREAT
                    | os.O_EXCL
                    | os.O_WRONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory,
                )
                try:
                    try:
                        _write_all(descriptor, contents)
                        sync_descriptor(descriptor)
                    finally:
                        os.close(descriptor)
                    try:
                        os.link(
                            temporary,
                            _INSPECTION_METADATA_NAME,
                            src_dir_fd=directory,
                            dst_dir_fd=directory,
                            follow_symlinks=False,
                        )
                        sync_descriptor(directory)
                    except FileExistsError:
                        existing = self._load_from(directory, path)
                        if existing is None:  # pragma: no cover - link reported it exists.
                            raise InstanceMetadataError.unsafe(
                                path, "snapshot disappeared"
                            ) from None
                        self._require_same(path, existing, metadata)
                finally:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=directory)
        except _InstanceDirectoryError as exc:
            raise InstanceMetadataError.unreadable(path, exc) from exc
        except InstanceMetadataError:
            raise
        except OSError as exc:
            raise InstanceMetadataError.unreadable(path, exc) from exc

    @staticmethod
    def _load_from(directory: int, path: Path) -> InstanceInspectionMetadata | None:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(_INSPECTION_METADATA_NAME, flags, dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise InstanceMetadataError.unreadable(path, exc) from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise InstanceMetadataError.unsafe(path, "snapshot is not a regular file")
            if opened.st_size > _INSPECTION_METADATA_BYTES:
                raise InstanceMetadataError.unsafe(path, "snapshot exceeds its byte bound")
            contents = _read_at_most(descriptor, _INSPECTION_METADATA_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(contents) > _INSPECTION_METADATA_BYTES:
            raise InstanceMetadataError.unsafe(path, "snapshot exceeds its byte bound")
        metadata = decode_json(
            contents,
            InstanceInspectionMetadata,
            source=str(path),
            max_bytes=_INSPECTION_METADATA_BYTES,
            options=ValidationOptions(strict=True),
        )
        if isinstance(metadata, Unreadable):
            raise InstanceMetadataError.unreadable(path, ValueError(metadata.details("snapshot")))
        return metadata

    @staticmethod
    def _require_same(
        path: Path,
        existing: InstanceInspectionMetadata,
        metadata: InstanceInspectionMetadata,
    ) -> None:
        if existing != metadata:
            raise InstanceMetadataError.unsafe(path, "an unlike immutable snapshot already exists")


class FakeInstanceMetadataStore:
    """In-memory immutable snapshot store for public-interface tests."""

    def __init__(self) -> None:
        """Start with no retained snapshots."""
        self.snapshots: dict[str, InstanceInspectionMetadata] = {}

    def load(self, instance_id: str) -> InstanceInspectionMetadata | None:
        """Return the retained snapshot, when present."""
        return self.snapshots.get(_checked_id(instance_id))

    def publish(self, metadata: InstanceInspectionMetadata) -> None:
        """Publish idempotently and reject a conflicting replacement."""
        existing = self.snapshots.get(metadata.id)
        if existing is not None and existing != metadata:
            path = Path("/run/vibesys/runs") / metadata.id / _INSPECTION_METADATA_NAME
            raise InstanceMetadataError.unsafe(path, "an unlike immutable snapshot already exists")
        self.snapshots[metadata.id] = metadata


def _read_at_most(descriptor: int, limit: int) -> bytes:
    """Read no more than ``limit`` bytes from a regular file descriptor."""
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, contents: bytes) -> None:
    """Write every byte or propagate the definite write failure."""
    view = memoryview(contents)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            message = "writing inspection metadata made no progress"
            raise OSError(message)
        view = view[written:]


def _bounded_utf8_tail(raw: bytes, limit: int) -> str:
    """Decode a byte suffix, preserving valid text and keeping its UTF-8 form bounded."""
    encoded = raw.decode("utf-8", errors="replace").encode("utf-8")
    if len(encoded) > limit:
        encoded = encoded[-limit:]
    return encoded.decode("utf-8", errors="ignore")


class FileInstanceHold:
    """A ``LifetimeLock`` plus the record file it vouches for."""

    def __init__(self, lock: LifetimeLock, record_path: Path) -> None:
        """Wrap a held lock; ``FileInstanceStore.hold`` creates these."""
        self._lock = lock
        self._record_path = record_path

    def publish(self, record: LiveInstanceRecord) -> None:
        """Write the record through a private temporary file and rename it."""
        _write_record(self._record_path, record)

    def release(self) -> None:
        """Drop the lock; the record stays as the tombstone."""
        self._lock.release()


def _write_record(path: Path, record: LiveInstanceRecord) -> None:
    temporary = path.with_name(f".{path.name}.{os.urandom(6).hex()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(record.model_dump_json() + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class FileInstanceStore:
    """The registry on a node-local filesystem under ``root``."""

    def __init__(self, root: Path, *, directory: str = INSTANCES_DIRECTORY) -> None:
        """Use ``root/<directory>`` (created private) for locks and records.

        Runs register in ``instances``; read-only servers that let Chat read ended runs
        (``server.host_helper_readers``) register in ``readers``, so no listing of runs sees them.
        """
        self._root = root
        self._directory = root / directory
        ensure_private_directory(self._directory, os.getuid())

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

    def bury(self, record: LiveInstanceRecord) -> bool:
        """Replace the record under the lock, if nobody holds it."""
        lock_path = self._lock_path(record.id)
        try:
            descriptor = os.open(lock_path, os.O_RDWR)
        except FileNotFoundError:
            # A released lock is never published again, so no holder can return.
            _write_record(self._record_path(record.id), record)
            return True
        try:
            if not try_lock(descriptor):
                return False
            _write_record(self._record_path(record.id), record)
            return True
        finally:
            os.close(descriptor)

    def hold(self, instance_id: str) -> FileInstanceHold:
        """Publish a held lock for ``instance_id``."""
        lock = LifetimeLock.publish(self._lock_path(instance_id))
        return FileInstanceHold(lock, self._record_path(instance_id))

    def _read(self, instance_id: str) -> LiveInstanceRecord | UnreadableRecord | None:
        try:
            # Bytes, so ``parse_record`` judges invalid UTF-8 unreadable; ``read_text``
            # raised ``UnicodeDecodeError`` out of every listing instead.
            raw = self._record_path(instance_id).read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return UnreadableRecord(id=instance_id, version=None)
        return parse_record(instance_id, raw)

    def _lock_path(self, instance_id: str) -> Path:
        return self._directory / f"{_checked_id(instance_id)}{_LOCK_SUFFIX}"

    def _record_path(self, instance_id: str) -> Path:
        return self._directory / f"{_checked_id(instance_id)}{_RECORD_SUFFIX}"


@dataclass
class _FakeEntry:
    lock: LockState
    raw: str | None = None


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
        self._store.entries[self._id].raw = record.model_dump_json()

    def release(self) -> None:
        """Drop the lock and keep the record; idempotent."""
        if self._released:
            return
        self._released = True
        self._store.entries[self._id].lock = LockState.ABSENT

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

    def plant(self, instance_id: str, raw: str, lock: LockState = LockState.HELD) -> None:
        """Store ``raw`` as an id's record, as a server of another version would write it."""
        self._published.add(_checked_id(instance_id))
        self.entries[instance_id] = _FakeEntry(lock, raw)

    def ids(self) -> tuple[str, ...]:
        """List ids with any state."""
        return tuple(sorted(self.entries))

    def observe(self, instance_id: str) -> InstanceObservation:
        """Return the entry's lock and record."""
        entry = self.entries.get(_checked_id(instance_id))
        if entry is None:
            return InstanceObservation(instance_id, LockState.ABSENT, None)
        record = None if entry.raw is None else parse_record(instance_id, entry.raw)
        return InstanceObservation(instance_id, entry.lock, record)

    def reap(self, instance_id: str) -> bool:
        """Remove an entry unless its lock is held or unreadable."""
        entry = self.entries.get(_checked_id(instance_id))
        if entry is None:
            return True
        if entry.lock in {LockState.HELD, LockState.UNKNOWN}:
            return False
        del self.entries[instance_id]
        return True

    def bury(self, record: LiveInstanceRecord) -> bool:
        """Replace a dead entry's record."""
        entry = self.entries.get(_checked_id(record.id))
        if entry is None or entry.lock in {LockState.HELD, LockState.UNKNOWN}:
            return False
        entry.raw = record.model_dump_json()
        return True

    def hold(self, instance_id: str) -> FakeInstanceHold:
        """Claim a never-published id."""
        if _checked_id(instance_id) in self._published:
            raise FileExistsError(instance_id)
        self._published.add(instance_id)
        self.entries[instance_id] = _FakeEntry(LockState.HELD)
        return FakeInstanceHold(self, instance_id)


# --- shell ------------------------------------------------------------------


class InstanceInspector:
    """Build one typed inspection from the registry and ID-scoped retained artifacts.

    When a live server ends while its log is read, the result says ``ended`` but retains the
    record from the first observation. A refresh therefore never presents a live state after the
    lock has dropped.
    """

    def __init__(
        self,
        store: InstanceStore,
        logs: InstanceLogs,
        metadata: InstanceMetadataStore,
        clock: Clock,
    ) -> None:
        """Compose registry observations with ID-scoped retained artifacts.

        ``clock`` is on the epoch timeline records are stamped with (``SystemClock``);
        it decides when a holder wedged after its tombstone inspects as live again.
        """
        self._store = store
        self._logs = logs
        self._metadata = metadata
        self._clock = clock

    def inspect(self, instance_id: str, *, include_log: bool = False) -> InstanceInspection:
        """Observe one id and optionally load its bounded diagnostic tail."""
        instance_id = _checked_id(instance_id)
        metadata = self._metadata.load(instance_id)
        before = self._store.observe(instance_id)
        log = self._logs.tail(instance_id) if include_log else None
        after = self._store.observe(instance_id) if include_log else before
        now = self._clock.now()
        state = _inspection_state(after, now)
        record = (
            after.record
            if state is InspectionState.LIVE and isinstance(after.record, LiveInstanceRecord)
            else before.record
            if _inspection_state(before, now) is InspectionState.LIVE
            and isinstance(before.record, LiveInstanceRecord)
            else None
        )
        paths = self._logs.paths(instance_id)
        control_socket = (
            metadata.control_socket_path if metadata is not None else str(paths.control_socket)
        )
        server_log = metadata.server_log_path if metadata is not None else str(paths.server_log)
        return InstanceInspection(
            id=instance_id,
            state=state,
            record=record,
            metadata=metadata,
            control_socket_path=control_socket,
            server_log_path=server_log,
            log=log,
        )


def _inspection_state(observation: InstanceObservation, now: float) -> InspectionState:
    if listed_live(observation, now) is not None:
        return InspectionState.LIVE
    match observation.lock, observation.record:
        case LockState.HELD, LiveInstanceRecord():
            return InspectionState.ENDED
        case LockState.HELD | LockState.UNKNOWN, _:
            return InspectionState.UNVERIFIED
        case _:
            return InspectionState.ENDED


@dataclass(frozen=True)
class StopEffects:
    """The I/O one ``LiveRegistry.stop`` uses: ask, record the stop, signal, and wait."""

    requester: StopRequester
    signaller: ProcessSignaller
    clock: Clock
    pause: Callable[[float], None]
    intents: StopIntents


class LiveRegistry:
    """List, find, register and stop detached servers over one ``InstanceStore``."""

    def __init__(self, store: InstanceStore, clock: Clock) -> None:
        """Compose the pure decisions with ``store``; ``clock`` ages tombstones."""
        self._store = store
        self._clock = clock

    def list(self) -> InstanceList:
        """Report live and recently ended servers; bury the crashed and remove the expired.

        The writing listing: an explicit ``instances list`` or ``runs.list``. A
        crashed server's tombstone is stamped with the time this listing found it.
        """
        now = self._clock.now()
        decision = self._survey(now)
        buried = tuple(crashed(record, now) for record in decision.crashed)
        for record in buried:
            self._store.bury(record)
        for instance_id in decision.dead:
            self._store.reap(instance_id)
        return _listing(decision, buried)

    def read(self) -> InstanceList:
        """Report what :meth:`list` would, without writing a tombstone or removing a record.

        For a watch, which reads the registry every second: a crashed server is
        reported without ``ended_at`` until a writing listing buries it, and an
        expired record stays on disk, unreported, until one removes it.
        """
        decision = self._survey(self._clock.now())
        return _listing(decision, decision.crashed)

    def _survey(self, now: float) -> Survey:
        return survey((self._store.observe(instance_id) for instance_id in self._store.ids()), now)

    def find(self, instance_id: str) -> LiveInstanceRecord | None:
        """Return the record of a server a client can still reach, else ``None``.

        Only a live holder that has not written its exit record. A server wedged
        in its exit lists as running (``listed_live``) so an operator can see and
        stop it, but it is gone for ``run.connect`` and attach: its run has
        ended and its socket no longer serves, so dialing it would report a
        connect failure a client retries instead of ``RUN_GONE``.
        """
        observation = self._store.observe(instance_id)
        record = observation.record
        live = judge(observation, self._clock.now()) is Verdict.LIVE
        return record if live and isinstance(record, LiveInstanceRecord) else None

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

        A server that has written its tombstone but still holds its lock is
        exiting: within ``EXIT_GRACE_SECONDS`` of its tombstone it is given
        ``STOP_TIMEOUT_SECONDS`` to let go, then (or at once, under ``force``, or
        once past the grace) sent SIGTERM, since its control socket no longer
        serves. A live server is first asked over its control socket, which stops the run
        at its next safe boundary on every platform. Only a server that does
        not acknowledge (wedged, or still binding), or a ``force`` stop, is
        sent SIGTERM; the signal goes only while the lock is still held,
        through a stable process reference, so a reused pid is never hit.
        """
        observation = self._store.observe(instance_id)
        record = observation.record
        now = self._clock.now()
        verdict = judge(observation, now)
        if verdict not in {Verdict.LIVE, Verdict.EXITING} or not isinstance(
            record, LiveInstanceRecord
        ):
            return InstanceStopResult(id=instance_id, outcome=StopOutcome.NOT_RUNNING)
        # Only a holder still within its exit grace may yet let go by itself; one
        # past it is wedged, and waiting longer would not change that.
        if (
            verdict is Verdict.EXITING
            and not force
            and exited_in_grace(record, now)
            and self._await_release(instance_id, effects)
        ):
            # It wrote its tombstone and let go: it stopped by itself.
            return InstanceStopResult(id=instance_id, outcome=StopOutcome.STOPPED)
        if (
            verdict is Verdict.LIVE
            and not force
            and effects.requester.request_stop(record.socket_path)
        ):
            released = self._await_release(instance_id, effects)
            return InstanceStopResult(
                id=instance_id,
                outcome=StopOutcome.STOPPED if released else StopOutcome.STOPPING,
                route=StopRoute.CONTROL_SOCKET,
            )
        # A signalled server cannot commit its run's stop, so the request is kept durably
        # first: a resume then only finishes the drain and ends the run.
        effects.intents.record(record)
        try:
            sent = effects.signaller.terminate_if_current(
                record.pid, lambda: self._held(instance_id)
            )
        except ProcessLookupError:
            sent = False
        except NotImplementedError:
            return InstanceStopResult(id=instance_id, outcome=StopOutcome.UNSUPPORTED)
        if not sent:
            return InstanceStopResult(id=instance_id, outcome=StopOutcome.NOT_RUNNING)
        released = self._await_release(instance_id, effects)
        return InstanceStopResult(
            id=instance_id,
            outcome=StopOutcome.STOPPED if released else StopOutcome.STILL_RUNNING,
            route=StopRoute.SIGNAL,
        )

    def _await_release(self, instance_id: str, effects: StopEffects) -> bool:
        """Wait, bounded, for the holder to drop its lock; its tombstone stays for listings."""
        deadline = effects.clock.now() + STOP_TIMEOUT_SECONDS
        while self._held(instance_id):
            if effects.clock.now() >= deadline:
                return False
            effects.pause(_STOP_POLL_SECONDS)
        return True

    def _held(self, instance_id: str) -> bool:
        return self._store.observe(instance_id).lock is LockState.HELD


def _listing(decision: Survey, crashed_records: Iterable[LiveInstanceRecord]) -> InstanceList:
    return InstanceList(
        instances=decision.live,
        ended=_by_start([*decision.ended, *crashed_records]),
        unreadable=decision.unreadable,
        unverified=decision.unverified,
    )


def host_facts() -> tuple[str, str]:
    """Return this node's hostname and the installed VibeSys version."""
    try:
        version = distribution_version("vibesys")
    except PackageNotFoundError:
        version = "0+unknown"
    return platform.node(), version


def parse_instance_id(value: str) -> str:
    """Return ``value`` if it is a registry id; ``ValueError`` naming the pattern otherwise."""
    return _checked_id(value)


def _checked_id(instance_id: str) -> str:
    if not _INSTANCE_ID.match(instance_id):
        message = f"instance id {instance_id!r} must match {INSTANCE_ID_PATTERN}"
        raise ValueError(message)
    return instance_id
