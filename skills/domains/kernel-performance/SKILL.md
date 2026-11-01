---
name: kernel-performance
description: Optimize a compute kernel using its scored benchmark and measured bottlenecks. Use when choosing launch geometry, tiling, memory layout, fusion, or precision changes.
---

# Kernel performance

Start with a correct baseline and the task's scored metric. Record the device,
shape, dtype, warmup, and timing method for each comparison. Separate compile,
setup, launch, and device execution costs when the benchmark permits it; only
the scored path decides whether a change helps.

Build a performance hypothesis from evidence: bytes moved, arithmetic work,
parallelism, and measured launch count. Change one variable at a time, such as
tile shape, warp count, layout, fusion boundary, or memory staging. Re-run the
accuracy checker before accepting a faster result. Compare medians or another
task-approved robust statistic over repeated runs, accounting for measurement
noise near the claimed improvement.

Use a profiler when timing alone cannot locate the cost. A counter suggests a
cause only when the captured input and launch match the scored workload;
validate the proposed fix with the original benchmark.
