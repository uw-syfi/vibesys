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

When GPU time is scarce or queued, compile a new kernel for the target
architecture on a machine without the GPU before you spend a GPU job on it:
Triton and HIP compilers can target an architecture offline and report shared
memory per block, register use and spills. A kernel over the device's
shared-memory limit, or one that spills, fails or runs slowly anyway, so fix
the tile or warp count first. Then use the GPU job for correctness and timing.
