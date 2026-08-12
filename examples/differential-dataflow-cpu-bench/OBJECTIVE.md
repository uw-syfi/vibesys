# Objective — superoptimize differential-dataflow's own source (CPU), in place

## The model

This target is **not** a from-scratch synthesis. Round 0 **is a real upstream
engine**: the vanilla source of the [differential-dataflow](https://github.com/TimelyDataflow/differential-dataflow)
crate, vendored verbatim into the workspace as `engine/` — simultaneously the
**baseline** and the **starting point**. Each round the agent makes **in-place
micro-optimizations to that real source** and is graded on two things:

1. **Correctness = output-equivalence** with the pristine round-0 engine, and
2. **CPU-seconds** to run a fixed workload, versus vanilla round 0.

The architecture stays **identical to differential-dataflow by construction** —
we edit its source, we never reimplement it. Any win is purely cycles the agent
shaved. The honest expectation is a **modest percentage, not a multiple** — and
that honesty is the entire point of this target.

## The workload

The engine is differential-dataflow's shipped **`bfs` example**: incremental
breadth-first distance from a root over a graph that is inserted, then perturbed
by inserting/removing a batch of edges each round. The graph is self-generated
from a fixed RNG seed — there is no on-disk input. The exact invocations (a
canonical metric workload plus a perturbation workload) live in
`reference/workload.py`, the single source of truth shared by the gate and the
benchmark. All measurement is at `-w 1` (one worker) for deterministic output
order; the result multiset is already canonical via `.consolidate()`.

Build (offline, against the warm crate cache):
```
cargo build --release --example bfs -p differential-dataflow \
    --offline --manifest-path engine/Cargo.toml
```
Binary: `engine/target/release/examples/bfs`.

## What you may edit (the optimization surface)

`engine/` is a trimmed differential-dataflow cargo workspace. In scope:

- **`engine/differential-dataflow/src/**`** — the library. This is the primary
  surface: allocation removal, `SmallVec` sizing, inlining, branch/merge tuning,
  data layout in `operators/`, `trace/implementations/`, `consolidation.rs`.
- **`engine/differential-dataflow/examples/bfs.rs`** — the query binary
  (secondary).
- **`engine/Cargo.toml` `[profile.release]`** — codegen knobs (LTO, codegen
  units, opt-level, panic).

Out of scope (not "the engine" here; the honest boundary): the pinned upstream
dependencies `timely` / `columnar` / etc., which come from the `~/.cargo`
registry cache via `Cargo.lock` and are **not** vendored or editable.

## The rules (enforced every round)

- **Micro-optimizations only.** Every hunk of `diff -ru _ref_engine engine`
  must be an identifiable micro-optimization of the existing code. Deleting and
  rewriting a module, swapping the algorithm or data-structure model, changing
  the execution/dataflow model, or pulling in a heavyweight dependency is an
  **automatic FAIL — even if faster and still correct.** `_ref_engine/` is the
  pristine round-0 snapshot the framework keeps for this diff.
- **Output-equivalence is absolute.** `acc_checker/equivalence_gate.py`
  runs the pristine engine LIVE on every workload and requires the candidate's
  normalized output to match **byte-for-byte** on all of them. A "faster" round
  that changes any BFS result fails here — that is the safety net, not a hole.
- **No reward hacking.** The engine must compute BFS in its own code. Reading a
  stored answer, short-circuiting the computation for the known workload, or
  shelling out to another engine is a fail even at exact match.

## The metric

`bench/benchmark.py` reports the headline
**`cpu_reduction_ratio = baseline_cpu_seconds / candidate_cpu_seconds`**
(higher is better; `> 1` means real cycles shaved; round 0 ≈ 1.0). The baseline
is round 0 itself, captured once on this box in `bench/baseline.json` by
`bench/capture_baseline.py`. CPU is the child process's exact
`getrusage(RUSAGE_CHILDREN)` user+sys time, reported as the median of several
runs after warmups. Unit: `x_vs_round0_cpu` — do not change it between rounds.
