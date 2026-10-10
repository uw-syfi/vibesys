"""Run one command on Slurm GPUs, or one trusted gate, through the run's host broker.

Usage::

    vibesys-gpu [--gpus N] [--time MINUTES] -- COMMAND...
    vibesys-gpu --gate accuracy|benchmark [ARGUMENT...]

The agent's sandbox cannot reach the Slurm controller, so the request goes to
the run's host-owned broker over a Unix socket (:data:`SOCKET_ENV`) with a
per-run token (:data:`TOKEN_ENV`). The first form runs COMMAND confined in a
new job. The second runs the framework's planned accuracy or benchmark gate;
the broker accepts only the arguments the plan allows, and relays the result
file a benchmark writes under ``/tmp`` back to this side of the socket.

Output streams as the job produces it, and the exit status is the command's.
Interrupting the client cancels the job, whether it is queued or running.

This module imports only the standard library on purpose. In a Docker agent
sandbox it runs as a single file copied to a mounted path, with whatever
``python3`` the agent image has and none of VibeSys's packages.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from types import FrameType

#: The variable naming the broker's Unix socket.
SOCKET_ENV = "VIBESYS_COMMAND_BROKER_SOCKET"
#: The variable carrying the per-run token; the name is public, the value is not.
TOKEN_ENV = "VIBESYS_COMMAND_BROKER_TOKEN"  # noqa: S105  # lint-waiver: LW-610011 [S105]; this is the name of the variable carrying the token, not a secret.
# > Renaming the constant to dodge the heuristic would hide what it names; the
# > value is an environment variable name with no credential in it.

_USAGE_ERROR = 2
_RECV_BYTES = 65_536
_MAX_FRAME_BYTES = 16 * 1024 * 1024
_GATE_KINDS = ("accuracy", "benchmark")
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_CONNECTION_CLOSED = "the command broker closed the connection"


class Wire(Protocol):
    """One open connection to the broker.

    This module cannot import VibeSys packages, so the interface is declared here;
    ``vs_sim.api.Connection`` satisfies it, which is how tests run the client against
    a simulated broker.
    """

    def send(self, data: bytes) -> None:
        """Write all of *data*; ``BrokenPipeError`` when the broker has closed its end."""
        ...

    def recv(self, max_bytes: int) -> bytes:
        """Up to *max_bytes* bytes, at least one; ``b""`` once the broker closed and all was read."""
        ...

    def close(self) -> None:
        """Close the connection; a read blocked in another thread or handler then ends."""
        ...


class _SocketWire:
    def __init__(self, path: str) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.connect(path)
        except OSError:
            self._sock.close()
            raise

    def send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def recv(self, max_bytes: int) -> bytes:
        return self._sock.recv(max_bytes)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            # Ends a read in progress on this socket, then releases it.
            self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()


def _lines(wire: Wire) -> Iterator[bytes]:
    """Yield each newline-terminated line the broker sends, then a final unterminated one."""
    buffer = b""
    while chunk := wire.recv(_RECV_BYTES):
        buffer += chunk
        while (end := buffer.find(b"\n") + 1) > 0:
            yield buffer[:end]
            buffer = buffer[end:]
    if buffer:
        yield buffer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibesys-gpu",
        description="Run a command on Slurm-allocated GPUs and stream its output.",
    )
    parser.add_argument("--gpus", type=int, default=None, help="GPUs to allocate")
    parser.add_argument(
        "--time", type=int, default=None, dest="time_minutes", help="time limit in minutes"
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _error(message: str) -> None:
    sys.stderr.write(f"vibesys-gpu: {message}\n")


def _write(chunk: bytes) -> None:
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()


class Stop:
    """The stop signal a client received, shared with the work it interrupts.

    :func:`main` routes the process's termination signals here; a run reacts
    through :attr:`event` or the :attr:`on_stop` callback it registers.
    """

    def __init__(self) -> None:
        """Start with no signal received."""
        self.event = threading.Event()
        self.signum: int | None = None
        self.on_stop: Callable[[], None] | None = None

    def handle(self, signum: int, _frame: FrameType | None) -> None:
        """Record *signum* and stop the run."""
        self.signum = signum
        self.event.set()
        if self.on_stop is not None:
            self.on_stop()

    @property
    def status(self) -> int | None:
        """Return the shell-style status for the received signal, if any."""
        return None if self.signum is None else 128 + self.signum


class MalformedFileError(ValueError):
    """The broker relayed a result file this client cannot decode."""

    def __init__(self) -> None:
        """Describe the malformed file."""
        super().__init__("the command broker sent a malformed file")


def _deliver_file(frame: dict[str, object]) -> None:
    """Write the result file the broker relays, atomically, at the path it names."""
    path = frame.get("path")
    data = frame.get("data")
    if not isinstance(path, str) or not isinstance(data, str):
        raise MalformedFileError
    try:
        content = base64.b64decode(data, validate=True)
    except binascii.Error as error:
        raise MalformedFileError from error
    target = Path(path)
    pending = target.with_name(f".{target.name}.{os.getpid()}.part")
    pending.write_bytes(content)
    pending.replace(target)


def _relay(wire: Wire, call: dict[str, object]) -> int | None:
    """Send *call* and relay what comes back; the exit status, or ``None`` if the broker hung up."""
    # The broker may drop the connection before it reads the request: a send
    # then fails with a broken pipe, the same dropped connection as a close
    # after the send, so it takes the same exit.
    wire.send(json.dumps(call, separators=(",", ":")).encode() + b"\n")
    for line in _lines(wire):
        if len(line) > _MAX_FRAME_BYTES:
            return None
        frame = json.loads(line)
        if "output" in frame:
            _write(base64.b64decode(frame["output"]))
        elif "file" in frame:
            _deliver_file(frame["file"])
        elif "exit" in frame:
            return int(frame["exit"])
        elif "error" in frame:
            _error(str(frame["error"]))
            return _USAGE_ERROR
    return None


def _request(call: dict[str, object], *, stop: Stop, dial: Callable[[str], Wire]) -> int:
    """Send *call* to the run's broker and relay its output, files, and exit status."""
    call = {"token": os.environ[TOKEN_ENV], "cwd": str(Path.cwd()), **call}
    wire = dial(os.environ[SOCKET_ENV])
    try:
        # The broker cancels the job when the connection closes.
        stop.on_stop = wire.close
        try:
            status = _relay(wire, call)
        except ConnectionError:
            # A reset or broken pipe is a dropped connection, like a clean close
            # without an exit status.
            status = None
        except OSError:
            # A read cut short by this client's own stop (the connection was closed
            # under it) is the stop, not a failure to report.
            if stop.status is None:
                raise
            status = None
    finally:
        wire.close()
    if status is not None:
        return status
    if stop.status is not None:
        return stop.status
    _error(_CONNECTION_CLOSED)
    return 1


def run_brokered(
    command: Sequence[str],
    *,
    gpus: int | None,
    time_minutes: int | None,
    stop: Stop | None = None,
    dial: Callable[[str], Wire] = _SocketWire,
) -> int:
    """Run *command* in a new GPU job through the broker; return its exit status."""
    return _request(
        {
            "op": "gpu",
            "argv": list(command),
            "gpus": gpus,
            "time_minutes": time_minutes,
            "env": dict(os.environ),
        },
        stop=stop or Stop(),
        dial=dial,
    )


def run_gate(
    kind: str,
    arguments: Sequence[str],
    *,
    stop: Stop | None = None,
    dial: Callable[[str], Wire] = _SocketWire,
) -> int:
    """Run the planned *kind* gate through the broker; return its exit status."""
    return _request(
        {"op": "gate", "kind": kind, "arguments": list(arguments)},
        stop=stop or Stop(),
        dial=dial,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run the command or gate through the broker."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    stop = Stop()
    previous = {signum: signal.signal(signum, stop.handle) for signum in _STOP_SIGNALS}
    try:
        return execute(arguments, stop=stop)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def execute(arguments: list[str], *, stop: Stop, dial: Callable[[str], Wire] = _SocketWire) -> int:
    """Run the command or gate *arguments* describe; the status :func:`main` exits with.

    Everything :func:`main` does except route the process's signals to *stop*.
    """
    if not os.environ.get(SOCKET_ENV) or not os.environ.get(TOKEN_ENV):
        _error(f"{SOCKET_ENV} is not set; no command broker is available")
        return _USAGE_ERROR
    try:
        if arguments[:1] == ["--gate"]:
            return _gate(arguments[1:], stop, dial)
        return _gpu(arguments, stop, dial)
    except (ValueError, OSError) as error:
        _error(str(error))
        return _USAGE_ERROR


def _gate(arguments: list[str], stop: Stop, dial: Callable[[str], Wire]) -> int:
    kind, rest = (arguments[0], arguments[1:]) if arguments else ("", [])
    if kind not in _GATE_KINDS:
        _error(f"--gate takes one of: {', '.join(_GATE_KINDS)}")
        return _USAGE_ERROR
    return run_gate(kind, rest[1:] if rest[:1] == ["--"] else rest, stop=stop, dial=dial)


def _gpu(arguments: list[str], stop: Stop, dial: Callable[[str], Wire]) -> int:
    args = _parser().parse_args(arguments)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        _error("missing command after --")
        return _USAGE_ERROR
    return run_brokered(
        command, gpus=args.gpus, time_minutes=args.time_minutes, stop=stop, dial=dial
    )


if __name__ == "__main__":
    sys.exit(main())
