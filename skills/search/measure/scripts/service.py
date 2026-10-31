#!/usr/bin/env python3
"""Run a service and a client under one bounded POSIX process-group owner.

Public API: serve(service, probe, client, host, timeout=30). Readiness exhaustion
raises ValueError; cleanup runs on every exit. CLI accepts JSON argv arrays, never a shell.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys

# LW-100351 [TID251]; standalone bounded waits; vs_sim adds a dependency, busy spinning wastes CPU.
import time  # noqa: TID251
from enum import Enum
from typing import Protocol

__all__ = ["Host", "PosixHost", "Reason", "ServiceError", "serve"]


class Reason(str, Enum):
    """Closed service failure set."""

    INVALID = "positive timeout and nonempty JSON argv arrays required"
    READINESS = "service readiness probe exhausted"
    EXITED = "service exited before readiness"
    CLEANUP = "owned service process group still exists after cleanup"
    CLIENT = "client failed"


class ServiceError(ValueError):
    """Service startup, readiness, client or cleanup failed."""

    def __init__(self, reason: Reason) -> None:
        """Create a diagnostic for a closed failure reason."""
        super().__init__(reason.value)


class Host(Protocol):
    """Process-group ownership boundary; start returns a handle owned until stop."""

    def start(self, argv: list[str]) -> object:
        """Start the owned process group."""

    def probe(self, handle: object, argv: list[str], timeout: float) -> bool:
        """Check readiness within a bound."""

    def client(self, argv: list[str], timeout: float) -> int:
        """Execute the foreground client with a deadline."""

    def stop(self, handle: object, timeout: float) -> None:
        """Stop and verify the entire owned group."""


def serve(
    service: list[str], probe: list[str], client: list[str], host: Host, *, timeout: float = 30
) -> int:
    """Execute client only after readiness; always stop and verify the owned group."""
    attempts = 50
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or not all(
            isinstance(argv, list) and argv and all(isinstance(v, str) for v in argv)
            for argv in (service, probe, client)
        )
    ):
        raise ServiceError(Reason.INVALID)
    handle = host.start(service)
    try:
        for _ in range(attempts):
            if host.probe(handle, probe, timeout / attempts):
                return host.client(client, timeout)
        raise ServiceError(Reason.READINESS)
    finally:
        host.stop(handle, timeout)


class PosixHost:
    """Own an isolated process group, including wrapper-shell descendants."""

    def start(self, argv: list[str]) -> subprocess.Popen:
        """Start a new session whose PID is also its process group."""
        # LW-100352 [S603]; JSON argv is an explicit user command; shell=True adds
        # injection and per-executable wrappers cannot support arbitrary task services.
        return subprocess.Popen(argv, start_new_session=True)  # noqa: S603

    def probe(self, handle: subprocess.Popen, argv: list[str], timeout: float) -> bool:
        """Probe readiness while checking the service is still alive."""
        if handle.poll() is not None:
            raise ServiceError(Reason.EXITED)
        started = time.monotonic()
        try:
            return self.client(argv, timeout) == 0
        except subprocess.TimeoutExpired:
            return False
        finally:
            time.sleep(max(0, timeout - (time.monotonic() - started)))

    def client(self, argv: list[str], timeout: float) -> int:
        """Run the foreground client within its timeout."""
        # LW-100353 [S603]; argv is the explicit task command; a shell adds quoting
        # hazards and an executable allowlist would reject legitimate user tasks.
        return subprocess.run(argv, timeout=timeout, check=False).returncode  # noqa: S603

    def stop(self, handle: subprocess.Popen, timeout: float) -> None:
        """Terminate, reap and verify the whole owned process group."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(handle.pid, sig)
            except ProcessLookupError:
                break
            try:
                handle.wait(timeout=timeout / 2)
            except subprocess.TimeoutExpired:
                continue
        handle.wait(timeout=timeout)
        try:
            os.killpg(handle.pid, 0)
        except ProcessLookupError:
            return
        raise ServiceError(Reason.CLEANUP)


def _terminate(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", type=json.loads, required=True)
    parser.add_argument("--probe", type=json.loads, required=True)
    parser.add_argument("--client", type=json.loads, required=True)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _terminate)
    try:
        sys.exit(serve(args.service, args.probe, args.client, PosixHost(), timeout=args.timeout))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        sys.stderr.write(f"error: {error}\n")
        sys.exit(2)
