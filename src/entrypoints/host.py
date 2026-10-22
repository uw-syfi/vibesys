"""Host-side maintenance commands, dispatched before starting a run engine."""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vibesys.api.host import host_call
from vibesys.api.host_operations.api import (
    ClusterRefusedError,
    LoginRefusedFact,
    LoginStartedFact,
    ProjectRefusedError,
    StartRefusedError,
    execute_inspect_candidate,
    serve_cluster,
    serve_login,
    serve_project,
    serve_start,
)
from vs_project.api import Project, ProjectError
from vs_proto.api import host_operations
from vs_proto.api import host_payloads as pb
from vs_sim.api import OsProcesses, OsThreads, Signals, SystemClock, WakeupFdSignalRelay

_HOST_CALL_COMMANDS = {op.cli: op.slot for op in host_operations.HOST_CALLS}
_READ_COMMANDS = {op.cli: op.slot for op in host_operations.READ_CALLS}
_WATCH_COMMANDS = {slot.replace("_", "-"): slot for slot in host_operations.WATCHES}
_MUTATION_COMMANDS = {op.cli: op.slot for op in host_operations.MUTATION_CALLS}


class _LoginHold(Protocol):
    """A started login the watch holds until cancel or completion."""

    @property
    def result(self) -> object:
        """The login's first answer."""

    def cancel(self) -> None:
        """Stop the login process group."""

    def wait(self) -> None:
        """Block until the login process has been reaped."""


class _LoginLibrary(Protocol):
    """The host's login library, the part the watch command uses."""

    def hold(self, provider: str) -> _LoginHold:
        """Start ``provider``'s login and keep its process group."""


if TYPE_CHECKING:
    from typing import BinaryIO

    from vs_sim.api import Processes, Threads


def run(argv: list[str], *, cwd: Path) -> tuple[int, str, str]:
    """Run a host maintenance method and return exit code, stdout and stderr."""
    parser = argparse.ArgumentParser(
        prog="vibesys host",
        epilog=(
            ", ".join(_HOST_CALL_COMMANDS)
            + " read a HostCallRequest from stdin and write a HostCallResponse to stdout."
        ),
    )
    methods = parser.add_subparsers(dest="method", required=True)
    convert = methods.add_parser("convert-run", help="convert a stopped run's JSONL journal")
    convert.add_argument("run", help="run id")
    convert.add_argument("--project", type=Path, default=cwd, help="project directory")
    bulk = methods.add_parser("convert-runs", help="convert stopped project and host-index runs")
    bulk.add_argument("--project", type=Path, default=cwd, help="project directory")
    args = parser.parse_args(argv)
    conversion = import_module("vibesys.api.transport")
    root = args.project if args.project.is_absolute() else cwd / args.project
    try:
        project = Project.open(root)
        if args.method == "convert-runs":
            rows = conversion.convert_runs(project, clock=SystemClock(), threads=OsThreads())
            counts = Counter(row.result.status for row in rows)
            output = "".join(
                f"{row.run}: {row.result.status}: {row.result.reason}\n" for row in rows
            )
            summary = ", ".join(
                f"{status}={counts[status]}" for status in conversion.ConversionStatus
            )
            return int(counts[conversion.ConversionStatus.FAILED] > 0), output + summary + "\n", ""
        result = conversion.convert_run(project, args.run, clock=SystemClock(), threads=OsThreads())
    except (OSError, ValueError, TypeError, ProjectError) as exc:
        return 1, "", f"vibesys host {args.method}: {exc}\n"
    return 0, f"{result.status}: {result.reason}\n", ""


def main(
    argv: list[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    processes: Processes | None = None,
    threads: Threads | None = None,
) -> int:
    """Run one host method from the current directory."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "inspect-candidate":
        parser = argparse.ArgumentParser(
            prog="vibesys host inspect-candidate",
            description="Read a binary ReadRequest from stdin; write a binary ReadResponse to stdout.",
        )
        parser.parse_args(arguments[1:])
        source = sys.stdin.buffer if stdin is None else stdin
        destination = sys.stdout.buffer if stdout is None else stdout
        destination.write(execute_inspect_candidate(source.read((1 << 20) + 1)))
        return 0
    if arguments and arguments[0] in _READ_COMMANDS:
        parser = argparse.ArgumentParser(
            prog=f"vibesys host {arguments[0]}",
            description="Read a binary ReadRequest from stdin; write a binary ReadResponse to stdout.",
        )
        parser.parse_args(arguments[1:])
        source = sys.stdin.buffer if stdin is None else stdin
        destination = sys.stdout.buffer if stdout is None else stdout
        host = import_module("entrypoints.host_helper").real_host(
            os.environ, os.getuid(), OsThreads() if threads is None else threads
        )
        reply = import_module("server.host_helper_requests").answer_registered_read
        destination.write(reply(arguments[0], source.read((1 << 20) + 1), host))
        return 0
    if arguments and arguments[0] in _WATCH_COMMANDS:
        parser = argparse.ArgumentParser(
            prog=f"vibesys host {arguments[0]}",
            description="Read a binary WatchRequest from stdin; write framed WatchResponses to stdout.",
        )
        parser.parse_args(arguments[1:])
        source = sys.stdin.buffer if stdin is None else stdin
        destination = sys.stdout.buffer if stdout is None else stdout
        owned = OsThreads() if threads is None else threads
        host = import_module("entrypoints.host_helper").real_host(os.environ, os.getuid(), owned)
        library = import_module("entrypoints.host_helper").AgentLibraryLogins
        if not isinstance(host.logins, library):
            message = "host logins cannot be held open"
            raise TypeError(message)
        _serve_login(arguments[0], source.read((1 << 20) + 1), destination, host.logins, owned)
        return 0
    if arguments and arguments[0] in _MUTATION_COMMANDS:
        parser = argparse.ArgumentParser(
            prog=f"vibesys host {arguments[0]}",
            description="Read a binary MutateRequest from stdin; write a binary MutateResponse to stdout.",
        )
        parser.parse_args(arguments[1:])
        source = sys.stdin.buffer if stdin is None else stdin
        destination = sys.stdout.buffer if stdout is None else stdout
        owned = OsThreads() if threads is None else threads
        destination.write(_serve_mutation(arguments[0], source.read((1 << 20) + 1), owned))
        return 0
    if arguments and arguments[0] in _HOST_CALL_COMMANDS:
        parser = argparse.ArgumentParser(
            prog=f"vibesys host {arguments[0]}",
            description="Read a binary HostCallRequest from stdin; write a binary HostCallResponse to stdout.",
        )
        parser.parse_args(arguments[1:])
        source = sys.stdin.buffer if stdin is None else stdin
        destination = sys.stdout.buffer if stdout is None else stdout
        destination.write(
            host_call(
                source.read((1 << 20) + 1),
                start_chat=import_module("entrypoints.chat_session").start_chat
                if _HOST_CALL_COMMANDS[arguments[0]] == "chat_message"
                else None,
                processes=OsProcesses() if processes is None else processes,
                threads=OsThreads() if threads is None else threads,
            )
        )
        return 0
    code, output, error = run(arguments, cwd=Path.cwd())
    sys.stdout.write(output)
    sys.stderr.write(error)
    return code


def _serve_login(
    cli: str, payload: bytes, destination: BinaryIO, logins: _LoginLibrary, threads: Threads
) -> None:
    """Start one login and hold this process until it ends or this process is signalled."""
    models = import_module("server.host_agent_logins")
    held: list[_LoginHold] = []
    stop = threads.event()

    def start(provider: str) -> LoginStartedFact | LoginRefusedFact:
        started = logins.hold(provider)
        held.append(started)
        result = started.result
        if isinstance(result, models.LoginStarted):
            return LoginStartedFact(result.instruction)
        if not isinstance(result, models.LoginUnavailable):
            message = "login result is neither started nor unavailable"
            raise TypeError(message)
        return LoginRefusedFact(
            reason=result.reason.value,
            command=result.command,
            detail=result.detail or "",
        )

    def cancel() -> None:
        stop.wait()
        if held:
            held[0].cancel()

    threads.spawn(cancel, name="login-cancel")

    def wait() -> None:
        if held:
            held[0].wait()

    def write(chunk: bytes) -> None:
        destination.write(chunk)

    with WakeupFdSignalRelay(threads).relay([Signals.SIGTERM], lambda _number: stop.set()):
        serve_login(payload, cli=cli, start=start, write=write, wait=wait)


def _serve_mutation(cli: str, payload: bytes, threads: Threads) -> bytes:
    """Dispatch one mutation command. Cluster and project changes are not starts."""
    slot = _MUTATION_COMMANDS[cli]
    if slot == "start_run":
        return _serve_start(payload, threads)
    if slot in {"put_cluster", "remove_cluster", "test_cluster"}:
        return _serve_cluster(payload, threads)
    return _serve_project(payload, threads)


def _serve_project(payload: bytes, threads: Threads) -> bytes:
    """Admit one project change, then apply it. Replay does not apply it again."""
    requests = import_module("server.host_helper_requests")
    host = import_module("entrypoints.host_helper").real_host(os.environ, os.getuid(), threads)

    def apply(slot: str, request: object) -> object:
        if not isinstance(request, pb.CreateExampleRequest | pb.CopyProjectRequest):
            message = "project request is not a project mutation"
            raise ProjectRefusedError(message)
        try:
            return requests.apply_registered_project(slot, request, host)
        except (requests.ProjectLookupError, TypeError, ValueError) as exc:
            raise ProjectRefusedError(str(exc)) from exc

    return serve_project(
        payload,
        store=Project.admissions(),
        host_user=str(os.getuid()),
        apply=apply,
    )


def _serve_cluster(payload: bytes, threads: Threads) -> bytes:
    """Admit one cluster change, then apply it. Replay does not apply it again."""
    requests = import_module("server.host_helper_requests")
    refused = import_module("server.host_helper_compute").ComputeError
    host = import_module("entrypoints.host_helper").real_host(os.environ, os.getuid(), threads)

    def apply(slot: str, request: object) -> object:
        if not isinstance(
            request, pb.PutClusterRequest | pb.RemoveClusterRequest | pb.TestClusterRequest
        ):
            message = "cluster request is not a cluster mutation"
            raise ClusterRefusedError(message)
        try:
            return requests.apply_registered_cluster(slot, request, host)
        except (refused, TypeError, ValueError) as exc:
            raise ClusterRefusedError(str(exc)) from exc

    return serve_cluster(
        payload,
        store=Project.admissions(),
        host_user=str(os.getuid()),
        apply=apply,
    )


def _serve_start(payload: bytes, threads: Threads) -> bytes:
    """Admit the start, then launch through the helper. Replay does not launch."""
    requests = import_module("server.host_helper_requests")
    models = import_module("server.host_api")

    def launch(request: pb.StartRunRequest) -> tuple[str, str]:
        try:
            body = requests.registry_start_request(request)
        except (TypeError, ValueError) as exc:
            raise StartRefusedError(str(exc)) from exc
        host = import_module("entrypoints.host_helper").real_host(os.environ, os.getuid(), threads)
        reply = requests.launch_registered_run(body, host)
        if isinstance(reply, models.HelperError):
            raise StartRefusedError(reply.message)
        started = reply.launch
        if isinstance(started, models.DetachedLaunchFailure):
            raise StartRefusedError(started.message)
        return started.run_id, started.id

    return serve_start(
        payload,
        store=Project.admissions(),
        launch=launch,
        host_user=str(os.getuid()),
    )


if __name__ == "__main__":
    raise SystemExit(main())
