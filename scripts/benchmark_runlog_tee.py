#!/usr/bin/env python3
"""Measure JSONL appends per second with and without the additive run.log tee.

Run with ``uv run python scripts/benchmark_runlog_tee.py``. Both modes use the
production event journal and temporary local files; setup and imports are not
included. Values are medians over alternating runs after one warm-up pair.
"""

from __future__ import annotations

import argparse
import os
import statistics
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from vibesys.run import CoreEventType, EventJournal
from vs_project.api import Project
from vs_sim.api.testing import ManualClock, SimThreads


def measure(root: Path, *, tee: bool, events: int, payload: str) -> float:
    """Measure one real journal's hot path, excluding attachment and epoch setup."""
    root.mkdir()
    project = Project.open(root)
    journal = EventJournal(ManualClock(start=1234), SimThreads())
    if tee:
        journal.enable_runlog(project, "benchmark")
    journal.attach(Project.log_directory_for(root, "benchmark"), "benchmark")
    started = time.perf_counter()
    for _ in range(events):
        journal.emit(CoreEventType.RUN_STARTED, payload)
    elapsed = time.perf_counter() - started
    journal.close()
    return events / elapsed


def main() -> None:
    """Print median throughput, latency and incremental cost."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=20000)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--payload-bytes", type=int, default=768)
    args = parser.parse_args()
    previous = os.environ.get("VIBESYS_STATE_HOME")
    rates: dict[bool, list[float]] = {False: [], True: []}
    try:
        with TemporaryDirectory(prefix="runlog-benchmark-") as temporary:
            root = Path(temporary)
            os.environ["VIBESYS_STATE_HOME"] = str(root / "state-home")
            for iteration in range(args.repeat + 1):
                for tee in (False, True) if iteration % 2 else (True, False):
                    rate = measure(
                        root / f"run-{iteration}-{tee}",
                        tee=tee,
                        events=args.events,
                        payload="x" * args.payload_bytes,
                    )
                    if iteration:
                        rates[tee].append(rate)
    finally:
        if previous is None:
            os.environ.pop("VIBESYS_STATE_HOME", None)
        else:
            os.environ["VIBESYS_STATE_HOME"] = previous
    baseline, tee_rate = (statistics.median(rates[mode]) for mode in (False, True))
    print(f"events={args.events}, repeats={args.repeat}, text_bytes={args.payload_bytes}")
    print(f"JSONL only: {baseline:.0f} appends/s, {1e6 / baseline:.2f} us/append")
    print(f"JSONL + tee: {tee_rate:.0f} appends/s, {1e6 / tee_rate:.2f} us/append")
    print(f"Incremental cost: {1e6 / tee_rate - 1e6 / baseline:.2f} us/append")


if __name__ == "__main__":
    main()
