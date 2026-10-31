"""Host commands with contract-only startup; dispatched commands load their I/O adapters."""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vibesys.api.host import host_call, terminate_host
from vibesys.api.host_operations.api import (
    ClusterRefusedError,
    LoginRefusedFact,
    LoginStartedFact,
    PricesRefusedError,
    PricesUnknownError,
    ProjectRefusedError,
    StartRefusedError,
    execute_inspect_candidate,
    serve_cluster,
    serve_login,
    serve_project,
    serve_start,
    serve_usage_prices,
)
from vs_project.api import Project, ProjectError
from vs_proto.api import host_operations
from vs_proto.api import host_payloads as pb
from vs_sim.api import (
    OsThreads,
    Platform,
    Signals,
    SystemClock,
    WakeupFdSignalRelay,
)

_HOST_CALL_COMMANDS = {op.cli: op.slot for op in host_operations.HOST_CALLS}
_READ_COMMANDS = {op.cli: op.slot for op in host_operations.READ_CALLS}
_WATCH_COMMANDS = {slot.replace("_", "-"): slot for slot in host_operations.WATCHES}
_MUTATION_COMMANDS = {op.cli: op.slot for op in host_operations.MUTATION_CALLS}


class LoginHold(Protocol):
    """A started login the watch holds until cancel or completion."""

    @property
    def result(self) -> object:
        """The login's first answer."""

    def cancel(self) -> None:
        """Stop the login process group."""

    def wait(self) -> None:
        """Block until the login process has been reaped."""


class LoginLibrary(Protocol):
    """The host's login library, the part the watch command uses."""

    def hold(self, provider: str) -> LoginHold:
        """Start ``provider``'s login and keep its process group."""


if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO

    from vs_sim.api import Processes, SignalRelay, SleepingClock, Threads


def run(argv: list[str], *, cwd: Path, clock: SleepingClock | None = None) -> tuple[int, str, str]:
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
    bulk.add_argument(
        "--reconvert",
        action="store_true",
        help="rebuild runs this migration already converted, from their untouched journals",
    )
    args = parser.parse_args(argv)
    conversion = import_module("vibesys.api.transport")
    clock = SystemClock() if clock is None else clock
    root = args.project if args.project.is_absolute() else cwd / args.project
    try:
        project = Project.open(root)
        if args.method == "convert-runs":
            rows = conversion.convert_runs(
                project, clock=clock, threads=OsThreads(), reconvert=args.reconvert
            )
            counts = Counter(row.result.status for row in rows)
            output = "".join(
                f"{row.run}: {row.result.status}: {row.result.reason}\n" for row in rows
            )
            summary = ", ".join(
                f"{status}={counts[status]}" for status in conversion.ConversionStatus
            )
            return int(counts[conversion.ConversionStatus.FAILED] > 0), output + summary + "\n", ""
        result = conversion.convert_run(project, args.run, clock=clock, threads=OsThreads())
    except (OSError, ValueError, TypeError, ProjectError) as exc:
        return 1, "", f"vibesys host {args.method}: {exc}\n"
    return 0, f"{result.status}: {result.reason}\n", ""


def main(
    argv: list[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    platform: Platform | None = None,
) -> int:
    """Run one host method from the current directory."""
    runtime = Platform.real() if platform is None else platform
    threads = runtime.threads
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
        host = import_module("entrypoints.host_helper").real_host(os.environ, os.getuid(), threads)
        facts = import_module("server.host_helper_requests").registry_read_source(
            host, https=import_module("vs_sim.api").UrllibHttpsGet()
        )
        reply = import_module("server.host_operation_reads").answer_registered_read
        destination.write(reply(arguments[0], source.read((1 << 20) + 1), facts))
        return 0
    if arguments and arguments[0] in _WATCH_COMMANDS:
        parser = argparse.ArgumentParser(
            prog=f"vibesys host {arguments[0]}",
            description="Read a binary WatchRequest from stdin; write framed WatchResponses to stdout.",
        )
        parser.parse_args(arguments[1:])
        source = sys.stdin.buffer if stdin is None else stdin
        destination = sys.stdout.buffer if stdout is None else stdout
        _WATCH_SERVERS[_WATCH_COMMANDS[arguments[0]]](
            source.read((1 << 20) + 1), destination, threads
        )
        return 0
    if arguments and arguments[0] in _MUTATION_COMMANDS:
        parser = argparse.ArgumentParser(
            prog=f"vibesys host {arguments[0]}",
            description="Read a binary MutateRequest from stdin; write a binary MutateResponse to stdout.",
        )
        parser.parse_args(arguments[1:])
        source = sys.stdin.buffer if stdin is None else stdin
        destination = sys.stdout.buffer if stdout is None else stdout
        owned = threads
        destination.write(
            terminate_host(
                source.read((1 << 20) + 1),
                platform=runtime,
            )
            if _MUTATION_COMMANDS[arguments[0]] == "terminate_run"
            else _serve_mutation(arguments[0], source.read((1 << 20) + 1), owned, runtime.processes)
        )
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
                platform=runtime,
            )
        )
        return 0
    code, output, error = run(arguments, cwd=Path.cwd(), clock=runtime.clock)
    sys.stdout.write(output)
    sys.stderr.write(error)
    return code


def _serve_saved_runs(payload: bytes, destination: BinaryIO, threads: Threads) -> None:
    """Stream one project's runs: one scan in this process, a frame as each batch is read."""
    host = import_module("entrypoints.host_helper").real_host(os.environ, os.getuid(), threads)
    facts = import_module("server.host_helper_requests").registry_read_source(host)

    def write(chunk: bytes) -> None:
        destination.write(chunk)
        # The gateway's progress bound counts what reached it, not what this process buffered.
        destination.flush()

    import_module("server.host_operation_reads").stream_saved_runs(payload, facts, write)


def _serve_login(payload: bytes, destination: BinaryIO, threads: Threads) -> None:
    """Wire the native login watch without constructing unrelated helper resources."""
    launcher = import_module("vs_sim.api").SubprocessPipedProcessLauncher()
    logins = import_module("server.host_agent_logins").host_logins(launcher, threads)
    watch_login(payload, destination, logins, threads, WakeupFdSignalRelay(threads))


_WATCH_SERVERS: dict[str, Callable[[bytes, BinaryIO, Threads], None]] = {
    "login": _serve_login,
    "stream_saved_runs": _serve_saved_runs,
}
"""What serves each registered watch, by slot: a watch with no entry has no command to run."""


def watch_login(
    payload: bytes,
    destination: BinaryIO,
    logins: LoginLibrary,
    threads: Threads,
    signals: SignalRelay,
) -> None:
    """Start one login and hold this process until it ends or this process is signalled."""
    models = import_module("server.host_agent_logins")
    held: list[LoginHold] = []
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
        # A watch stays alive after its first frame; a pipe cannot wait for process exit.
        destination.flush()

    with signals.relay([Signals.SIGTERM], lambda _number: stop.set()):
        serve_login(payload, cli="login", start=start, write=write, wait=wait)


def _serve_mutation(cli: str, payload: bytes, threads: Threads, processes: Processes) -> bytes:
    """Dispatch one mutation command. Cluster and project changes are not starts."""
    slot = _MUTATION_COMMANDS[cli]
    if slot == "start_run":
        return _serve_start(payload, threads)
    if slot in {"put_cluster", "remove_cluster", "test_cluster"}:
        return _serve_cluster(payload, processes)
    if slot == "usage_prices_save":
        return _serve_usage_prices(payload)
    return _serve_project(payload, threads)


def _serve_usage_prices(payload: bytes) -> bytes:
    """Admit one price save, then apply it. Replay does not apply it again."""
    scan = import_module("server.usage_scan")
    pricing = import_module("server.usage_pricing")

    def apply(request: pb.UsagePricesSaveRequest) -> pb.UsagePricesSaveResponse:
        try:
            return scan.save_prices(scan.usage_directory(), request)
        except pricing.PriceBookError as exc:
            raise PricesRefusedError(str(exc)) from exc
        except OSError as exc:
            raise PricesUnknownError from exc

    return serve_usage_prices(
        payload, store=Project.admissions(), host_user=str(os.getuid()), apply=apply
    )


def _serve_project(payload: bytes, threads: Threads) -> bytes:
    """Admit one project change, then apply it. Replay does not apply it again."""
    requests = import_module("server.host_helper_requests")
    refused = import_module("server.host_examples").ProjectLookupError
    host = import_module("entrypoints.host_helper").real_host(os.environ, os.getuid(), threads)

    def apply(slot: str, request: object) -> object:
        if not isinstance(request, pb.CreateExampleRequest | pb.CopyProjectRequest):
            message = "project request is not a project mutation"
            raise ProjectRefusedError(message)
        try:
            return requests.apply_registered_project(slot, request, host)
        except (refused, TypeError, ValueError) as exc:
            raise ProjectRefusedError(str(exc)) from exc

    return serve_project(
        payload,
        store=Project.admissions(),
        host_user=str(os.getuid()),
        apply=apply,
    )


def _serve_cluster(payload: bytes, processes: Processes) -> bytes:
    """Admit one cluster change, then apply it. Replay does not apply it again."""
    models = import_module("server.host_compute")
    refused = models.ComputeError
    adapters = import_module("server.host_compute_runtime")
    probe = import_module("vs_sim.api").ProcessesProbe(processes)
    compute = adapters.MachineCompute(
        adapters.OsConfigFiles(), probe, home=str(Path.home()), environ=os.environ
    )

    def apply(slot: str, request: object) -> object:
        if not isinstance(
            request, pb.PutClusterRequest | pb.RemoveClusterRequest | pb.TestClusterRequest
        ):
            message = "cluster request is not a cluster mutation"
            raise ClusterRefusedError(message)
        try:
            return models.apply_registered_cluster(slot, request, compute)
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
    refused = import_module("server.host_examples").ProjectLookupError
    models = import_module("server.host_api")

    def launch(request: pb.StartRunRequest) -> tuple[str, str]:
        try:
            body = requests.registry_start_request(request)
        except (TypeError, ValueError) as exc:
            raise StartRefusedError(str(exc)) from exc
        host = import_module("entrypoints.host_helper").real_host(os.environ, os.getuid(), threads)
        try:
            reply = requests.launch_registered_run(body, host)
        except refused as exc:
            raise StartRefusedError(str(exc)) from exc
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
