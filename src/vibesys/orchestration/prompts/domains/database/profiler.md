## Engine profile capture

Profile the **same workload the benchmark scores**, at steady state after
warmup, in the **exact configuration the metric uses** (for example the same
worker/parallelism count and input sizes) so the profile attributes cost on the
scored path rather than a different one. Start from the objective and manifest to
identify the engine binary, the fixed workload arguments, and the headline cost
metric.

Useful evidence includes:

- Benchmark output with the headline cost metric (for example CPU-seconds or
  wall time) alongside any latency, throughput, or resource fields it reports.
- A CPU profile or flamegraph attributing self and inclusive time to functions
  on the hot path — captured with whatever profiler fits the engine's language
  and build (for example `perf`, `samply`, `cargo-flamegraph`, or `py-spy`).
- An allocation profile showing heap allocations, retained bytes, and copy
  volume on the hot path, when allocation is a suspected cost.
- Hardware counters where available: cache misses, branch mispredictions, and
  instructions-per-cycle for the hottest functions.
- A phase breakdown that separates build/compile time from run time, so a
  build-flag change is never mistaken for a run-time win.

Rank bottlenecks by self time, then compare the **same function across the
candidate and the pristine reference** to confirm a micro-optimization actually
moved the hot spot instead of shifting cost elsewhere or regressing another
path. Capture after warmup and discard the first iterations.

Keep the benchmark's headline cost metric as the authoritative score: a profile
is evidence for **where** to optimize, not the score itself. Reject a profile
that does not match the scored workload identity and configuration, and remember
that output-equivalence and the behavioral-consistency gates still gate any
change the profile motivates.
