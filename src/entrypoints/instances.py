"""``vibesys instances``: list and stop the detached servers live on this node.

Both commands read the per-user live registry (``server.instances``). ``--json``
prints one ``InstanceList`` or ``InstanceStopResult`` document on stdout; the
human form prints one line per server. ``stop`` exits 0 when the server stopped
or accepted the stop (``stopped``, ``stopping``) and 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys

from server.instances import (
    ControlSocketStopRequester,
    FileInstanceStore,
    InstanceList,
    InstanceStopResult,
    LiveRegistry,
    StopEffects,
    StopOutcome,
    instance_root,
    parse_instance_id,
)
from vs_sim.api import OsThreads, PidfdProcessSignaller, UnixNetwork


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
    stop.add_argument(
        "--force",
        action="store_true",
        help="skip the control socket and send SIGTERM (interrupts the active agent call)",
    )
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
        case StopOutcome.STOPPING:
            return (
                f"{result.id} accepted the stop and will exit after its active agent call; "
                "check `vibesys instances list`."
            )
        case StopOutcome.NOT_RUNNING:
            return f"No live server has id {result.id}."
        case StopOutcome.STILL_RUNNING:
            return f"Signalled {result.id}, but it is still running; try again shortly."
        case StopOutcome.UNSUPPORTED:
            return (
                f"{result.id} did not answer on its control socket, and this host cannot "
                "signal it without risking a reused pid."
            )


def run(
    argv: list[str],
    *,
    registry: LiveRegistry,
    effects: StopEffects,
) -> tuple[int, str]:
    """Run one ``vibesys instances`` command; return its exit code and stdout text."""
    args = _parser().parse_args(argv)
    if args.command == "list":
        listing = registry.list()
        return 0, (listing.model_dump_json() if args.json else format_list(listing)) + "\n"
    result = registry.stop(args.id, effects, force=args.force)
    code = 0 if result.outcome in {StopOutcome.STOPPED, StopOutcome.STOPPING} else 1
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
        effects=StopEffects(
            requester=ControlSocketStopRequester(UnixNetwork()),
            signaller=PidfdProcessSignaller(),
            clock=threads,
            pause=threads.sleep,
        ),
    )
    sys.stdout.write(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
