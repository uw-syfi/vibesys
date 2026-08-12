"""Fixed workload for the differential-dataflow `bfs` superoptimization target.

Single source of truth for the exact `bfs` invocations used by BOTH the
correctness gate (`acc_checker/equivalence_gate.py`) and the CPU benchmark
(`bench/benchmark.py`). There is intentionally no on-disk input artifact:
`bfs` self-generates its graph from a fixed RNG seed (`bfs.rs` seeds worker 0
with `&[1,2,3,4]`), so a workload is fully specified by its argv.

Why these exact args (validated offline, 2026-08-09):
  * `inspect` (arg 5) is REQUIRED — without it `bfs` filters the result to
    nothing and emits no data lines (`bfs.rs:19,35-42`).
  * `-w 1` (single worker) makes the emitted line ORDER deterministic. The
    output *multiset* is already canonical via `.consolidate()`; running one
    worker removes the only remaining nondeterminism (inter-worker print race).
    Correctness is still checked defensively by sorting the data lines.
  * CANONICAL is the metric workload: ~0.93 CPU-s median at `-w 1`, ~4.8 KB of
    normalized output, meta-CoV ~0.1% under a median-of-N benchmark.
  * PERTURBATION is a *different, smaller* graph. The gate runs BOTH: because the
    pristine round-0 engine is executed LIVE to produce each golden (never a
    stored file), an engine cannot pass by memorizing one answer — it must
    reproduce the real BFS result on two distinct inputs.

Argv shape:  bfs <nodes> <edges> <batch> <rounds> inspect -w <workers>
"""

from __future__ import annotations

# nodes, edges, batch, rounds, "inspect", "-w", workers
CANONICAL: list[str] = ["200000", "2000000", "200", "10", "inspect", "-w", "1"]
PERTURBATION: list[str] = ["100000", "1000000", "200", "10", "inspect", "-w", "1"]

# The gate checks output-equivalence on EVERY workload here (canonical + ≥1 more,
# the anti-memorization property). The benchmark times only METRIC_WORKLOAD.
WORKLOADS: list[list[str]] = [CANONICAL, PERTURBATION]
METRIC_WORKLOAD: list[str] = CANONICAL

# Relative path (from an engine cargo-workspace root) to the compiled bfs binary.
BFS_BINARY_RELPATH = "target/release/examples/bfs"


# Build command for a bfs engine, given its Cargo manifest path. Offline against
# the warm ~/.cargo cache — the run box never fetches crates.
def build_cmd(manifest_path: str) -> list[str]:
    return [
        "cargo",
        "build",
        "--release",
        "--example",
        "bfs",
        "-p",
        "differential-dataflow",
        "--offline",
        "--manifest-path",
        manifest_path,
    ]


def normalize(stdout: str) -> str:
    """Canonicalize bfs stdout to just its data lines, order-independent.

    `bfs` prints progress/status lines (``performing BFS ...``, ``loaded``,
    per-round timings) interleaved with the actual result: consolidated
    ``\\t(distance, time, diff)`` integer tuples. Correctness compares ONLY the
    data lines (leading tab + ``(``), sorted — timings and worker-order are not
    part of the answer.
    """
    lines = [ln for ln in stdout.splitlines() if ln.startswith("\t(")]
    lines.sort()
    return "\n".join(lines)
