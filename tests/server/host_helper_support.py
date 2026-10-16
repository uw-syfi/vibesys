"""Fakes of the host helper's interfaces, and a host made of them, for deterministic tests.

The registry is the real ``LiveRegistry`` over ``FakeInstanceStore``; directories,
launches and the checkout are in-memory Fakes held to the same contracts as the
real implementations in ``server.host_helper_os``; projects are ``FileProjects`` on a
temporary directory, since temporary files are test inputs.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass, field, replace
from datetime import UTC
from typing import TYPE_CHECKING

from tests.support.example_materializer import FakeExampleMaterializer

from entrypoints.examples import HelperExampleProjects
from server.host_api import (
    Checkout,
    DetachedLaunchFailure,
    HarnessOption,
    InstanceStatus,
    LiveInstanceRecord,
    LogOffset,
    LogPipe,
    LogPipeSpan,
    LogStream,
    LogStreamOutcome,
    LogTail,
    RunPhase,
    RunStatus,
    parse_helper_message,
)
from server.host_helper import serve_channel
from server.host_helper_logs import (
    HelperLogs,
    StoredBytes,
    UnknownInstanceError,
    UnknownLogStreamError,
    clip_pipes,
)
from server.host_helper_machine import HostMachineProbe
from server.host_helper_os import FileProjects
from server.host_helper_requests import HelperHost, PathHints, Scan, ScannedEntry
from server.host_preflight import Check, CheckId, CheckState, PreflightReport, SettingsValid
from server.instances import ControlSocketStopRequester
from server.registry import (
    FakeInstanceLogs,
    FakeInstanceMetadataStore,
    FakeInstanceStore,
    FakeStopIntents,
    InstanceInspector,
    LiveRegistry,
    StopEffects,
)
from vs_decode.api import Unreadable
from vs_sim.api.testing import HANG_GUARD_S, FakeProcessSignaller, ScriptedProbe

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from typing import Any

    from pydantic import JsonValue

    from vs_sim.api import Network, ProbeResult, Threads, Worker
    from vs_sim.api.testing import SimThreads

HOME = "/home/user"
VIBESYS_VERSION = "0+test"
HARNESSES = (
    HarnessOption(provider="codex", model="gpt-a", default=True),
    HarnessOption(provider="codex", model="gpt-b", default=False),
    HarnessOption(provider="claude", model="claude-a", default=True),
)
"""The vetted pairs a fake host offers: two providers, each default first."""


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


@dataclass
class FakeRunPreflight:
    """A ``RunPreflight`` that answers ``report`` and notes each project and arguments asked."""

    report_: PreflightReport = field(
        default_factory=lambda: PreflightReport(
            settings=SettingsValid(),
            uses_docker=True,
            agent_image=Check(id=CheckId.AGENT_IMAGE, state=CheckState.OK, detail="cached"),
            evaluator=Check(
                id=CheckId.EVALUATOR_AVAILABLE, state=CheckState.OK, detail="benchmark command"
            ),
        )
    )
    calls: list[tuple[Path, tuple[str, ...]]] = field(default_factory=list)

    def report(self, project: Path, arguments: tuple[str, ...]) -> PreflightReport:
        self.calls.append((project, arguments))
        return self.report_


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


@dataclass
class _FakeStream:
    owner: dict[str, JsonValue]
    command: str
    held: bytearray = field(default_factory=bytearray)
    oldest: int = 0
    """The offset of ``held[0]``; bytes before it were evicted."""
    pipes: list[LogPipeSpan] = field(default_factory=list)
    outcome: LogStreamOutcome | None = None

    @property
    def end(self) -> int:
        return self.oldest + len(self.held)


@dataclass
class FakeLogReader:
    """An in-memory log store with the ``LogReader`` contract, and the writes a test makes to it.

    ``evict`` drops a stream's oldest bytes the way a capped store does. Callers on
    several simulated threads share it under ``threads``' lock, as the real store's
    files are shared.
    """

    threads: Threads
    instances: dict[str, dict[str, _FakeStream]] = field(default_factory=dict)
    opened: list[str] = field(default_factory=list)
    """Each instance ``instance`` opened, in order: one per logs request."""

    def __post_init__(self) -> None:
        self._lock = self.threads.lock()

    def open(
        self, instance_id: str, stream_id: str, owner: dict[str, JsonValue], command: str
    ) -> None:
        with self._lock:
            self.instances.setdefault(instance_id, {})[stream_id] = _FakeStream(owner, command)

    def write(
        self, instance_id: str, stream_id: str, data: bytes, pipe: LogPipe = LogPipe.STDOUT
    ) -> None:
        with self._lock:
            stream = self.instances[instance_id][stream_id]
            assert stream.outcome is None
            if not data:
                return
            last = stream.pipes[-1] if stream.pipes else None
            if last is not None and last.pipe is pipe:
                stream.pipes[-1] = last.model_copy(update={"length": last.length + len(data)})
            else:
                stream.pipes.append(LogPipeSpan(offset=stream.end, length=len(data), pipe=pipe))
            stream.held += data

    def close(
        self, instance_id: str, stream_id: str, outcome: LogStreamOutcome = LogStreamOutcome.ENDED
    ) -> None:
        with self._lock:
            self.instances[instance_id][stream_id].outcome = outcome

    def evict(self, instance_id: str, stream_id: str, before: int) -> None:
        with self._lock:
            stream = self.instances[instance_id][stream_id]
            drop = max(0, min(before, stream.end) - stream.oldest)
            del stream.held[:drop]
            stream.oldest += drop
            stream.pipes = list(clip_pipes(tuple(stream.pipes), stream.oldest, stream.end))

    def streams(self, instance_id: str) -> tuple[LogStream, ...]:
        with self._lock:
            return tuple(
                LogStream(
                    id=stream_id,
                    owner=stream.owner,
                    command=stream.command,
                    retained_start=stream.oldest,
                    size=stream.end,
                    outcome=stream.outcome,
                )
                for stream_id, stream in self._instance(instance_id).items()
            )

    def read(
        self, instance_id: str, stream_id: str, start: LogOffset | LogTail, max_bytes: int
    ) -> StoredBytes:
        with self._lock:
            streams = self._instance(instance_id)
            if stream_id not in streams:
                raise UnknownLogStreamError(stream_id)
            stream = streams[stream_id]
            match start:
                case LogOffset(offset=offset):
                    wanted = offset
                case LogTail(tail_bytes=tail_bytes):
                    wanted = max(stream.end - min(tail_bytes, max_bytes), 0)
            first = max(wanted, stream.oldest)
            index = first - stream.oldest
            data = bytes(stream.held[index : index + max_bytes])
            return StoredBytes(
                offset=first,
                data=data,
                pipes=clip_pipes(tuple(stream.pipes), first, first + len(data)),
                truncated_before=wanted < stream.oldest,
                eof=first + len(data) >= stream.end,
                closed=stream.outcome is not None,
            )

    def instance(self, instance_id: str) -> FakeOpenedLogs:
        """The ``LogReader`` role: ``instance_id``'s logs, opened; each read sees later writes."""
        with self._lock:
            self._instance(instance_id)
            self.opened.append(instance_id)
        return FakeOpenedLogs(self, instance_id)

    def _instance(self, instance_id: str) -> dict[str, _FakeStream]:
        if instance_id not in self.instances:
            raise UnknownInstanceError(instance_id)
        return self.instances[instance_id]


@dataclass(frozen=True)
class FakeOpenedLogs:
    """``FakeLogReader``'s ``InstanceLogs``: one instance's streams in the shared Fake."""

    store: FakeLogReader
    instance_id: str

    def streams(self) -> tuple[LogStream, ...]:
        return self.store.streams(self.instance_id)

    def read(self, stream_id: str, start: LogOffset | LogTail, max_bytes: int) -> StoredBytes:
        return self.store.read(self.instance_id, stream_id, start, max_bytes)


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
    """A helper host over Fakes, on ``threads``' clock and ``network``, with no log store."""
    instance_store = store or FakeInstanceStore()
    return HelperHost(
        registry=LiveRegistry(instance_store, threads),
        inspector=InstanceInspector(
            instance_store,
            FakeInstanceLogs(),
            FakeInstanceMetadataStore(),
            threads,
        ),
        stop_effects=StopEffects(
            requester=ControlSocketStopRequester(network, clock=threads, deadline_clock=threads),
            signaller=FakeProcessSignaller(set()),
            clock=threads,
            pause=threads.sleep,
            intents=FakeStopIntents(),
        ),
        projects=FileProjects(),
        directories=directories or FakeDirectories(tree={HOME: {}}),
        launcher=launcher or FakeRunLauncher(failed_launch),
        preflight=FakeRunPreflight(),
        examples=HelperExampleProjects(None, FakeExampleMaterializer()),
        checkout=FakeCheckout(),
        dialer=network,
        vibesys_version=VIBESYS_VERSION,
        logs=None,
        harnesses=lambda: HARNESSES,
        machine=quiet_machine(),
        clock=threads,
        timezone=UTC,
    )


def quiet_machine(
    script: Callable[[tuple[str, ...]], ProbeResult | None] = lambda _argv: None,
    *,
    installed: frozenset[str] = frozenset(),
    installed_in: Mapping[str, frozenset[str]] | None = None,
    launch_path: str = "/usr/bin",
) -> HostMachineProbe:
    """The real machine probe over ``script``'s commands: 4 cores, programs on a search path.

    The default script answers no command, so the machine has no GPU and no Docker.
    ``installed`` are the programs in ``/usr/bin`` and ``installed_in`` those in other
    directories; ``launch_path`` is the ``PATH`` a run launches provider CLIs with.
    """
    directories = {"/usr/bin": installed, **(installed_in or {})}

    def which(program: str, path: str | None) -> str | None:
        for directory in (path or "").split(":"):
            if program in directories.get(directory, frozenset()):
                return f"{directory}/{program}"
        return None

    return HostMachineProbe(
        ScriptedProbe(script),
        cpu_cores=lambda: 4,
        which=which,
        harness_binaries=lambda: (("claude", "claude"), ("codex", "codex")),
        launch_environment=lambda: {"PATH": launch_path},
    )


def with_logs(host: HelperHost, logs: FakeLogReader, threads: Threads) -> HelperHost:
    """``host`` with ``logs`` as its log store, waited on with ``threads``' clock: it offers ``logs``."""
    return replace(host, logs=HelperLogs(logs, threads))


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
        assert not isinstance(parse_helper_message(line), Unreadable), line
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
