---
name: kernel-rocprof-analysis
description: Interpret AMD rocprofv3 and rocprof-compute evidence for a ROCm kernel task. Use when a rocprof capture is available and a measured bottleneck needs diagnosis.
---

# Kernel rocprof analysis

Identify the candidate's dispatches from a system trace before collecting
counters. Library and JIT-compiled kernel names rarely match a friendly
operator name, so take the exact name from the trace instead of guessing a
filter. Match each dispatch to the benchmarked candidate, input shape, dtype,
device, and warmup state; exclude first-call JIT compilation and tuning from
the captured window.

Each PMC pass reruns the profiled workload, and counter and thread-trace
collection perturb timing. Use the task benchmark for before and after
performance decisions.

Form one hypothesis from the relevant counters. Occupancy on CDNA follows
wavefronts of 64 lanes and is bounded by VGPR, AGPR, LDS, and scratch use;
low occupancy alone does not prove it limits throughput. HBM bandwidth and
L2 hit rates need byte counts and access patterns to be meaningful. For
matrix-heavy kernels, compare MFMA issue rate with the architecture's peak
before blaming memory. LDS bank conflicts matter only when LDS traffic is on
the critical path. Reach for instruction-level thread trace only after
counters point at a specific stall class.

Report the capture identity, dominant dispatches, counter values, uncertainty
or unavailable counters, and the next experiment. Confirm any optimization
with the accuracy checker and scored benchmark.
