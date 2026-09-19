#!/usr/bin/env python3
"""Measure event-journal attach wall time and peak RSS in fresh processes."""

from __future__ import annotations

import argparse
import json
import math
import os
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_MIB = 1024 * 1024
_DEFAULT_SIZES_MIB = (20, 100, 500)


@dataclass(frozen=True, slots=True)
class Measurement:
    """One isolated attach measurement."""

    wall_seconds: float
    peak_rss_mib: float
    records: int
    parsed_records: int
    source_bytes: int
    index_bytes: int


@dataclass(frozen=True, slots=True)
class BenchmarkTarget:
    """Stable inputs shared by every mode for one journal size."""

    repeats: int
    python: Path
    module_root: Path
    source: Path


def _event_line(sequence: int, payload_bytes: int = 900) -> bytes:
    event = {
        "protocol_version": 1,
        "sequence": sequence,
        "run_id": "benchmark",
        "timestamp": "2026-01-01T00:00:00Z",
        "type": "output",
        "text": "x" * payload_bytes,
    }
    return (json.dumps(event, separators=(",", ":")) + "\n").encode()


def _generate_journal(path: Path, target_bytes: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    written = 0
    with path.open("wb") as stream:
        while written < target_bytes:
            count += 1
            line = _event_line(count)
            stream.write(line)
            written += len(line)
    return count


def _worker(path: Path) -> None:
    from server.events import EventStore  # noqa: PLC0415  # implementation selected by PYTHONPATH

    started = time.perf_counter()
    store = EventStore(path, run_id="benchmark")
    wall_seconds = time.perf_counter() - started
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_rss_mib = rss / (_MIB if sys.platform == "darwin" else 1024)
    index_path = path.with_name(f"{path.name}.idx")
    print(  # benchmark result protocol
        json.dumps(
            {
                "wall_seconds": wall_seconds,
                "peak_rss_mib": peak_rss_mib,
                "records": len(store.event_headers()),
                "parsed_records": store.parsed_record_count,
                "source_bytes": path.stat().st_size,
                "index_bytes": index_path.stat().st_size if index_path.exists() else 0,
            }
        )
    )


def _measure(python: Path, module_root: Path, source: Path) -> Measurement:
    environment = dict(os.environ)
    source_path = str(module_root / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        [source_path, environment["PYTHONPATH"]] if environment.get("PYTHONPATH") else [source_path]
    )
    result = subprocess.run(  # noqa: S603  # explicit benchmark interpreter and script
        [str(python), str(Path(__file__).resolve()), "--worker", str(source)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return Measurement(**json.loads(result.stdout))


def _prepare_warm(python: Path, module_root: Path, source: Path) -> None:
    _measure(python, module_root, source)


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _summary(measurements: list[Measurement]) -> dict[str, float | int]:
    walls = [item.wall_seconds for item in measurements]
    rss = [item.peak_rss_mib for item in measurements]
    sample = measurements[-1]
    return {
        "wall_median_seconds": statistics.median(walls),
        "wall_p95_seconds": _percentile(walls, 0.95),
        "peak_rss_median_mib": statistics.median(rss),
        "peak_rss_p95_mib": _percentile(rss, 0.95),
        "records": sample.records,
        "parsed_records": sample.parsed_records,
        "source_bytes": sample.source_bytes,
        "index_bytes": sample.index_bytes,
    }


def _run_mode(
    mode: str,
    *,
    target: BenchmarkTarget,
    next_sequence: int,
) -> tuple[list[Measurement], int]:
    index_path = target.source.with_name(f"{target.source.name}.idx")
    measurements: list[Measurement] = []
    if mode == "warm":
        index_path.unlink(missing_ok=True)
        _prepare_warm(target.python, target.module_root, target.source)
    for _ in range(target.repeats):
        if mode == "cold":
            index_path.unlink(missing_ok=True)
        elif mode == "changed-source":
            index_path.unlink(missing_ok=True)
            _prepare_warm(target.python, target.module_root, target.source)
            with target.source.open("ab") as stream:
                stream.write(_event_line(next_sequence))
            next_sequence += 1
        measurements.append(_measure(target.python, target.module_root, target.source))
    return measurements, next_sequence


def _render_markdown(results: dict[str, dict[str, dict[str, float | int]]]) -> str:
    lines = [
        "| Journal | Mode | Wall median | Wall p95 | Peak RSS median | Peak RSS p95 | Index |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for size, modes in results.items():
        for mode, values in modes.items():
            lines.append(
                f"| {size} MiB | {mode} | {values['wall_median_seconds']:.3f}s | "
                f"{values['wall_p95_seconds']:.3f}s | {values['peak_rss_median_mib']:.1f} MiB | "
                f"{values['peak_rss_p95_mib']:.1f} MiB | "
                f"{values['index_bytes'] / _MIB:.1f} MiB |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    """Generate journals and benchmark isolated cold, warm, and changed attaches."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--module-root", type=Path, default=Path.cwd())
    default_work_dir = Path(tempfile.gettempdir()) / "vibesys-event-attach-bench"
    parser.add_argument("--work-dir", type=Path, default=default_work_dir)
    parser.add_argument("--sizes-mib", type=int, nargs="+", default=_DEFAULT_SIZES_MIB)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker is not None:
        _worker(args.worker)
        return
    if args.repeats < 1 or any(size < 1 for size in args.sizes_mib):
        parser.error("sizes and repeats must be positive")

    results: dict[str, dict[str, dict[str, float | int]]] = {}
    for size_mib in args.sizes_mib:
        source = args.work_dir / f"events-{size_mib}mib.jsonl"
        next_sequence = _generate_journal(source, size_mib * _MIB) + 1
        target = BenchmarkTarget(
            repeats=args.repeats,
            python=args.python,
            module_root=args.module_root,
            source=source,
        )
        modes: dict[str, dict[str, float | int]] = {}
        for mode in ("cold", "warm", "changed-source"):
            measurements, next_sequence = _run_mode(
                mode,
                target=target,
                next_sequence=next_sequence,
            )
            modes[mode] = _summary(measurements)
        results[str(size_mib)] = modes

    rendered = _render_markdown(results)
    print(rendered, end="")  # benchmark report
    if args.output is not None:
        args.output.write_text(rendered)


if __name__ == "__main__":
    main()
