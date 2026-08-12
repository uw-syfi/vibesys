# differential-dataflow (bfs) — micro-optimize a real engine's own source, in place (CPU)

**Use for:** the `differential-dataflow-cpu-bench` target (see its `OBJECTIVE.md`). This is
**superoptimization of a real upstream engine**, not synthesis. Round 0 is the *vanilla source of the
[differential-dataflow](https://github.com/TimelyDataflow/differential-dataflow) crate*, vendored
verbatim into the workspace as `engine/` — it is simultaneously the **baseline** and the **starting
point**. Each round the agent makes **in-place micro-optimizations to that real source** and is graded
on (1) **output-equivalence** with the pristine round 0 and (2) **CPU-seconds** on a fixed workload.

The workload is differential-dataflow's shipped **`bfs` example**: incremental breadth-first distance
from a root over a graph that is inserted, then perturbed by inserting/removing a batch of edges each
round. The graph is self-generated from a fixed RNG seed — there is no input file. The exact
invocations live in `reference/workload.py` (the single source of truth shared by the gate and the
benchmark), run at `-w 1` for deterministic output order; the result multiset is already canonical via
`.consolidate()`.

**The architecture is identical to differential-dataflow by construction** — we edit its source, we
never reimplement it. Any win is purely cycles the agent shaved. The honest expectation is a **modest
percentage, not a multiple**, and that honesty is the point. The role sections below are injected into
the base implementer/judge/orchestrator prompts.

## The optimization surface (what "the engine" is here)

`engine/` is a trimmed differential-dataflow cargo workspace with a single member. Editable:

- **`engine/differential-dataflow/src/**`** — the library (primary surface).
- **`engine/differential-dataflow/examples/bfs.rs`** — the query binary (secondary).
- **`engine/Cargo.toml` `[profile.release]`** — codegen knobs.

Not editable / not "the engine": the pinned upstream deps `timely` / `columnar` / etc., resolved from
the `~/.cargo` registry cache via `Cargo.lock`. That is the honest boundary.

Build (offline, warm cache): `cargo build --release --example bfs -p differential-dataflow --offline
--manifest-path engine/Cargo.toml`. Binary: `engine/target/release/examples/bfs`.

## implementer

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
  in `reference/workload.py`. `{{ accuracy_checker_path }}/equivalence_gate.py` runs the pristine engine
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

## judge

Enforce **output-equivalence with the pristine round-0 engine** as a hard gate before any CPU credit,
and enforce that every change is a **micro-optimization**, not a re-architecture. There is no external
truth to regenerate here: correctness *is* "produces exactly what vanilla differential-dataflow
produces."

- **⛔ IN-PLACE-EDIT / NO-REARCHITECTURE is a HARD GATE, checked first (EVERY round, round 1 included).**
  The architecture is frozen by round 0, so diff the candidate against the pristine snapshot:
  ```
  diff -ru _ref_engine engine    # (ignoring target/)
  ```
  Every hunk must be an identifiable **micro-optimization**. **Automatic FAIL** if a module was deleted
  and rewritten, an operator was reimplemented, the algorithm or its complexity class changed, the
  dataflow/execution or concurrency model changed, or a heavyweight dependency was added — **even if the
  result is faster and still byte-identical.** Name the ✅ micro-optimization behind each hunk; if a hunk
  isn't one, reject the round. A round that only tweaks hot-path layout/allocation/codegen and stays
  equivalent is fine (even if its CPU win is small or zero); a round that changes the model is not.

- **⛔ OUTPUT-EQUIVALENCE IS A HARD GATE.** Run:
  ```
  {{ accuracy_checker_path }}/equivalence_gate.py \
      --engine-cmd 'engine/target/release/examples/bfs' \
      --rebuild-cmd 'cargo build --release --example bfs -p differential-dataflow --offline --manifest-path engine/Cargo.toml'
  ```
  It rebuilds the candidate, runs the **pristine `_ref_engine/` LIVE** on every workload in
  `reference/workload.py` (canonical + perturbation) to produce the golden, and requires the candidate's
  normalized output to match **byte-for-byte on all of them**. Require **exit 0 / PASS**. Running the
  pristine engine live on ≥2 distinct workloads is the anti-memorization mechanism — there is no stored
  golden to hardcode. Treat any diverging tuple on any workload as a real defect, not noise.

- **No reward hacking.** The engine must compute BFS in its own (differential-dataflow's) code. Reading
  a stored answer, short-circuiting the computation for the known workload, embedding/shelling out to
  another engine, or otherwise faking the output is a FAIL even at exact match.

- **CPU is scored only after both gates pass.** The metric is `cpu_reduction_ratio` from
  `{{ bench_path }}/benchmark.py` (higher is better), and it is only meaningful as a **same-code,
  same-output** number won purely by micro-optimization against the pristine `_ref_engine/`. A round
  that is faster because it changed the model or the output is a FAIL, not a win.

## orchestrator

Sequence rounds so **output stays byte-identical and CPU follows**. The architecture is already settled
by round 0: the vanilla differential-dataflow source at `engine/`, which already computes the correct
BFS result. There is no round that designs it. bfs is a single fixed workload — completeness is one
correct engine, so every round's deliverable is the **same correct engine, micro-optimized further**;
rounds differ only in how little CPU it burns.

- **⛔ Round 0 IS the frozen architecture.** It already produces the exact BFS output the equivalence
  gate checks against. The algorithm, the dataflow/execution model, the operator/trace data structures,
  and the concurrency model are all fixed here and may never be changed.
- **Every round (round 1 included) is MICRO-OPTIMIZATION ONLY** on the vendored engine, edited in place
  — remove hot-loop allocation, tighten a merge/consolidation loop, right-size buffers, improve the
  layout of existing structures, adjust `[profile.release]` codegen flags — **while keeping the output
  byte-identical every round**. **No round may re-architect to go faster:** do not delete/rewrite a
  module, reimplement an operator, swap the algorithm or complexity class, change the dataflow/execution
  model, or change concurrency. If a change isn't a micro-optimization (see the implementer §Optimization
  discipline), or it changes any BFS tuple, the round FAILS and reverts.
- Never trade away output-equivalence for CPU: the metric is `cpu_reduction_ratio` (round-0 CPU ÷
  candidate CPU) **at byte-identical output**, won purely by micro-optimization against the pristine
  `_ref_engine/`. A modest single-digit-percent win that is genuinely the same engine is the goal — not
  a large number bought by changing what the engine does.
