Profile the candidate invocation and input shape that the benchmark scores.
Record the command, device, shape, dtype, warmup, and capture conditions so
the evidence can be compared with the benchmark. Exclude compilation and setup
from a steady-state kernel capture unless the task score includes them.

Report the dominant kernel launches and the counters or timings that support a
specific optimization hypothesis. For CUDA, useful signals can include
achieved occupancy, memory throughput, cache behavior, warp stalls, and launch
overhead. For ROCm, they can include wavefront occupancy and its VGPR or LDS
limit, HBM bandwidth, L2 hit rate, MFMA utilization, and LDS bank conflicts.
Both are subject to the selected profiler's available metrics. State when a
counter is unavailable or a capture perturbs timing. Use the task benchmark
for the final performance comparison.
