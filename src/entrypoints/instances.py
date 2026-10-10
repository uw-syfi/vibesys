"""``vibesys instances``: list and stop the detached servers live on this node.

Both commands read the per-user live registry (``server.instances``). ``--json``
prints one ``InstanceList`` or ``InstanceStopResult`` document on stdout; the
human form prints one line per server.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING

from server.instances import (
    FileInstanceStore,
    InstanceList,
    InstanceStopResult,
    LiveRegistry,
    StopOutcome,
    instance_root,
    parse_instance_id,
)
from vs_sim.api import OsThreads, PidfdProcessSignaller

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Clock, ProcessSignaller


def _instance_id(value: str) -> str:
    """Parse a registry id, so a typo can never become a path."""
    try:
        return parse_instance_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibesys instances",
        description="List and stop detached VibeSys servers running on this node.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list live detached servers")
    listing.add_argument("--json", action="store_true", help="print one JSON document")
    stop = commands.add_parser("stop", help="stop one live detached server")
    stop.add_argument("id", type=_instance_id, help="the server's registry id")
    stop.add_argument("--json", action="store_true", help="print one JSON document")
    return parser


def format_list(listing: InstanceList) -> str:
    """Render a listing for a terminal, one server per line."""
    if not listing.instances and not listing.unverified:
        return "No detached VibeSys servers are running on this node."
    lines = [
        f"{record.id}  {record.status}  pid {record.pid}  "
        f"run {record.run_id or '-'}  {record.project_root}"
        for record in listing.instances
    ]
    lines.extend(
        f"{instance_id}  unverified (its lock could not be read)"
        for instance_id in listing.unverified
    )
    return "\n".join(lines)


def format_stop(result: InstanceStopResult) -> str:
    """Render a stop outcome for a terminal."""
    match result.outcome:
        case StopOutcome.STOPPED:
            return f"Stopped {result.id}."
        case StopOutcome.NOT_RUNNING:
            return f"No live server has id {result.id}."
        case StopOutcome.STILL_RUNNING:
            return f"Signalled {result.id}, but it is still running; try again shortly."
        case StopOutcome.UNSUPPORTED:
            return (
                f"This host cannot signal {result.id} without risking a reused pid; "
                "stop it from its own client instead."
            )


def run(
    argv: list[str],
    *,
    registry: LiveRegistry,
    signaller: ProcessSignaller,
    clock: Clock,
    pause: Callable[[float], None],
) -> tuple[int, str]:
    """Run one ``vibesys instances`` command; return its exit code and stdout text."""
    args = _parser().parse_args(argv)
    if args.command == "list":
        listing = registry.list()
        return 0, (listing.model_dump_json() if args.json else format_list(listing)) + "\n"
    result = registry.stop(args.id, signaller=signaller, clock=clock, pause=pause)
    code = 0 if result.outcome is StopOutcome.STOPPED else 1
    return code, (result.model_dump_json() if args.json else format_stop(result)) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Run ``vibesys instances`` against this node's registry."""
    threads = OsThreads()
    try:
        root = instance_root(os.environ, os.getuid())
    except PermissionError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    code, output = run(
        sys.argv[1:] if argv is None else argv,
        registry=LiveRegistry(FileInstanceStore(root)),
        signaller=PidfdProcessSignaller(),
        clock=threads,
        pause=threads.sleep,
    )
    sys.stdout.write(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
