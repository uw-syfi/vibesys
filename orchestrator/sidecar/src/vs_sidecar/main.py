"""The sidecar process: ``python -m vs_sidecar --launch FILE --state-dir DIR --socket PATH``.

The daemon writes the ``LaunchSession`` it wants (protobuf JSON) to a file, starts this process in
its own session so it outlives the daemon, and talks to it over the socket. Everything the session
did is in ``--state-dir``, so a restarted sidecar can tell the daemon what happened.
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
#: How long the process stays up after the session closes, so the daemon can read the close.
LINGER_SECONDS = 60.0


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


def load_launch(path: Path, worktree: str | None) -> fx.LaunchSession:
    """The launch spec in *path*, with *worktree* filled in where the file has none."""
    launch = json_format.Parse(path.read_text(), fx.LaunchSession())
    if worktree and not launch.worktree:
        launch.worktree = worktree
    return launch


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
        )

    return make


def _conversation_id(launch: fx.LaunchSession) -> str:
    """A conversation id derived from the session, so a relaunch of the same session gets the same one."""
    name = f"{launch.run_id}/{launch.session.agent_id}/{launch.session.session}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def work(engine: Engine, wakeup: Wakeup, stop: threading.Event) -> None:
    """The worker: run queued turns, poll the provider between them, until closed or stopped."""
    while not stop.is_set() and not engine.finished:
        seen = wakeup.version
        if engine.run_next():
            continue
        engine.poll_idle()
        wakeup.wait(seen, IDLE_TICK_SECONDS)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one session. Returns the process exit code."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.detach:
        detach(args.state_dir / "sidecar.log")
    launch = load_launch(args.launch, args.worktree)
    wakeup = Wakeup()
    engine = Engine(
        launch,
        FileStore(args.state_dir),
        driver_factory(launch, load_mcp(args.mcp_config)),
        wakeup.notify,
    )
    engine.start()
    server = serve(engine, wakeup, args.socket)
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    work(engine, wakeup, stop)
    stop.wait(LINGER_SECONDS if engine.finished else 0)
    server.stop(grace=1).wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
