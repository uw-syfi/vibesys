"""Fakes of the host helper's interfaces, and a host made of them, for deterministic tests.

The registry is the real ``LiveRegistry`` over ``FakeInstanceStore``; directories,
launches and the checkout are in-memory Fakes held to the same contracts as the
real implementations in ``server.host_helper_os``; projects are ``FileProjects`` on a
temporary directory, since temporary files are test inputs.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from server.host_api import (
    Checkout,
    DetachedLaunchFailure,
    InstanceStatus,
    LiveInstanceRecord,
    RunPhase,
    RunStatus,
    parse_helper_message,
)
from server.host_helper import HelperHost, PathHints, Scan, ScannedEntry, serve_channel
from server.host_helper_os import FileProjects
from server.instances import ControlSocketStopRequester
from server.registry import FakeInstanceStore, LiveRegistry, StopEffects
from vs_sim.api.testing import HANG_GUARD_S, FakeProcessSignaller

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from typing import Any

    from vs_sim.api import Network, Threads, Worker
    from vs_sim.api.testing import SimThreads

HOME = "/home/user"
VIBESYS_VERSION = "0+test"


@dataclass
class FakeDirectories:
    """An in-memory tree: each path maps to its entries, ``True`` for a directory.

    A path in ``denied`` raises ``PermissionError`` when scanned; a path that is a
    file raises ``NotADirectoryError``; anything else absent raises ``FileNotFoundError``.
    """

    tree: Mapping[str, Mapping[str, bool]]
    denied: frozenset[str] = frozenset()
    hinted: Mapping[str, PathHints] = field(default_factory=dict)
    scanned: list[str] = field(default_factory=list)

    def home(self) -> str:
        return HOME

    def expand(self, path: str) -> str:
        if path == "~" or path.startswith("~/"):
            return HOME + path[1:]
        return path

    def scan(self, path: str, limit: int) -> Scan:
        self.scanned.append(path)
        directory = path.rstrip("/") or "/"
        if directory in self.denied:
            raise PermissionError(path)
        if directory not in self.tree:
            parent, name = posixpath.split(directory)
            if self.tree.get(parent, {}).get(name) is False:
                raise NotADirectoryError(path)
            raise FileNotFoundError(path)
        entries = tuple(ScannedEntry(name, is_dir) for name, is_dir in self.tree[directory].items())
        return Scan(entries=entries[:limit], complete=len(entries) <= limit)

    def hints(self, path: str) -> PathHints:
        return self.hinted.get(
            path, PathHints(vibesys_checkout=False, vibesys_project=False, git_repository=False)
        )


@dataclass
class FakeRunLauncher:
    """Answers each launch with ``outcome`` for its project and arguments, recording the call."""

    outcome: Callable[[Path, tuple[str, ...]], LiveInstanceRecord | DetachedLaunchFailure]
    calls: list[tuple[Path, tuple[str, ...]]] = field(default_factory=list)

    def start(
        self, project: Path, arguments: tuple[str, ...]
    ) -> LiveInstanceRecord | DetachedLaunchFailure:
        self.calls.append((project, arguments))
        return self.outcome(project, arguments)


@dataclass(frozen=True)
class FakeCheckout:
    """A checkout description fixed by the test."""

    checkout: Checkout | None = None

    def describe(self) -> Checkout | None:
        return self.checkout


def record(
    instance_id: str,
    *,
    run_id: str = "run-1",
    project_root: str = "/project",
    phase: RunPhase | None = None,
    ended_at: float | None = None,
) -> LiveInstanceRecord:
    """A serving record (or, with ``ended_at``, a tombstone) for tests."""
    return LiveInstanceRecord(
        id=instance_id,
        status=InstanceStatus.SERVING,
        phase=phase or RunPhase(status=RunStatus.RUNNING),
        socket_path="run.sock",
        log_path=f"/run/{instance_id}/server.log",
        project_root=project_root,
        run_id=run_id,
        pid=4242,
        started_at=1.0,
        ended_at=ended_at,
        hostname="node",
        vibesys_version=VIBESYS_VERSION,
    )


def failed_launch(_: Path, __: tuple[str, ...]) -> DetachedLaunchFailure:
    """A launch that starts nothing."""
    return DetachedLaunchFailure(
        code="invalid_arguments", stage="argument_parsing", message="no", exit_code=2
    )


def fake_host(
    threads: Threads,
    network: Network,
    *,
    store: FakeInstanceStore | None = None,
    directories: FakeDirectories | None = None,
    launcher: FakeRunLauncher | None = None,
) -> HelperHost:
    """A helper host over Fakes, on ``threads``' clock and ``network``."""
    return HelperHost(
        registry=LiveRegistry(store or FakeInstanceStore(), threads),
        stop_effects=StopEffects(
            requester=ControlSocketStopRequester(network),
            signaller=FakeProcessSignaller(set()),
            clock=threads,
            pause=threads.sleep,
        ),
        projects=FileProjects(),
        directories=directories or FakeDirectories(tree={HOME: {}}),
        launcher=launcher or FakeRunLauncher(failed_launch),
        checkout=FakeCheckout(),
        dialer=network,
        vibesys_version=VIBESYS_VERSION,
    )


@dataclass
class ChannelClient:
    """The client's end of a channel on simulated threads: what it sends, and what the helper wrote."""

    threads: SimThreads
    pending: bytearray = field(default_factory=bytearray)
    closed: bool = False
    data: bytearray = field(default_factory=bytearray)
    status: int | None = None

    def __post_init__(self) -> None:
        self._cond = self.threads.condition()

    def send(self, message: dict[str, Any] | bytes) -> None:
        with self._cond:
            self.pending += (
                message if isinstance(message, bytes) else json.dumps(message).encode() + b"\n"
            )
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    def read(self, max_bytes: int) -> bytes:
        with self._cond:
            self._cond.wait_for(lambda: bool(self.pending) or self.closed)
            chunk = bytes(self.pending[:max_bytes])
            del self.pending[:max_bytes]
            return chunk

    def write(self, data: bytes) -> None:
        with self._cond:
            self.data += data
            self._cond.notify_all()

    def receive(self) -> dict[str, Any]:
        """The next message line the helper wrote."""
        with self._cond:
            assert self._cond.wait_for(lambda: b"\n" in self.data, HANG_GUARD_S)
            line, _, rest = bytes(self.data).partition(b"\n")
            self.data = bytearray(rest)
        parse_helper_message(line)
        return json.loads(line)


@dataclass
class ByteBuffer:
    """A sink that keeps what was written."""

    data: bytearray = field(default_factory=bytearray)

    def write(self, data: bytes) -> None:
        self.data += data


def serve_on(
    threads: SimThreads, client: ChannelClient, errors: ByteBuffer, host: HelperHost
) -> Worker:
    """Serve ``client``'s channel on a worker; its exit status lands in ``client.status``."""

    def run() -> None:
        client.status = serve_channel(
            host=host,
            source=client,
            output=client,
            errors=errors,
            threads=threads,
        )

    return threads.spawn(run, name="helper")
