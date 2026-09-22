"""Deterministic real backend for the browser smoke test, without agent calls."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from server.events import AgentOutputChunkData, EventType, RunStartedData
from server.runtime import ServerRuntime


def main() -> None:
    """Run a controlled invocation loop until the test terminates the process."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-socket", type=Path, required=True)
    args = parser.parse_args()
    runtime = ServerRuntime(socket_path=args.control_socket)

    def run() -> None:
        runtime.journal.record(
            EventType.RUN_STARTED,
            data=RunStartedData(outer_loop="agent", input="browser-smoke", max_rounds=100),
        )
        index = 0
        while True:
            index += 1
            execution = runtime.controller.start_agent_execution(
                "implementer", "round-1", "Deterministic browser verification"
            )
            if "finish-browser-fixture" in execution.user_prompt:
                return
            runtime.journal.record(
                EventType.AGENT_OUTPUT_CHUNK,
                agent_kind="implementer",
                round_label="round-1",
                execution_id=execution.execution_id,
                data=AgentOutputChunkData(
                    channel="assistant",
                    content=f"Browser fixture step {index}: {execution.user_prompt}",
                ),
            )
            time.sleep(0.3)
            runtime.controller.after_agent(
                "implementer", "round-1", execution_id=execution.execution_id
            )

    runtime.run(run)


if __name__ == "__main__":
    main()
