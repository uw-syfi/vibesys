"""Real live gateway child for the browser adverse-connectivity matrix."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from launch import default_runs
from server.runtime import ServerRuntime

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-directory", type=Path, required=True)
    parser.add_argument("--web-assets", type=Path, required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--port", type=int, required=True)
    return parser.parse_args()


def wait_for_owner_close(read_owner: Callable[[], bytes], request_stop: Callable[[], None]) -> None:
    """Request shutdown when the owning worker closes its inherited pipe."""
    try:
        while read_owner():
            pass
    finally:
        request_stop()


def main() -> int:
    """Serve one blocked live run until the owning browser test stops it."""
    arguments = _arguments()
    stopped = threading.Event()
    runtime = ServerRuntime(
        runs=default_runs(),
        socket_path=arguments.store_directory / "control.sock",
        web=True,
        web_port=arguments.port,
        web_assets=arguments.web_assets,
        instance_path=arguments.store_directory / "web-gateway.json",
        detach=True,
    )

    def request_stop(_signal: int, _frame: FrameType | None) -> None:
        stopped.set()
        runtime.shutdown()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    owner_fd = sys.stdin.fileno()
    threading.Thread(
        target=wait_for_owner_close,
        args=(lambda: os.read(owner_fd, 4096), lambda: request_stop(0, None)),
        daemon=True,
        name="web-matrix-owner",
    ).start()

    def live_run() -> None:
        runtime.executions.publish_agent_output(f"{arguments.marker}\n")
        stopped.wait()

    runtime.run(live_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
