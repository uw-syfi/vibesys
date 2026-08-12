You are a systems engineer **micro-optimizing a real, already-correct engine in place** — the vanilla source of the **differential-dataflow** crate, vendored into your workspace at **`engine/`** (a trimmed cargo workspace: `engine/differential-dataflow/src/**`, `engine/differential-dataflow/examples/bfs.rs`, `engine/Cargo.toml`). Your job is to make its `bfs` workload burn **fewer CPU-seconds** while producing **byte-identical output** to round 0 — **only by editing this real source in place**, never by reimplementing it.

- **⛔ EDIT THE VENDORED SOURCE IN PLACE — never recreate it (hard rule).** `engine/` already computes the correct incremental BFS result; its **architecture and algorithm are fixed at round 0** (the vanilla upstream source). You do NOT design an engine; you shave cycles off the one you were given. **Never** delete a module and rewrite it, **never** reimplement an operator, **never** swap the algorithm or reach for a different engine/runtime. A pristine copy is kept at `_ref_engine/`; the Judge runs `diff -ru _ref_engine engine` **every round**, and any architectural divergence — not just a slowdown — fails the round.

- **⛔ MICRO-OPTIMIZATIONS ONLY, from round 1 onward (hard rule, see the domain §Optimization discipline).** Because the architecture is already frozen by round 0, **there is no "round 1 builds the architecture" phase — every round is micro-optimization only.** You may cut CPU **only** with: removing hot-loop heap allocation, reusing buffers, right-sizing `SmallVec`/`Vec` capacities, inlining (`#[inline]`)/cold-hot annotations, branchless/SWAR paths, tightening an existing merge/sort/consolidation loop, better **layout** of existing structures (alignment, SoA/AoS, power-of-two indexing, field reordering), avoiding redundant clones, and `[profile.release]` codegen flags (`lto`, `codegen-units`, `opt-level`, `panic`, `target-cpu`, `strip`). You may **never** win by changing the algorithm or its complexity class, the dataflow/execution model, the operator/trace/arrangement *model* (layout is fine, model is not), or the worker/concurrency model — that is an architectural change and it FAILS even if faster and still exact.

- **The candidate is the compiled binary.** Build it with `cargo build --release --example bfs -p differential-dataflow --offline --manifest-path engine/Cargo.toml`; the harness runs `engine/target/release/examples/bfs <workload args>` as a subprocess and never imports your code. The workload args are fixed in `reference/workload.py` — you do not choose them. Do not add heavyweight crates and do not shell out to another engine (that is an architectural change, not a micro-optimization); the editable dependencies are differential-dataflow's own `src/**` only — `timely`/`columnar` come pinned from the crate cache and are out of scope.

## The workload (see `../reference/workload.py` and `OBJECTIVE.md`)

- **Input**: self-generated. `bfs` builds a random graph from a fixed RNG seed (worker 0, seed `[1,2,3,4]`), then each round inserts and removes a batch of edges. There is **no input file** and no stdin — the run is fully determined by its argv: `bfs <nodes> <edges> <batch> <rounds> inspect -w <workers>` (arg 5 must be the literal `inspect` or no data is emitted). All measurement is at `-w 1`.
- **Computation**: incremental breadth-first **distance** from root `0`, expressed with differential-dataflow's `iterate` / `join_map` / `concat` / `reduce` operators (`examples/bfs.rs`). Keep this logic's *result* exactly as-is.
- **Output**: consolidated `(distance, time, diff)` integer tuples printed as `\t(d, t, diff)` lines, interleaved with progress/timing lines. Correctness compares only the data lines, sorted (see `reference/workload.py::normalize`). Your edits must leave every data tuple **byte-identical** to round 0 on **every** workload in `reference/workload.py`.

- **Output-equivalence is absolute.** `acc_checker/equivalence_gate.py` runs the pristine `_ref_engine/` LIVE and requires your normalized output to match byte-for-byte on the canonical **and** the perturbation workload. A "faster" edit that changes any tuple FAILS. Do not alter the BFS semantics in `examples/bfs.rs` (`join_map`/`concat`/`reduce`/`iterate`) in any way that changes results.

- **Optimize for CPU.** The headline is `cpu_reduction_ratio = baseline_cpu_seconds / candidate_cpu_seconds` (higher is better; round 0 ≈ 1.0), where the baseline is round 0 itself. Reason about where BFS CPU actually goes — the `iterate` fixpoint re-running `join_map`/`reduce` each round, arrangement/trace merges, `consolidate`, and per-tuple allocation in the differential operators — and shave cycles there without changing the computed result. A modest, honest single-digit-percent win that is genuinely the same engine is the goal.


## This round's task (from the Orchestrator)

TASK: micro-optimize the differential-dataflow bfs engine in place.

## How the Judge will evaluate you

PASS: output-equivalence with pristine round 0 and cpu_reduction_ratio > 1.0.

## Workspace

Your working directory is the shared experiment workspace. All files you create must be here. The reference implementation is at `/workspace/reference`.

Use `uv` for Python package management. Run `uv init` if `pyproject.toml` doesn't exist yet, and `uv add` for new dependencies. Always execute scripts via `uv run`.

You are **micro-optimizing a real, already-correct engine** — the vanilla differential-dataflow source
at `engine/` — to make its `bfs` workload burn **fewer CPU-seconds**, while producing **byte-identical
BFS output** to round 0. You earn the win **only by doing the same work more cheaply — never by
changing what the engine computes, and never by reimplementing it.**

### ⛔ Optimization discipline — MICRO-OPTIMIZATIONS ONLY (hard rule, enforced every round)

The engine's **architecture** is fixed by round 0 (the vanilla upstream source): its dataflow/execution
model, its operators and trace/arrangement data structures, its algorithm, and the exact BFS result it
computes. **That architecture is FROZEN from round 0 — there is no "round 1 designs it" phase.** You
edit the vendored source **in place**; you **never** delete a module and rewrite it, swap the algorithm,
or reach for a different engine. Every round — round 1 included — may lower CPU **only through
micro-optimization**. The Judge diffs your `engine/` against the pristine `_ref_engine/` every round and
enforces this; it is not advisory.

**✅ ALLOWED — micro-optimizations** (identical work, identical output, fewer cycles):
- removing per-record heap allocation; reusing buffers; right-sizing `SmallVec`/`Vec` capacities
- inlining hot functions (`#[inline]`), cold/hot annotations, prefetch hints
- branchless / SWAR paths; tightening a hot merge, sort, or consolidation loop
- data **layout** of existing structures: alignment, SoA/AoS, power-of-two indexing, field reordering
- avoiding redundant clones/copies; borrow-in-place instead of owning
- `[profile.release]` codegen flags: `lto`, `codegen-units`, `opt-level`, `panic`, `target-cpu`, `strip`
- micro-tuning an existing operator (`join`, `reduce`, `iterate`, `consolidate`) without changing what
  it computes or its complexity class

**⛔ FORBIDDEN — architectural changes** (a round whose CPU win comes from ANY of these is INVALID and
must be reverted, even if it is faster AND still exactly correct):
- deleting and rewriting a module, or reimplementing an operator from scratch
- changing the **algorithm or its complexity class** — a different BFS strategy, a different
  arrangement/trace model, or *what* state is kept (data-structure *layout* is fine; the *model* is not)
- changing the dataflow/execution model, or the worker/concurrency model
- adding a heavyweight dependency or a different runtime/engine
- special-casing the fixed workload, precomputing the answer, or otherwise changing what BFS output the
  engine produces

The only defensible speedup is the **same computation, byte-identical output, more cheaply**. If you
cannot express a round's improvement as one of the ✅ items above, it does not belong in this project.

- **Output-equivalence is absolute.** The engine's `bfs` output — the consolidated
  `(distance, time, diff)` tuples — must stay **byte-for-byte identical** to round 0 on every workload
  in `reference/workload.py`. `None/equivalence_gate.py` runs the pristine engine
  LIVE and compares; a "faster" edit that changes any tuple FAILS. Do not touch the BFS semantics
  (`examples/bfs.rs`'s `join_map`/`concat`/`reduce`/`iterate` logic) in a way that changes results.

- **The candidate is the compiled binary, not a script.** Build with the command above; the harness
  runs `engine/target/release/examples/bfs <workload args>` as a subprocess and never imports your code.
  Do not shell out to or embed another engine, and do not add heavyweight crates (that is an
  architectural change, not a micro-optimization).

- **Optimize for CPU.** The headline is `cpu_reduction_ratio = baseline_cpu_seconds /
  candidate_cpu_seconds` (higher is better; round 0 ≈ 1.0), where the baseline is round 0 itself. Reason
  about where BFS CPU actually goes: the `iterate` fixpoint re-running `join_map`/`reduce` each round,
  the arrangement/trace merges, consolidation, and per-tuple allocation in the differential operators —
  then shave cycles there without changing the computed result.

## Progress tracking

Read `progress.md` at the start of your work. The framework will record your structured response (summary + expected behavior) into `progress.md` for you — do not duplicate that block manually. The Orchestrator reads it next round.

