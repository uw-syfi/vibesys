"""``vibesys instances``: list, inspect, and stop detached servers on this node.

The commands read the per-user live registry (``server.registry``). ``--json``
prints one versioned document on stdout. ``inspect`` reads diagnostics only
after explicit ``--include-log`` consent, and the registry bounds the tail.
``stop`` exits 0 when the server stopped or accepted the stop (``stopped``,
``stopping``) and 1 otherwise.

This is a light host command: its module-level imports stay within the
allowlist ``tests/architecture/test_light_entrypoints.py`` enforces, so
neither command imports the run engine. A stop that asks a server over its
control socket speaks only the run protocol's light control envelope.
"""

from __future__ import annotations

import argparse
import os
import sys

from server.host_api import (
    InstanceInspection,
    InstanceList,
    InstanceStopResult,
    LiveInstanceRecord,
    StopOutcome,
)
from server.host_helper_os import ProjectStopIntents
from server.instances import ControlSocketStopRequester
from server.registry import (
    FileInstanceLogs,
    FileInstanceMetadataStore,
    FileInstanceStore,
    InstanceInspector,
    InstanceLogError,
    InstanceMetadataError,
    LiveRegistry,
    StopEffects,
    instance_root,
    parse_instance_id,
)
from vs_project.api import OperatorDialer
from vs_sim.api import OsThreads, PidfdProcessSignaller, SystemClock, UnixNetwork


def _instance_id(value: str) -> str:
    """Parse a registry id, so a typo can never become a path."""
    try:
        return parse_instance_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibesys instances",
        description="List, inspect, and stop detached VibeSys servers running on this node.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list live detached servers")
    listing.add_argument("--json", action="store_true", help="print one JSON document")
    inspect = commands.add_parser("inspect", help="inspect one detached server")
    inspect.add_argument("id", type=_instance_id, help="the server's registry id")
    inspect.add_argument("--json", action="store_true", help="print one JSON document")
    inspect.add_argument(
        "--include-log",
        action="store_true",
        help="include a bounded server-log tail (it may contain sensitive output)",
    )
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
    if not (listing.instances or listing.ended or listing.unreadable or listing.unverified):
        return "No detached VibeSys servers are running on this node."
    lines = [
        f"{record.id}  {_phase(record)}  pid {record.pid}  run {record.run_id}  "
        f"{record.project_root}"
        for record in (*listing.instances, *listing.ended)
    ]
    lines.extend(
        f"{record.id}  unreadable (record version {record.version})"
        for record in listing.unreadable
    )
    lines.extend(
        f"{instance_id}  unverified (its lock could not be read)"
        for instance_id in listing.unverified
    )
    return "\n".join(lines)


def _phase(record: LiveInstanceRecord) -> str:
    phase = record.phase
    detail = phase.stage or (phase.reason.message if phase.reason else None)
    ended = " (ended)" if record.ended_at is not None else ""
    return f"{phase.status}{ended}" + (f": {detail}" if detail else "")


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


def format_inspection(inspection: InstanceInspection) -> str:
    """Render one inspection without loading or inferring any additional data."""
    identity = inspection.metadata or inspection.record
    lines = [f"Instance: {inspection.id}", f"State: {inspection.state.value}"]
    if identity is not None:
        lines.extend(
            (
                f"Hostname: {identity.hostname}",
                f"Run: {identity.run_id}",
                f"Project: {identity.project_root}",
                f"PID: {identity.pid}",
                f"Started: {identity.started_at}",
                f"VibeSys: {identity.vibesys_version}",
                f"Protocol: {identity.protocol_version}",
            )
        )
        task = inspection.metadata.task if inspection.metadata is not None else None
        if task is not None:
            lines.insert(3, f"Task: {task}")
        if identity.vibesys_root is not None:
            lines.append(f"Checkout: {identity.vibesys_root}")
    lines.extend(
        (
            f"Control socket: {inspection.control_socket_path}",
            f"Server log: {inspection.server_log_path}",
        )
    )
    if inspection.log is None:
        lines.append("Diagnostics: not loaded (pass --include-log)")
    elif inspection.log.state == "missing":
        lines.append("Diagnostics: no retained server log")
    else:
        qualification = "truncated" if inspection.log.truncated else "complete"
        lines.append(f"Diagnostics ({qualification} bounded tail):")
        lines.append(inspection.log.text)
    return "\n".join(lines)


def run(
    argv: list[str],
    *,
    registry: LiveRegistry,
    inspector: InstanceInspector,
    effects: StopEffects,
) -> tuple[int, str]:
    """Run one ``vibesys instances`` command; return its exit code and stdout text."""
    args = _parser().parse_args(argv)
    if args.command == "list":
        listing = registry.list()
        return 0, (listing.model_dump_json() if args.json else format_list(listing)) + "\n"
    if args.command == "inspect":
        inspection = inspector.inspect(args.id, include_log=args.include_log)
        return (
            0,
            (inspection.model_dump_json() if args.json else format_inspection(inspection)) + "\n",
        )
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
    store = FileInstanceStore(root)
    # Records carry epoch times, so they age on the epoch clock, not ``threads``' monotonic one.
    epoch = SystemClock()
    try:
        code, output = run(
            sys.argv[1:] if argv is None else argv,
            registry=LiveRegistry(store, epoch),
            inspector=InstanceInspector(
                store,
                FileInstanceLogs(root),
                FileInstanceMetadataStore(root),
                epoch,
            ),
            effects=StopEffects(
                requester=ControlSocketStopRequester(
                    OperatorDialer.for_user(UnixNetwork(), os.environ, os.getuid()),
                    clock=epoch,
                    deadline_clock=threads,
                ),
                signaller=PidfdProcessSignaller(),
                clock=threads,
                pause=threads.sleep,
                intents=ProjectStopIntents(),
            ),
        )
    except (InstanceLogError, InstanceMetadataError) as exc:
        sys.stderr.write(f"vibesys instances: {exc}\n")
        return 1
    sys.stdout.write(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
