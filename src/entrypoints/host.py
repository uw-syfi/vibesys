"""Host commands with contract-only startup; dispatched commands load their I/O adapters.

Each command is a new process, so a command pays for every module its family imports. The
module level holds the command tables and nothing else a command might not use; each family
is a lazy module (``server.lazy_module``) loaded when its command is dispatched, and a
registered read builds only the host facts it asks for (``entrypoints.host_reads``).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from server import lazy_module
from vs_proto.api import host_operations
from vs_sim.api import (
    OsThreads,
    Platform,
    Signals,
    SubprocessPipedProcessLauncher,
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

    from entrypoints import chat_session, host_mutations, host_reads
    from server import host_agent_logins, host_operation_reads
    from vibesys.api import host as host_api
    from vibesys.api import transport as conversion
    from vibesys.api.host_operations import api as login_operations
    from vibesys.api.host_operations import candidate
    from vs_project import api as projects
    from vs_sim.api import SignalRelay, SleepingClock, Threads
else:
    chat_session = lazy_module("entrypoints.chat_session")
    host_mutations = lazy_module("entrypoints.host_mutations")
    host_reads = lazy_module("entrypoints.host_reads")
    host_agent_logins = lazy_module("server.host_agent_logins")
    host_operation_reads = lazy_module("server.host_operation_reads")
    host_api = lazy_module("vibesys.api.host")
    conversion = lazy_module("vibesys.api.transport")
    login_operations = lazy_module("vibesys.api.host_operations.api")
    candidate = lazy_module("vibesys.api.host_operations.candidate")
    projects = lazy_module("vs_project.api")


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
    clock = SystemClock() if clock is None else clock
    root = args.project if args.project.is_absolute() else cwd / args.project
    try:
        project = projects.Project.open(root)
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
    except (OSError, ValueError, TypeError, projects.ProjectError) as exc:
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
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "inspect-candidate":
        parser = argparse.ArgumentParser(
            prog="vibesys host inspect-candidate",
            description="Read a binary ReadRequest from stdin; write a binary ReadResponse to stdout.",
        )
        parser.parse_args(arguments[1:])
        source = sys.stdin.buffer if stdin is None else stdin
        destination = sys.stdout.buffer if stdout is None else stdout
        destination.write(candidate.execute_inspect_candidate(source.read((1 << 20) + 1)))
        return 0
    if arguments and arguments[0] in _READ_COMMANDS:
        parser = argparse.ArgumentParser(
            prog=f"vibesys host {arguments[0]}",
            description="Read a binary ReadRequest from stdin; write a binary ReadResponse to stdout.",
        )
        parser.parse_args(arguments[1:])
        source = sys.stdin.buffer if stdin is None else stdin
        destination = sys.stdout.buffer if stdout is None else stdout
        destination.write(
            host_reads.serve_read(arguments[0], source.read((1 << 20) + 1), platform=runtime)
        )
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
            source.read((1 << 20) + 1), destination, runtime
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
        destination.write(
            host_mutations.serve_mutation(
                _MUTATION_COMMANDS[arguments[0]], source.read((1 << 20) + 1), runtime
            )
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
            host_api.host_call(
                source.read((1 << 20) + 1),
                start_chat=chat_session.start_chat
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


def _serve_saved_runs(payload: bytes, destination: BinaryIO, platform: Platform) -> None:
    """Stream one project's runs: one scan in this process, a frame as each batch is read."""
    facts = host_reads.read_source(platform)

    def write(chunk: bytes) -> None:
        destination.write(chunk)
        # The gateway's progress bound counts what reached it, not what this process buffered.
        destination.flush()

    host_operation_reads.stream_saved_runs(payload, facts, write)


def _serve_login(payload: bytes, destination: BinaryIO, platform: Platform) -> None:
    """Wire the native login watch without constructing unrelated helper resources."""
    threads = platform.threads
    logins = host_agent_logins.host_logins(SubprocessPipedProcessLauncher(), threads)
    watch_login(payload, destination, logins, threads, WakeupFdSignalRelay(threads))


_WATCH_SERVERS: dict[str, Callable[[bytes, BinaryIO, Platform], None]] = {
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
    held: list[LoginHold] = []
    stop = threads.event()

    def start(
        provider: str,
    ) -> login_operations.LoginStartedFact | login_operations.LoginRefusedFact:
        started = logins.hold(provider)
        held.append(started)
        result = started.result
        if isinstance(result, host_agent_logins.LoginStarted):
            return login_operations.LoginStartedFact(result.instruction)
        if not isinstance(result, host_agent_logins.LoginUnavailable):
            message = "login result is neither started nor unavailable"
            raise TypeError(message)
        return login_operations.LoginRefusedFact(
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
        login_operations.serve_login(payload, cli="login", start=start, write=write, wait=wait)


if __name__ == "__main__":
    raise SystemExit(main())
