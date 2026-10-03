#!/usr/bin/env python3
"""Cut the checked-in session slices out of the full tracegen output.

The full trace (6000 sessions, 3.4 MB; see ../README.md "Benchmark inputs")
is not committed. The modes replay only a prefix of it, so the bundle commits
contiguous session ranges instead:

  traces/coding_session_0000-0259.csv  quick (first 60) and full (all 260)
                                       sessions, measured only
  traces/coding_session_3000-3299.csv  a disjoint range held out from tuning;
                                       holdout measures the first 260
  traces/coding_session_5000-5011.csv  the warmup pool for every mode (12
                                       sessions, disjoint from both measured
                                       ranges above -- see run.py's
                                       `WARMUP_TRACE` and README.md "Warmup")

Each slice keeps the source rows byte for byte except `arrival_time_ms`, which
is shifted so the slice's first session arrives at 0 (session_runner requires
a canonical trace to start at 0; replay is saturated, so arrivals are unused).

    python3 benchmark/slice_trace.py coding_session_synthetic.csv benchmark/traces
"""

from __future__ import annotations

import argparse
from pathlib import Path

SLICES: tuple[tuple[int, int], ...] = ((0, 260), (3000, 3300), (5000, 5012))


def slice_name(start: int, stop: int) -> str:
    return f"coding_session_{start:04d}-{stop - 1:04d}.csv"


def cut(lines: list[str], start: int, stop: int) -> str:
    """Return the CSV text for sessions `start <= n < stop` of `lines` (header first)."""
    out = [lines[0]]
    base: float | None = None
    for line in lines[1:]:
        fields = line.rstrip("\n").split(",")
        index = int(fields[1].rsplit("_", 1)[1])
        if not start <= index < stop:
            continue
        arrival = float(fields[3])
        if base is None:
            base = arrival
        fields[3] = f"{arrival - base:.6f}"
        out.append(",".join(fields) + "\n")
    if base is None:
        raise SystemExit(f"no sessions in [{start}, {stop}) in the source trace")
    return "".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("source", type=Path, help="full coding_session_synthetic.csv")
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()

    lines = args.source.read_text().splitlines(keepends=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for start, stop in SLICES:
        path = args.out_dir / slice_name(start, stop)
        path.write_text(cut(lines, start, stop))
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
