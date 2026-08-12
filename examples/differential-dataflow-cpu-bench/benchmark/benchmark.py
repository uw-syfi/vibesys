"""CPU benchmark harness for the differential-dataflow `bfs` superoptimization target.

The optimization axis is **CPU-seconds** the candidate engine burns to run the
fixed BFS workload (`reference/workload.py::METRIC_WORKLOAD`), lower is better.
Unlike the Nexmark targets there is no streaming server and no external baseline
engine: the baseline is **round 0 itself** — the *vanilla vendored
differential-dataflow source*, captured once on this box by `capture_baseline.py`
into `baseline.json`. Every later round is the same source with in-place
micro-optimizations, so the ratio is a same-code, same-guarantees CPU win.

Measurement: the workload self-generates its graph from a fixed seed and runs to
completion (no stdin), so CPU is taken as the child process's exact
`getrusage(RUSAGE_CHILDREN)` user+sys time via `os.wait4` — this captures every
worker thread and needs no polling. We run a few warmups then report the MEDIAN
of N timed runs, which is robust to the occasional scheduler tail (validated
meta-CoV ~0.1% at the canonical size).

HEADLINE (read by the Perf Evaluator): **`cpu_reduction_ratio` =
baseline_cpu_seconds / candidate_cpu_seconds** (higher is better; `> 1` ⇒ the
agent shaved real cycles off the vanilla engine; round 0 ≈ 1.0). Emitted only
when `baseline.json` is present; otherwise raw `cpu_seconds` is reported.

Usage:
  python3 bench/benchmark.py \
      --engine-cmd 'engine/target/release/examples/bfs' --output-json /tmp/perf.json
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE = os.path.normpath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_WORKSPACE, "reference"))
import workload  # noqa: E402

BASELINE_JSON = os.path.join(_HERE, "baseline.json")

DEFAULT_REPS = 7
DEFAULT_WARMUPS = 2


def cpu_once(binary, args):
    """Run the binary once; return exact child user+sys CPU-seconds (all threads)."""
    pid = os.fork()
    if pid == 0:  # child
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.execvp(binary, [binary, *args])
        except Exception:
            os._exit(127)
    _, status, ru = os.wait4(pid, 0)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)
        raise RuntimeError(f"engine exited abnormally ({code}) on args {args}")
    return ru.ru_utime + ru.ru_stime


def measure_cpu(binary, args, reps=DEFAULT_REPS, warmups=DEFAULT_WARMUPS):
    """Median-of-`reps` CPU-seconds after `warmups` untimed runs."""
    if not os.path.exists(binary):
        raise FileNotFoundError(f"engine binary not found: {binary}")
    for _ in range(warmups):
        cpu_once(binary, args)
    samples = [cpu_once(binary, args) for _ in range(reps)]
    return statistics.median(samples), samples


def _load_baseline():
    if not os.path.exists(BASELINE_JSON):
        return None
    try:
        with open(BASELINE_JSON) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--engine-cmd",
        required=True,
        help="candidate bfs binary path (no workload args; the harness appends them), "
        "e.g. 'engine/target/release/examples/bfs'",
    )
    ap.add_argument("--reps", type=int, default=DEFAULT_REPS)
    ap.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args()

    tokens = shlex.split(args.engine_cmd)
    if len(tokens) != 1:
        print(
            "benchmark ERROR: --engine-cmd must be just the binary path (the harness "
            "appends the fixed workload args).",
            file=sys.stderr,
        )
        return 2
    binary = tokens[0]
    wl = workload.METRIC_WORKLOAD

    print(f"benchmark — bfs CPU on fixed workload: {' '.join(wl)}")
    try:
        cpu_seconds, samples = measure_cpu(binary, wl, reps=args.reps, warmups=args.warmups)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"benchmark ERROR: {e}", file=sys.stderr)
        return 2

    baseline = _load_baseline()
    result = {
        "cpu_seconds": round(cpu_seconds, 6),
        "workload": wl,
        "reps": args.reps,
        "warmups": args.warmups,
        "samples": [round(s, 6) for s in samples],
        "baseline_cpu_seconds": None,
        "cpu_reduction_ratio": None,
    }
    if baseline and baseline.get("baseline_cpu_seconds"):
        b = float(baseline["baseline_cpu_seconds"])
        result["baseline_cpu_seconds"] = b
        result["cpu_reduction_ratio"] = round(b / cpu_seconds, 4) if cpu_seconds > 0 else None
        result["baseline_host"] = baseline.get("host")

    print(f"  cpu_seconds        : {result['cpu_seconds']}")
    print(f"  samples            : {result['samples']}")
    if result["cpu_reduction_ratio"] is not None:
        print(f"  baseline_cpu_seconds: {result['baseline_cpu_seconds']}")
        print(f"Primary metric: cpu_reduction_ratio = {result['cpu_reduction_ratio']}")
    else:
        print("  (no baseline.json — reporting raw cpu_seconds)")
        print(f"Primary metric: cpu_seconds = {result['cpu_seconds']}")

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"  wrote {args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
