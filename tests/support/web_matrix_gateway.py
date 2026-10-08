"""Real live gateway child for the browser adverse-connectivity matrix."""

from __future__ import annotations

import argparse
import signal
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from launch import default_runs
from server.runtime import ServerRuntime

if TYPE_CHECKING:
    from types import FrameType


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-directory", type=Path, required=True)
    parser.add_argument("--web-assets", type=Path, required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--port", type=int, required=True)
    return parser.parse_args()


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

    def live_run() -> None:
        runtime.journal.publish_output("stdout", f"{arguments.marker}\n", source="web-matrix")
        stopped.wait()

    runtime.run(live_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
