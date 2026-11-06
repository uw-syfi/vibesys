"""The sidecar process: ``python -m vs_sidecar --launch FILE --state-dir DIR --socket PATH``.

The daemon writes the ``LaunchSession`` it wants (protobuf JSON) to a file, starts this process in
its own session so it outlives the daemon, and talks to it over the socket. Everything the session
did is in ``--state-dir``, so a restarted sidecar can tell the daemon what happened.

The agent is a child of this process and leads a process group of its own. A sidecar that is killed
without warning cannot stop it, so the state directory names both for the daemon, which reaps them
(see the README, "Process lifetime"): ``sidecar.pid`` and ``agent.pgids``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import protovalidate
from agentshim import StdioMcpServer
from google.protobuf import json_format

from vs_sidecar.driver import AgentshimDriver, build_session
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import effects_pb2 as fx
from vs_sidecar.server import Wakeup, serve
from vs_sidecar.store import FileStore

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentshim import RawFrame

    from vs_sidecar.driver import Driver

#: How long the worker sleeps between looks at the provider when nothing else wakes it.
IDLE_TICK_SECONDS = 0.25
#: How long the process stays up after the session closes, for the case where no daemon is there to
#: end it: one that is down, or restarting, still finds the close when it reattaches. A running daemon
#: does not wait for this; it ends the sidecar as soon as it has the close ("Reattach" and the
#: release in orchestrator/internal/daemon), so a stopped run leaves no sidecar behind.
LINGER_SECONDS = 60.0
#: How long a stopped sidecar waits for its worker to wind down before it gives up on it.
STOP_SECONDS = 30.0
#: The state directory's names for the process ids the daemon reaps.
PIDFILE = "sidecar.pid"
GROUPS_FILE = "agent.pgids"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """The command line."""
    parser = argparse.ArgumentParser(prog="vs_sidecar", description=__doc__)
    parser.add_argument("--launch", type=Path, required=True, help="LaunchSession as protobuf JSON")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument(
        "--worktree", help="the agent's working directory, when the launch has none"
    )
    parser.add_argument("--mcp-config", type=Path, help="JSON list of stdio MCP servers")
    parser.add_argument("--detach", action="store_true", help="fork into the background first")
    return parser.parse_args(argv)


class LaunchError(ValueError):
    """The launch cannot start a session; the message names what is wrong."""


def load_launch(path: Path, worktree: str | None) -> fx.LaunchSession:
    """The launch spec in *path*, with *worktree* filled in where the file has none.

    Raises ``LaunchError`` for a launch that breaks its contract or has no worktree, from the file
    or ``--worktree``: the agent needs a directory to work in.
    """
    launch = json_format.Parse(path.read_text(), fx.LaunchSession())
    if worktree and not launch.worktree:
        launch.worktree = worktree
    if not launch.worktree:
        msg = (
            "launch.worktree is empty: give the agent's directory in the launch or with --worktree"
        )
        raise LaunchError(msg)
    try:
        protovalidate.Validator().validate(launch, fail_fast=True)
    except protovalidate.ValidationError as error:
        msg = f"launch is invalid: {error}"
        raise LaunchError(msg) from error
    check_plugin(launch)
    return launch


def check_plugin(launch: fx.LaunchSession) -> None:
    """Refuse a launch whose plugin is missing or is not in its marketplace, naming the field.

    A session always loads the plugin and there is no default, so an absent one is an error here,
    before an agent starts, and not a session that quietly runs without the skills.
    """
    if not launch.HasField("plugin"):
        msg = "launch.plugin is missing: every session loads the vibesys plugin, and the daemon sets it"
        raise LaunchError(msg)
    manifest = Path(launch.plugin.marketplace) / ".claude-plugin" / "marketplace.json"
    try:
        data = json.loads(manifest.read_text())
    except OSError as error:
        msg = f"launch.plugin.marketplace {launch.plugin.marketplace} has no readable {manifest.name}: {error}"
        raise LaunchError(msg) from error
    except ValueError as error:
        msg = f"launch.plugin.marketplace: {manifest} is not JSON: {error}"
        raise LaunchError(msg) from error
    entries = data.get("plugins") if isinstance(data, dict) else None
    names = (
        [entry.get("name") for entry in entries if isinstance(entry, dict)]
        if isinstance(entries, list)
        else []
    )
    if launch.plugin.name not in names:
        msg = f"launch.plugin.name {launch.plugin.name!r} is not a plugin of {manifest}"
        raise LaunchError(msg)


def child_pids(proc: Path = Path("/proc")) -> list[int]:
    """The process ids of this process's children, read from *proc* (Linux)."""
    found: list[int] = []
    for children in sorted(proc.glob("self/task/*/children")):
        try:
            found.extend(int(token) for token in children.read_text().split())
        except (OSError, ValueError):
            continue
    return sorted(set(found))


def agent_groups(proc: Path = Path("/proc")) -> list[int]:
    """The process groups led by this process's children: the agent, which starts a session."""
    groups: list[int] = []
    for pid in child_pids(proc):
        try:
            if os.getpgid(pid) == pid:
                groups.append(pid)
        except ProcessLookupError:
            continue
    return groups


def write_atomically(path: Path, text: str) -> None:
    """Replace *path* with *text* so a reader sees the old file or the new one."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text)
    temporary.replace(path)


def load_mcp(path: Path | None) -> list[StdioMcpServer]:
    """The stdio MCP servers in *path*: ``[{"name", "command", "args", "env"}]``."""
    if path is None:
        return []
    return [
        StdioMcpServer(
            name=entry["name"],
            command=entry["command"],
            args=tuple(entry.get("args", ())),
            env=dict(entry.get("env", {})),
        )
        for entry in json.loads(path.read_text())
    ]


def detach(log: Path) -> None:
    """Leave the caller's process group and session, as a daemon does, and log to *log*."""
    if os.fork():
        os._exit(0)
    os.setsid()
    if os.fork():
        os._exit(0)
    log.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


def driver_factory(
    launch: fx.LaunchSession, servers: Sequence[StdioMcpServer]
) -> Callable[[Callable[[RawFrame], None]], Driver]:
    """Drivers over the real provider for *launch*."""
    preferred = None if launch.resume_conversation_id else _conversation_id(launch)

    def make(raw_sink: Callable[[RawFrame], None]) -> Driver:
        return AgentshimDriver(
            lambda conversation_id: build_session(
                launch, mcp_servers=servers, raw_sink=raw_sink, new_conversation_id=conversation_id
            ),
            preferred_id=preferred,
            workdir=launch.worktree,
        )

    return make


def _conversation_id(launch: fx.LaunchSession) -> str:
    """A conversation id derived from the session, so a relaunch of the same session gets the same one."""
    name = f"{launch.run_id}/{launch.session.agent_id}/{launch.session.session}"
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


class AgentGroups:
    """Keeps ``agent.pgids`` naming the process groups of this process's children.

    One thread calls ``record``; ``clear`` runs after that thread has stopped.
    """

    def __init__(self, path: Path, groups: Callable[[], list[int]] = agent_groups) -> None:
        """Write to *path* the groups *groups* finds."""
        self._path = path
        self._groups = groups
        self._written: str | None = None

    def record(self) -> None:
        """Rewrite the file if the groups changed."""
        self._write("".join(f"{pgid}\n" for pgid in self._groups()))

    def clear(self) -> None:
        """Name no group: the agent is closed, so a later reap must kill nothing."""
        self._write("")

    def _write(self, text: str) -> None:
        if text != self._written:
            write_atomically(self._path, text)
            self._written = text


def keep_recording(groups: AgentGroups, done: threading.Event) -> None:
    """Record *groups* every tick until *done*.

    An agent the provider respawns mid-turn is named within a tick of its start.
    """
    groups.record()
    while not done.wait(IDLE_TICK_SECONDS):
        groups.record()


def serve_session(engine: Engine, wakeup: Wakeup, args: argparse.Namespace) -> None:
    """Serve *engine* and run its worker until the session ends or a signal stops it.

    On the way out, whatever the reason (a signal, a failed write, a bug), the agent is closed, so
    it does not outlive the sidecar's own knowledge of it. A failure in the worker is raised here
    after that cleanup.
    """
    groups = AgentGroups(args.state_dir / GROUPS_FILE)
    stop = threading.Event()
    wake = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: (stop.set(), wake.set()))
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

    server = None
    try:
        server = serve(engine, wakeup, args.socket)
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
        if server is not None:
            server.stop(grace=1).wait()
    if failure:
        raise failure[0]


def main(argv: Sequence[str] | None = None) -> int:
    """Run one session. Returns the process exit code."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.detach:
        detach(args.state_dir / "sidecar.log")
    try:
        launch = load_launch(args.launch, args.worktree)
    except LaunchError as error:
        sys.stderr.write(f"vs_sidecar: {error}\n")
        return 2
    args.state_dir.mkdir(parents=True, exist_ok=True)
    write_atomically(args.state_dir / PIDFILE, f"{os.getpid()}\n")
    wakeup = Wakeup()
    engine = Engine(
        launch,
        FileStore.at(args.state_dir),
        driver_factory(launch, load_mcp(args.mcp_config)),
        wakeup.notify,
    )
    serve_session(engine, wakeup, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
