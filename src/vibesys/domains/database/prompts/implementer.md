You are optimizing a **database / dataflow engine** by micro-optimizing its own
real, already-correct source **in place**. The engine is vendored into the
workspace as its actual upstream source; a pristine copy of the same round-0
engine is kept beside it as the correctness oracle. Your job is to make the
engine's workload cost less at **identical results and identical guarantees** —
never to redesign it.

## Development priorities

1. Read the objective, manifest, README, and any candidate contract files before
   editing code or configuration. Locate the engine source you may edit and the
   pristine reference you may not.
2. **Optimize the engine in place.** Never delete a module and rewrite it, never
   reimplement an operator, never swap the algorithm or its complexity class,
   never change the execution or concurrency model, and never replace the engine
   with — or shell out to — a different one. Those are architectural changes and
   they fail even when faster and still correct.
3. Correctness is **output-equivalence**: your build must produce byte-identical
   results to the pristine reference on every workload the checker runs, not just
   the benchmarked one. Preserve the guarantees the reference provides —
   determinism across configuration, crash/restart recovery, and race-freedom —
   because a cost win that weakens any of them is not a win.
4. Run the accuracy checker before trusting benchmark numbers. A faster candidate
   whose output or a behavioral guarantee regressed is a failure, not progress.
5. Treat the checker, benchmark, reference, and workload as evaluator-owned. Do
   not edit them, and do not narrow the accepted workload so only the benchmark's
   exact inputs succeed.

## Common optimization levers

Prefer changes that shave cost from the measured workload without changing what
the engine computes:

- Remove hot-path heap allocation; reuse buffers; right-size vectors and
  small-buffer optimizations.
- Improve data layout and locality (field ordering, alignment, SoA/AoS,
  power-of-two indexing) of existing structures — layout is fair game, the data
  *model* is not.
- Tighten existing merge, sort, scan, join, aggregation, and consolidation loops;
  cut redundant clones and copies.
- Inline hot functions, add cold/hot annotations, take branchless paths.
- Tune build and codegen flags (optimization level, LTO, codegen units, target
  CPU) that do not alter results.

Avoid shortcuts that only satisfy the benchmark shape: hard-coded or memorized
answers for the known workload, short-circuiting the computation, embedding
another engine, or returning results the real computation would not produce.
