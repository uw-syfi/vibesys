"""The process that holds one session: ``python -m vs_sidecar``, with no arguments.

Its one input is a binary ``SessionBootstrap`` on standard input, which says where to listen and
where its private state is. Everything else arrives over ``AgentSessionService``: the host starts
this process in its own session so it outlives the host, says ``Hello``, and sends ``Open``. Everything
the session did is in the state directory, so a restarted process can tell the host what happened.

The agent is a child of this process and leads a process group of its own. A process that is killed
without warning cannot stop it, so the process holds an exclusive lock for its whole life and keeps a
``SessionProcesses`` record of its pid and the agent's groups, both at paths the bootstrap names. The
host tries the lock to learn whether this process lives, and reaps the groups when it is free (see
the README, "Process lifetime").
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, NoReturn

import protovalidate
from google.protobuf.message import DecodeError

from vs_sidecar.driver import AgentshimDriver, build_session
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc
from vs_sidecar.holder import HolderLock, HolderLockedError, SessionHolder, SessionProcessesFile
from vs_sidecar.server import Wakeup, serve
from vs_sidecar.store import FileStore

if TYPE_CHECKING:
    from collections.abc import Callable

    import grpc
    from agentshim import RawFrame

    from vs_sidecar.driver import Driver

    DriverFactory = Callable[[Callable[[RawFrame], None]], Driver]
    #: Makes the drivers of a session, from its request and the process's state directory.
    SessionDrivers = Callable[[sc.OpenSessionRequest, Path], DriverFactory]

#: How long the worker sleeps between looks at the provider when nothing else wakes it.
IDLE_TICK_SECONDS = 0.25
#: How long the process stays up after the session closes, for the case where no daemon is there to
#: end it: one that is down, or restarting, still finds the close when it reattaches. A running daemon
#: does not wait for this; it ends the sidecar as soon as it has the close ("Reattach" and the
#: release in orchestrator/internal/daemon), so a stopped run leaves no sidecar behind.
LINGER_SECONDS = 60.0
#: How long a stopped sidecar waits for its worker to wind down before it gives up on it.
STOP_SECONDS = 30.0
#: How long a process nobody has opened waits before it ends. A host that started it and died comes
#: back well within this and opens it; one that does not has left nothing that needs it. It is as long
#: as the host gives a session to open (``core.LaunchTimeout``).
OPEN_WAIT_SECONDS = 300.0
#: The program that lists every process and its parent, where there is no ``/proc`` to read.
PS = "/bin/ps"


class BootstrapError(ValueError):
    """The bootstrap cannot start a process; the message names what is wrong."""


def read_bootstrap(stream: BinaryIO) -> sc.SessionBootstrap:
    """The bootstrap in *stream*, read to its end. Raises ``BootstrapError`` for a bad one."""
    bootstrap = sc.SessionBootstrap()
    try:
        bootstrap.ParseFromString(stream.read())
        protovalidate.Validator().validate(bootstrap)
    except DecodeError as error:
        msg = f"standard input is not a SessionBootstrap: {error}"
        raise BootstrapError(msg) from error
    except protovalidate.ValidationError as error:
        msg = f"the bootstrap is invalid: {error}"
        raise BootstrapError(msg) from error
    return bootstrap


def child_pids(proc: Path = Path("/proc"), table: Callable[[], str] | None = None) -> list[int]:
    """The process ids of this process's children.

    They are read from *proc* when the system has one that lists them (Linux). Otherwise, as on macOS,
    they come from the process table *table* gives, in the form of ``ps -A -o pid=,ppid=``.
    """
    found: list[int] = []
    listed = False
    for children in sorted(proc.glob("self/task/*/children")):
        listed = True
        try:
            found.extend(int(token) for token in children.read_text().split())
        except (OSError, ValueError):
            continue
    if listed:
        return sorted(set(found))
    return sorted(_children_in(table() if table is not None else _process_table(), os.getpid()))


def _process_table() -> str:
    """Every process and its parent, one ``pid ppid`` pair a line."""
    # lint-waiver: LW-770101 [S603]; a fixed program with fixed arguments and no input of ours.
    # > The alternative is a libproc binding through ctypes, which would be the only unchecked foreign
    # > call in this process; ps is the same table on Linux and macOS and its output is a fixed shape.
    return subprocess.run(  # noqa: S603
        [PS, "-A", "-o", "pid=,ppid="], capture_output=True, text=True, check=True
    ).stdout


def process_started(pid: int) -> str:
    """When the process *pid* started, as ``ps`` prints it in a fixed locale and zone.

    The text is an opaque token the host compares for equality, read the same way on Linux and macOS
    with no ``/proc``. It is empty when it cannot be read, which the host takes to prove nothing.
    """
    # lint-waiver: LW-770104 [S603]; a fixed program with a number for its one argument.
    # > Same reason as the process table above: ps is the one source that is the same on Linux and macOS.
    done = subprocess.run(  # noqa: S603
        [PS, "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
        env={"LC_ALL": "C", "TZ": "UTC"},
    )
    return done.stdout.strip() if done.returncode == 0 else ""


def _children_in(table: str, parent: int) -> list[int]:
    children: list[int] = []
    for line in table.splitlines():
        try:
            pid, ppid = line.split()
        except ValueError:
            continue
        if ppid == str(parent):
            children.append(int(pid))
    return children


def agent_groups(proc: Path = Path("/proc"), table: Callable[[], str] | None = None) -> list[int]:
    """The process groups led by this process's children: the agent, which starts a session."""
    groups: list[int] = []
    for pid in child_pids(proc, table):
        try:
            if os.getpgid(pid) == pid:
                groups.append(pid)
        except ProcessLookupError:
            continue
    return groups


def real_drivers(request: sc.OpenSessionRequest, _state: Path) -> DriverFactory:
    """Drivers over the real provider for *request*."""
    preferred = None if request.resume_token else _conversation_id(request)

    def make(raw_sink: Callable[[RawFrame], None]) -> Driver:
        return AgentshimDriver(
            lambda conversation_id: build_session(
                request, raw_sink=raw_sink, new_conversation_id=conversation_id
            ),
            preferred_id=preferred,
            workdir=request.working_directory,
        )

    return make


def _conversation_id(request: sc.OpenSessionRequest) -> str:
    """A conversation id derived from the session, so a relaunch of the same session gets the same one."""
    name = f"{request.session.agent_id}/{request.session.session}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def work(
    engine: Engine,
    wakeup: Wakeup,
    stop: threading.Event,
) -> None:
    """The worker: run queued turns, poll the provider between them, until closed or stopped.

    Raises a store failure that a command met on a server thread.
    """
    while not stop.is_set() and not engine.finished:
        engine.check()
        seen = wakeup.version
        if engine.run_next():
            continue
        engine.poll_idle()
        wakeup.wait(seen, IDLE_TICK_SECONDS)


def keep_recording(groups: SessionProcessesFile, done: threading.Event) -> None:
    """Record *groups* every tick until *done*.

    An agent the provider respawns mid-turn is named within a tick of its start.
    """
    groups.record()
    while not done.wait(IDLE_TICK_SECONDS):
        groups.record()


def leave_unopened() -> NoReturn:
    """End a process nobody opened, at once.

    It holds no agent, so nothing needs winding down.
    """
    os._exit(0)


def serve_holder(
    bootstrap: sc.SessionBootstrap,
    drivers: SessionDrivers,
    *,
    open_wait: float = OPEN_WAIT_SECONDS,
    leave: Callable[[], NoReturn] = leave_unopened,
) -> None:
    """Serve until the session ends or a signal stops it.

    On the way out, whatever the reason (a signal, a failed write, a bug), the agent is closed, so
    it does not outlive this process's own knowledge of it. A failure in the worker is raised here
    after that cleanup.
    """
    state = Path(bootstrap.state_directory)
    state.mkdir(parents=True, exist_ok=True)
    # First, and for the whole life of the process: a second process for this session ends here, at once,
    # whether or not the first one listens yet.
    try:
        lock = HolderLock.take(Path(bootstrap.lock_file))
    except HolderLockedError as error:
        sys.stderr.write(f"vs_sidecar: another process lives for this session: {error}\n")
        leave()
    with lock:
        _serve_locked(bootstrap, drivers, open_wait=open_wait, leave=leave)


def _serve_locked(
    bootstrap: sc.SessionBootstrap,
    drivers: SessionDrivers,
    *,
    open_wait: float,
    leave: Callable[[], NoReturn],
) -> None:
    """Serve while holding the lock, which the caller frees when this returns."""
    state = Path(bootstrap.state_directory)
    socket = Path(bootstrap.listen_socket)
    wakeup = Wakeup()
    holder = SessionHolder(
        lambda request: Engine(
            request.session, FileStore.at(state), drivers(request, state), wakeup.notify
        )
    )
    groups = SessionProcessesFile(
        Path(bootstrap.processes_file), os.getpid(), agent_groups, process_started
    )
    ending = _Ending()

    def stopped(*_: object) -> None:
        ending.stop.set()
        ending.wake.set()
        holder.abandon_waiting()

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, stopped)
    # Before the socket answers, so whoever reaches this process finds the pid that goes with it.
    groups.clear()
    server = serve(holder, wakeup, socket)
    engine = holder.wait_opened(open_wait)
    if engine is None:
        leave()
    run_session(engine, wakeup, server, groups, ending)


@dataclass(frozen=True)
class _Ending:
    """What ends a session: *stop* asks for it, and *wake* rouses whoever waits for the end."""

    stop: threading.Event = field(default_factory=threading.Event)
    wake: threading.Event = field(default_factory=threading.Event)


def run_session(
    engine: Engine,
    wakeup: Wakeup,
    server: grpc.Server,
    groups: SessionProcessesFile,
    ending: _Ending,
) -> None:
    """Run *engine*'s worker until the session ends or it is stopped, then wind down."""
    stop, wake = ending.stop, ending.wake
    failure: list[BaseException] = []
    recorded = threading.Event()
    recorder = threading.Thread(
        target=keep_recording, args=(groups, recorded), name="vs-sidecar-groups", daemon=True
    )
    recorder.start()

    def run() -> None:
        try:
            engine.start()
            work(engine, wakeup, stop)
        # lint-waiver: LW-770003 [BLE001]; the worker must report any failure to the main thread.
        # > A narrower except would let a store failure or a bug end the thread silently, and then
        # > the agent would be left running; the main thread re-raises what is caught here.
        except BaseException as error:  # noqa: BLE001
            failure.append(error)
        finally:
            wake.set()

    try:
        # A daemon thread: a worker still stuck after STOP_SECONDS must not hold the process open.
        worker = threading.Thread(target=run, name="vs-sidecar-worker", daemon=True)
        worker.start()
        wake.wait()
        if stop.is_set():
            engine.shutdown()
        worker.join(STOP_SECONDS)
        if not stop.is_set() and engine.finished:
            stop.wait(LINGER_SECONDS)
    finally:
        engine.release()
        recorded.set()
        recorder.join()
        groups.clear()
        server.stop(grace=1).wait()
    if failure:
        raise failure[0]


def main(drivers: SessionDrivers = real_drivers) -> int:
    """Run one process. Returns its exit code."""
    if sys.argv[1:]:
        sys.stderr.write(f"vs_sidecar takes no arguments: {sys.argv[1:]}\n")
        return 2
    try:
        bootstrap = read_bootstrap(sys.stdin.buffer)
    except BootstrapError as error:
        sys.stderr.write(f"vs_sidecar: {error}\n")
        return 2
    serve_holder(bootstrap, drivers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
