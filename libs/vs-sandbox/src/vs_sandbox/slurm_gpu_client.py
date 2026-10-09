"""Run one command on Slurm GPUs: ``python -m vs_sandbox.slurm_gpu_client``.

Usage::

    python -m vs_sandbox.slurm_gpu_client [--gpus N] [--time MINUTES] -- COMMAND...

Inside an agent sandbox of the ``slurm-gpu`` run environment, the request goes
to the run's host broker, which runs the command confined in a new job. On the
host itself (the framework's trusted gates), ``--config`` names the operator
file and the command runs directly with ``srun``. The agent sandbox cannot use
that path: it has no Slurm credentials.

Output streams as the job produces it, and the exit status is the command's.
Interrupting the client cancels the job, whether it is queued or running.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sandbox.signal_relay import relay_signals
from vs_sandbox.slurm_gpu import GpuCommand, SlurmGpuLauncher, load_slurm_gpu_config
from vs_sandbox.slurm_gpu_broker import SOCKET_ENV, TOKEN_ENV

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import FrameType

_USAGE_ERROR = 2
_MAX_FRAME_BYTES = 4 * 1024 * 1024
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibesys-gpu",
        description="Run a command on Slurm-allocated GPUs and stream its output.",
    )
    parser.add_argument("--gpus", type=int, default=None, help="GPUs to allocate")
    parser.add_argument(
        "--time", type=int, default=None, dest="time_minutes", help="time limit in minutes"
    )
    parser.add_argument("--config", type=Path, default=None, help=argparse.SUPPRESS)
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


def _shutdown(client: socket.socket) -> None:
    """Close both directions; a second stop signal finds it already closed."""
    with contextlib.suppress(OSError):
        client.shutdown(socket.SHUT_RDWR)


def run_brokered(
    command: Sequence[str],
    *,
    gpus: int | None,
    time_minutes: int | None,
    stop: Stop | None = None,
) -> int:
    """Send one request to the run's host broker and relay its output and exit status."""
    stop = stop or Stop()
    request = {
        "token": os.environ[TOKEN_ENV],
        "argv": list(command),
        "cwd": str(Path.cwd()),
        "gpus": gpus,
        "time_minutes": time_minutes,
        "env": dict(os.environ),
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(os.environ[SOCKET_ENV])
        # The broker cancels the job when the connection closes.
        stop.on_stop = lambda: _shutdown(client)
        client.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        with client.makefile("rb") as frames:
            for line in frames:
                if len(line) > _MAX_FRAME_BYTES:
                    break
                frame = json.loads(line)
                if "output" in frame:
                    _write(base64.b64decode(frame["output"]))
                elif "exit" in frame:
                    return int(frame["exit"])
                elif "error" in frame:
                    _error(str(frame["error"]))
                    return _USAGE_ERROR
    if stop.status is not None:
        return stop.status
    _error("the GPU broker closed the connection")
    return 1


def run_direct(
    config_path: Path,
    command: Sequence[str],
    *,
    gpus: int | None,
    time_minutes: int | None,
    stop: Stop | None = None,
) -> int:
    """Run *command* with ``srun`` from this host, for trusted framework gates."""
    stop = stop or Stop()
    config = load_slurm_gpu_config(config_path)
    request = config.request(gpus, time_minutes)
    status = SlurmGpuLauncher(config).run(
        request,
        GpuCommand(argv=tuple(command), cwd=Path.cwd(), env=dict(os.environ)),
        write=_write,
        cancel=stop.event,
    )
    return stop.status if stop.status is not None else status


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run the command through the broker or ``srun``."""
    args = _parser().parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        _error("missing command after --")
        return _USAGE_ERROR
    stop = Stop()
    try:
        with relay_signals(_STOP_SIGNALS, lambda number: stop.handle(number, None)):
            return _run(args, command, stop)
    except ValueError as error:
        _error(str(error))
        return _USAGE_ERROR


def _run(args: argparse.Namespace, command: Sequence[str], stop: Stop) -> int:
    limits = {"gpus": args.gpus, "time_minutes": args.time_minutes, "stop": stop}
    if os.environ.get(SOCKET_ENV) and os.environ.get(TOKEN_ENV):
        return run_brokered(command, **limits)
    if args.config is not None:
        return run_direct(args.config, command, **limits)
    _error(f"{SOCKET_ENV} is not set; no GPU broker is available")
    return _USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
