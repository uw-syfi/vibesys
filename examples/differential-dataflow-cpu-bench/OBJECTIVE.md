# Objective — superoptimize differential-dataflow's own source (CPU), in place

## The model

This target is **not** a from-scratch synthesis. Round 0 **is a real upstream
engine**: the vanilla source of the [differential-dataflow](https://github.com/TimelyDataflow/differential-dataflow)
crate, materialized at a pinned commit into the workspace as the editable
`engine/` — simultaneously the **baseline** and the **starting point**. A
byte-identical pristine copy is materialized alongside it as `_ref_engine/` (never
edited) for the correctness diff. Both come from the same repo + commit declared
as the two `[[workspace.sources]]` in `vibesys.input.toml`. Each round the agent
makes **in-place micro-optimizations to `engine/`** and is graded on two things:

1. **Correctness = output-equivalence** with the pristine round-0 engine, and
2. **CPU-seconds** to run a fixed workload, versus vanilla round 0.

The architecture stays **identical to differential-dataflow by construction** —
we keep its algorithm, its data model, and its execution/concurrency model (the
**walls**), and never swap in a different engine; *within those walls* we may
restructure or reimplement an operator's internals. Any win is purely cycles the
agent shaved at the same complexity and byte-identical output. The honest
expectation is a **modest percentage, not a multiple** — and that honesty is the
entire point of this target.

## The workload

The engine is differential-dataflow's shipped **`bfs` example**: incremental
breadth-first distance from a root over a graph that is inserted, then perturbed
by inserting/removing a batch of edges each round. The graph is self-generated
from a fixed RNG seed — there is no on-disk input. The exact invocations (a
canonical metric workload plus a perturbation workload) live in
`reference/workload.py`, the single source of truth shared by the gates and the
benchmark. All measurement is at `-w 1` (one worker) for deterministic output
order; the result multiset is already canonical via `.consolidate()`.

Build (offline, against the warm crate cache):
```
export PATH="$HOME/.cargo/bin:$PATH"
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
registry cache via `Cargo.lock` and are **not** editable. `_ref_engine/` is the
framework's pristine round-0 snapshot and must never be edited.

## The rules (enforced every round)

- **Stay inside the walls — same algorithm, same output.** You may micro-optimize
  *or* restructure / reimplement an operator's internals, as long as `diff -ru
  _ref_engine engine` never breaches a wall: swapping the algorithm or its
  complexity class, changing the arrangement/trace **data model** (*what* state is
  kept), changing the execution/dataflow or concurrency model, or pulling in a
  heavyweight dependency is an **automatic FAIL — even if faster and still
  correct.** Line-level layout and an operator's *internal* representation are
  fair game; the externally-observable model is not.
- **Output-equivalence is absolute.** `accuracy_checker/checker.py` runs the
  pristine engine LIVE on every workload and requires the candidate's normalized
  output to match **byte-for-byte** on all of them, plus a broad differential-fuzz
  campaign, a metamorphic determinism check across worker counts, a crash-recovery
  check, and a ThreadSanitizer race check. A "faster" round that changes any BFS
  result fails here — that is the safety net, not a hole.
- **No reward hacking.** The engine must compute BFS in its own code. Reading a
  stored answer, short-circuiting the computation for the known workload, or
  shelling out to another engine is a fail even at exact match.

## The metric

`benchmark/benchmark.py` reports the contract metric **`cpu_seconds`** — the
median child-process `getrusage(RUSAGE_CHILDREN)` user+sys CPU-seconds to run the
fixed metric workload, taken as the median of several timed runs after warmups.
Lower is better (`objectives.toml` declares `direction = "min"`). For display it
also emits **`cpu_reduction_ratio = baseline_cpu_seconds / candidate_cpu_seconds`**
(higher is better; `> 1` means real cycles shaved; round 0 ≈ 1.0). The baseline is
round 0 itself, captured once on this box in `benchmark/baseline.json` by
`benchmark/capture_baseline.py`.
